#!/usr/bin/env bash
# Write the Temporal report for a repository indexed with ./scripts/index.sh.
#
#   ./scripts/report.sh <name>
#
# `name` is the name the repository was indexed under. The report says whether
# the repository suits Temporal (with a design, or why it is not a fit) and, if it
# already uses Temporal, how well. It uses only the index, the knowledge pack and
# the Temporal Docs MCP, and is written to reports/<name>/temporal-report.md
# (with the same data as temporal-report.json).
set -euo pipefail

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit $([[ $# -lt 1 ]] && echo 1 || echo 0)
fi
name="$1"
shift

cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then
  echo "error: .env not found. Run: cp .env.example .env  (then set ANTHROPIC_API_KEY)" >&2
  exit 1
fi

creds_dir="${CODE_ATLAS_CONFIG_DIR:-$HOME/.config/code-atlas}"
mkdir -p "$creds_dir" reports

docker compose run --rm -v "$creds_dir:/run/code-atlas" -v "$PWD/reports:/reports" indexer \
  report --repo-id "$name" "$@" || {
  status=$?
  if [[ $status -eq 2 ]]; then
    echo "Preflight failed; no report written. (If the Temporal Docs MCP login is missing or expired: python3 scripts/mcp_login.py)" >&2
  fi
  exit "$status"
}
echo "Report: $PWD/reports/$name/temporal-report.md"
