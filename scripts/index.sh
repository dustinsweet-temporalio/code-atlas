#!/usr/bin/env bash
# Index a local git repository into Code Atlas.
#
#   ./scripts/index.sh /path/to/repo [name] [extra semantic-ingress sync options...]
#
# `name` is how the repository appears in the index (letters, numbers, - and _).
# It defaults to the folder name. Only committed files at HEAD are indexed.
# Re-runs are incremental; --force rebuilds the whole index from scratch.
# With the Temporal lens on, the Temporal report is built and stored afterwards
# (--no-report skips it).
# Indexing runs as a Temporal workflow (sync-<name>, see http://localhost:8233).
# If a run crashes or you press Ctrl-C, run the same command again: it attaches
# to the running workflow and resumes where it stopped.
#
# Examples:
#   ./scripts/index.sh ~/code/my-service
#   ./scripts/index.sh ~/code/my-service my-service --dry-run
#   ./scripts/index.sh ~/code/my-service my-service --window-size 8
#   ./scripts/index.sh ~/code/my-service my-service --force
#   ./scripts/index.sh ~/code/my-service my-service --no-report
set -euo pipefail

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit $([[ $# -lt 1 ]] && echo 1 || echo 0)
fi

repo_path="$(cd "$1" && pwd)"
shift

name="${1:-}"
if [[ -n "$name" && "$name" != -* ]]; then
  shift
else
  name="$(basename "$repo_path")"
fi
name="$(printf '%s' "$name" | tr -c 'A-Za-z0-9_\n-' '_')"

if [[ ! -d "$repo_path/.git" && ! -f "$repo_path/.git" ]]; then
  echo "error: $repo_path is not a git repository (no .git). Run 'git init' and commit first," >&2
  echo "       or point this at the repository root." >&2
  exit 1
fi

cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then
  echo "error: .env not found. Run: cp .env.example .env  (then set ANTHROPIC_API_KEY)" >&2
  exit 1
fi

# Temporal Docs MCP credentials (written by scripts/mcp_login.py), used by the Temporal lens.
creds_dir="${CODE_ATLAS_CONFIG_DIR:-$HOME/.config/code-atlas}"
mkdir -p "$creds_dir"
mounts=(-v "$repo_path:/repos/$name:ro" -v "$creds_dir:/run/code-atlas")

# Check the pack, Anthropic, MongoDB, the Temporal server and the Temporal Docs MCP before spending anything.
docker compose run --rm "${mounts[@]}" indexer preflight || {
  status=$?
  echo "Preflight failed; not indexing. (If the Temporal Docs MCP login is missing or expired: python3 scripts/mcp_login.py)" >&2
  exit "$status"
}

echo "Indexing $repo_path as '$name' (committed files at HEAD)"
status=0
docker compose run --rm "${mounts[@]}" indexer \
  sync --repo-path "/repos/$name" --repo-id "$name" "$@" || status=$?
echo "Workflow: http://localhost:${TEMPORAL_UI_PORT:-8233}/namespaces/default/workflows/sync-$name"
exit "$status"
