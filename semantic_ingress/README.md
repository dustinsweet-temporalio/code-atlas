# Semantic Ingress

The indexer behind Code Atlas. It reads a local git repository at its committed `HEAD`, analyses eligible files with an Anthropic model, then generates folder summaries from the deepest folders up to the repository root. Summaries and a directory listing go to MongoDB; the source files are copied to a local source directory so the query agent can retrieve them later.

Most people run it through `./scripts/index.sh` from the repository root (see the top-level README). This page covers running it directly.

## Install (outside Docker)

Python 3.11 or newer:

```bash
python -m pip install -e .
```

Settings come from the environment (the same variables as `.env`; see [docs/CONFIGURATION.md](../docs/CONFIGURATION.md)). Outside Docker you also need `MONGO_URL` (default `mongodb://localhost:27017`), `SOURCE_DIR` (default `/data/source`, so set it to a writable folder) and a Temporal server at `TEMPORAL_ADDRESS` (default `localhost:7233`; `temporal server start-dev` runs one).

## Commands

```bash
semantic-ingress estimate --repo-path /path/to/repo
semantic-ingress sync --repo-path /path/to/repo --repo-id my-repo
```

| Command | Purpose |
| --- | --- |
| `sync` | Index or update a repository, as the Temporal workflow `sync-<repo-id>`. Detects full vs incremental by comparing the index with git |
| `estimate` | Local token and cost estimate. Makes no API calls |
| `repair-folders` | Legacy. Targets the old structured schema and does not work on indexes made by `sync` |
| `dir-list-only` | Regenerate only the directory listing document |
| `source-mirror` | Force the source copy to match the repository exactly (no LLM calls) |

Common options: `--repo-path` (repository root), `--repo-id` (name in the index: letters, numbers, `-`, `_`), `--prefix` (database prefix, default `DB_PREFIX`), `--dry-run`, `--window-size` (summaries generated at a time, default `INDEX_WINDOW_SIZE` = 5; `--max-workers` is a deprecated alias), `--output-dir` (write a local diagnostic export). `--dry-run` skips writes but still calls the LLM.

## `sync` on Temporal

`sync` connects to the Temporal server, starts a worker in its own process (task queue `code-atlas-sync-<repo-id>`), and starts or attaches to the workflow `sync-<repo-id>`. It prints progress while the workflow runs and the same end-of-run summary as before. Ctrl-C leaves the workflow on the server; running the same command again attaches to it and the new worker resumes it from its history.

The workflow (`semantic_ingress/temporal/workflow.py`) runs these phases: plan (pin HEAD, full or incremental, stage the work items) → file summaries → lens refresh (`--refresh-lens`) → folder summaries (deepest level first) → repository summary and lens → delete removed files → persist → index metadata and text indexes → source mirror → Temporal report → disk export → clean up staging. File, lens and folder summaries run in a sliding window of `--window-size` activities. Activities stage their documents in the `code_atlas_ingest` database instead of returning them, so workflow history stays small; the workflow continues as new when Temporal suggests it (`--can-after N` forces it after N items, for testing).

Retries belong to Temporal: in `sync`, the LLM client makes one attempt per call and classifies failures (rate limits, overload and invalid responses are retried with backoff and the API's retry-after hint; other 4xx errors, refusals and unreadable files are not).

## Storage contract

| Artifact | Location |
| --- | --- |
| File summaries | `<prefix>_<repo-id>.file_summaries` |
| Folder summaries, including the root path `.` | `<prefix>_<repo-id>.folder_summaries` |
| Repository summary (`repo_summary`) | `<prefix>_<repo-id>.repo_summaries` |
| Directory listing (`dir_list`) | `<prefix>_<repo-id>.repo_summaries` |
| Mirrored source | `<SOURCE_DIR>/<repo-id>/<path>` |
| Blob IDs for incremental runs | `<SOURCE_DIR>/.oids/<repo-id>/<path>` |

Documents carry `prose_summary`, repository and path identifiers, timestamps and git metadata. `sync` creates MongoDB text indexes on `prose_summary`.

## Notes

- Only committed files at `HEAD` are summarised. The source copy is taken from the working tree.
- Large eligible files are truncated before analysis; very large folders can get a mechanical stub. See [docs/KNOWN_LIMITATIONS.md](../docs/KNOWN_LIMITATIONS.md).
- Writes are not one transaction. After a partial failure, re-run `sync`: an interrupted workflow resumes where it stopped, and a finished one with failures is followed by an incremental run that retries them. (`--resume-cache` and the pickle cache were removed.)
- Reading a bind-mounted repository owned by another user works: libgit2's ownership check is switched off for the indexer.
