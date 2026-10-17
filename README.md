# Code Atlas

Ask questions about your code. Code Atlas indexes a git repository into a hierarchy of LLM-written summaries (files, then folders, up to the repository root), then answers questions by navigating that hierarchy and pulling in source code where needed.

Everything runs locally in Docker. What leaves your machine: the LLM calls, which go to the Anthropic API with your key, and, with the Temporal lens on (the default), documentation searches sent to the Temporal Docs MCP (search questions only, never your code).

## Quick start

```sh
cp .env.example .env          # then set ANTHROPIC_API_KEY in .env
docker compose up --build -d  # MongoDB, the Temporal dev server and the web app
python3 scripts/mcp_login.py  # one-time sign-in to the Temporal Docs MCP (for the Temporal lens)
./scripts/index.sh /path/to/your/repo [name]
```

Skip the sign-in by setting `LENS_ENABLED=false` in `.env` if you do not want the Temporal lens.

Open <http://localhost:8090> (change the port with `APP_PORT` in `.env`), select an index on the left, click **New chat**, and ask your question. Each conversation stays with the index it was started on; the index is shown above the chat and under the conversation's name. Indexing runs are visible in the Temporal UI at <http://localhost:8233> (the **Temporal UI** link at the bottom of the sidebar).

## Indexing a repository

```sh
./scripts/index.sh ~/code/my-service                 # indexed as "my-service"
./scripts/index.sh ~/code/my-service backend         # indexed as "backend"
./scripts/index.sh ~/code/my-service backend --dry-run
```

- The folder must be a git repository. The indexer reads the **committed files at `HEAD`**; uncommitted changes and untracked files are not seen. Getting the git state you want before indexing is up to you.
- The name is how the repository appears in the UI and in questions. Use letters, numbers, `-` and `_`.
- What gets skipped (folders, file types, size limit) is set in `.env`; see `INDEX_EXCLUDE` and the comments above it.
- Re-running on the same name is incremental: only files whose content changed are re-summarised. Add `--force` to rebuild the whole index from scratch (for example `./scripts/index.sh ~/code/my-service my-service --force`). An index built by an older version of the indexer is rebuilt automatically the first time, and the run says why. When a new version changes what summaries say (the summary prompt version), existing summaries are kept: changed files get the new version, the run warns, the UI marks the repository "mixed summaries", and `--force` regenerates everything.
- Before spending money, `docker compose run --rm -v /path/to/repo:/repos/x:ro indexer estimate --repo-path /repos/x` prints a token and cost estimate without calling the API.

The script mounts your folder read-only into the indexer container, so nothing in your repository is modified. It first runs a preflight (knowledge pack, Anthropic API, MongoDB, Temporal server, Temporal Docs MCP) and stops before spending anything if a check fails.

### Indexing runs on Temporal

Each run is a Temporal workflow, `sync-<name>`, on the Temporal dev server in `compose.yaml`. The indexer container runs the worker for as long as `index.sh` runs, and prints a link to the workflow in the Temporal UI (<http://localhost:8233>), where you can follow every file, folder and retry.

- **Resume:** if a run crashes, the container is killed, or you press Ctrl-C, run the same `index.sh` command again. It attaches to the running workflow (with the options it was started with) and carries on; finished summaries are not generated or paid for again. Only one run per name can be in progress.
- **Throughput:** up to 5 file or folder summaries are generated at a time (`INDEX_WINDOW_SIZE` in `.env`, or `--window-size`). The old default was 8 parallel calls, so a run with default settings takes somewhat longer than before; raise the window if your rate limits allow. `--max-workers` still works as an alias.
- **Retries:** rate limits and transient API errors are retried by Temporal with backoff (honouring the API's retry-after hint), and show up as retries in the UI. A file that still fails is listed at the end of the run, and the next run picks it up again.
- **Nothing is written to the index until generation is done:** summaries are staged in a separate database (`code_atlas_ingest`) and written to the index in one phase at the end; a full re-index replaces the old index only then.

## The Temporal lens

With `LENS_ENABLED=true` (the default), indexing also records how each file, folder and the repository relate to durable execution: side effects, waits, hand-rolled retries and status tables, use of Temporal or of other orchestrators, and which Temporal patterns apply. It is grounded in a knowledge pack, `knowledge/temporal-pack.yaml`, whose entries were checked against the Temporal documentation, and the repository-level pass cites Temporal Docs pages it looked up during the run. In the UI, the repository list shows the pack version an index was built with; ask about durability, orchestration or Temporal and the agent pulls the lens in.

- **Sign-in:** `python3 scripts/mcp_login.py` opens your browser once and stores a refresh token in `~/.config/code-atlas/` (readable only by you). Run it again if indexing reports the login expired.
- **Cost:** roughly $7 per thousand files on top of normal indexing, plus a fixed ~$0.40 per run while parallel calls warm the prompt cache. `estimate` includes it.
- **Pack updates:** a newer pack version does not regenerate anything by itself. Changed files use it, the UI marks the index as mixed, and `--refresh-lens` updates every file's lens (summaries are not regenerated): `./scripts/index.sh ~/code/my-service my-service --refresh-lens`.
- **Off:** set `LENS_ENABLED=false`; indexing then behaves as before and does not contact the Docs MCP.

### Temporal report

With the Temporal lens on, indexing also builds the Temporal report and stores it in the index (`--no-report` skips it). Open it in the UI with **Temporal report** above the chat, or ask about it in the chat. To rebuild it on its own, or to get it as files:

```sh
./scripts/report.sh my-service      # also writes reports/my-service/temporal-report.md and .json
```

The report finds the repository's processes and judges each one on its own: should it use Temporal, and why? If the repository already uses Temporal, the report also reviews that code.

- **Per process:** the report investigates each process in the source and writes up how it runs today: what triggers it, the unit of work and how it scales, the steps and their side effects, where state lives, when results are persisted, and what a crash at each stage loses. Every fact cites source lines and is checked against them.
- **Scores:** a process needs one strong reason to use Temporal: durable pipeline (long or expensive multi-step work, such as LLM pipelines), distributed transaction, long-lived waits, or scheduled work. Its fit is the strongest of these, on a 0–10 scale. Reliability burden, separability and migration ease adjust the score, and verified gates (reasons Temporal is a poor fit) cap it.
- **Verdicts:** suitable from 5.75/10, not suitable up to 4.25, and borderline in between, because a process's score can differ by up to about 0.7 between runs. Suitable and borderline processes get a design (workflows, activities, signals, timers; what code it removes, what stays outside, and why) and count toward the share of durable work that moves to Temporal. Not suitable processes get the reasons, and what would change the verdict.
- **Existing Temporal code:** bars scored from review findings confirmed against the source, each with a fix.

It uses only the index, the mirrored source, the knowledge pack, the report rubric (`knowledge/temporal-rubric.yaml`) and the Temporal Docs MCP. Code checks every fact, path and citation the model writes; anything it cannot ground is dropped and listed in the appendix.

- **In the UI:** the report view leads with the opportunities (each process that would gain from Temporal, its strongest reason, its score, and what you gain), then the picture at a glance, then details per process: how it runs today with line references, what a crash loses, the score breakdown and the design. **Discuss in chat** starts a question about a process. **Detailed report** switches to the complete report (the same document as `temporal-report.md`), and **Download** saves the view you are on: the summary as a standalone HTML page, the detailed report as Markdown.
- **In the chat:** the assistant reads the stored report and can search the Temporal docs (Temporal Docs MCP) for follow-up questions, citing what it uses. The light at the bottom of the sidebar shows whether the docs search works: green when connected, red with the reason and fix in its tooltip, grey when you have not signed in (`python3 scripts/mcp_login.py`, then `docker compose up -d app`).

A first report costs about $0.15–0.30 per process investigated (at most 8). Every model step is cached on its inputs, so re-indexing unchanged code rebuilds the report for about nothing, and a change re-runs only the processes it touches; `./scripts/report.sh my-service --fresh` runs everything again.

## Choosing models

Set these in `.env`; any Anthropic model ID works. Defaults are `claude-sonnet-5-5` for both.

| Setting | Purpose |
| --- | --- |
| `QUERY_MODEL` | Answers questions |
| `INDEX_MODEL` | Writes the summaries. Most of the cost is here, so this is the one to drop to a cheaper model (for example `claude-haiku-5-5`) |
| `*_MAX_TOKENS` | Output cap per response (raised well above the old 4096) |
| `*_EFFORT`, `*_THINKING`, `*_TEMPERATURE` | Optional. Only sent when set, because models differ (see below) |
| `INDEX_PRICE_INPUT_PER_1M`, `INDEX_PRICE_OUTPUT_PER_1M` | Used only for the cost report. Update when you change `INDEX_MODEL` |

Model differences that matter:

- **Claude 5.x** (`claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`) reject a non-default `TEMPERATURE`. Leave it blank. They support `EFFORT` (`low` to `max`). Thinking is on by default and its tokens count toward `MAX_TOKENS`.
- **Haiku 4.5 and other older models** do not support `EFFORT`: blank it. `TEMPERATURE` is allowed.
- If you change the model and a request fails with a 400, the optional settings are the first thing to check. The error text names the rejected parameter.

All settings are documented in [`.env.example`](.env.example) and [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## How it works

```
 your git repo ──► indexer (Temporal worker) ──► MongoDB   (file + folder summaries, text-indexed)
 (read-only)          │        ▲
                      │        └── Temporal dev server (the sync workflow; UI on :8233)
                      └──────► source volume (copy of the files, for source retrieval)

 browser ──► app (FastAPI + agent) ──► MongoDB, source volume
                       │
                       └──► Anthropic API
```

| Path | Purpose |
| --- | --- |
| [semantic_ingress/](semantic_ingress/README.md) | The indexer: git tree analysis, LLM summaries, MongoDB persistence, source mirroring; `sync` as a Temporal workflow |
| [code_atlas/](code_atlas/README.md) | The query agent (tools, prompt) and the web server |
| [web/](web/) | The browser UI (plain HTML/JS, no build step; libraries vendored in `web/vendor/`) |
| [compose.yaml](compose.yaml), [Dockerfile](Dockerfile) | One image used for both the app and the indexer |
| [docs/](docs/INDEX_FLOW.md) | Process diagrams and known limitations |

Questions are answered by an agent that starts at the repository's root summary, follows relevant folders, and reads file summaries and source as needed. See [docs/INDEX_FLOW.md](docs/INDEX_FLOW.md).

## Data and housekeeping

- Summaries live in the `mongo-data` Docker volume; mirrored source in `source-data`; Temporal workflow history in `temporal-data`.
- `docker compose down` stops everything and keeps your data. `docker compose down -v` also **deletes the index and the mirrored source**.
- MongoDB is not published to your host. To inspect it: `docker compose exec mongo mongosh`.
- Maintenance commands (run through `docker compose run --rm indexer <command> ...`): `sync`, `report`, `preflight`, `estimate`, `dir-list-only`, `source-mirror`. Add `--help` to any of them.
- Temporal reports are written to `reports/<name>/` in this folder (ignored by git).
- Chat history is stored in your browser only.

## Troubleshooting

- **Banner says `ANTHROPIC_API_KEY is not set`**: set it in `.env` and run `docker compose up -d` again (containers read `.env` at start).
- **A different page or a 404 at the URL**: another app is probably using the port. Set `APP_PORT` in `.env`, then `docker compose up -d`.
- **`429` / rate limit errors while indexing**: lower `INDEX_WINDOW_SIZE` (or pass `--window-size`). Temporal already retries with backoff; the retries are visible in the Temporal UI.
- **Preflight: `Temporal server not reachable`**: run `docker compose up -d temporal` (the indexer also starts it on demand) and check `TEMPORAL_ADDRESS`.
- **A run was interrupted**: run the same `index.sh` command again; it resumes the workflow. To abandon it instead, terminate `sync-<name>` in the Temporal UI (staged summaries are removed after 7 days).
- **Summaries look truncated or JSON errors during indexing**: raise `INDEX_MAX_TOKENS` or lower `INDEX_EFFORT`.
- **"Not a valid Git repository"**: pass the repository root (the folder that contains `.git`).
- **Preflight: `No Temporal Docs MCP credentials` or `token refresh refused`**: run `python3 scripts/mcp_login.py` (or set `LENS_ENABLED=false`).
- **Preflight: canary search returned too few results**: the Temporal Docs MCP is degraded; try again later.
- **UI shows `pack 0.1.0 + 0.1.1` or `(partial)`**: some files carry an older lens or none; index again with `--refresh-lens`.

## Known limitations

Carried over from the original design and unchanged on purpose: see [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). The main ones are that questions are scoped to one repository at a time and search is text search over summaries (not embeddings).
