# Configuration

All settings live in `.env` (copy [`.env.example`](../.env.example)). Compose passes the file to both the app and the indexer containers. Optional settings are only sent to the API when they are not blank.

| Setting | Default | Used by | Purpose |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | (required) | both | Anthropic API key |
| `QUERY_MODEL` | `claude-sonnet-5-5` | app | Model that answers questions |
| `QUERY_MAX_TOKENS` | `32000` | app | Output cap per model response (thinking counts toward it on 5.x models) |
| `QUERY_EFFORT` | `medium` | app | `low`, `medium`, `high`, `xhigh`, `max`; blank for models without effort support |
| `QUERY_THINKING` | blank | app | Blank (model default), `adaptive`, or `between_tools` |
| `QUERY_TEMPERATURE` | blank | app | Leave blank on 5.x models |
| `INDEX_MODEL` | `claude-sonnet-5-5` | indexer | Model that writes summaries |
| `INDEX_MAX_TOKENS` | `16000` | indexer | Output cap per summary call |
| `INDEX_EFFORT` | `low` | indexer | As `QUERY_EFFORT` |
| `INDEX_THINKING` | blank | indexer | As `QUERY_THINKING` |
| `INDEX_TEMPERATURE` | blank | indexer | As `QUERY_TEMPERATURE` |
| `INDEX_TIMEOUT_SECONDS` | `300` | indexer | Per-request timeout |
| `INDEX_API_MAX_RETRIES` | `5` | indexer | SDK retries on rate limits and transient errors |
| `INDEX_MAX_WORKERS` | `8` | indexer | Parallel LLM calls (`--max-workers` overrides) |
| `INDEX_PRICE_INPUT_PER_1M` | `2.00` | indexer | USD per million input tokens, for the cost report only |
| `INDEX_PRICE_OUTPUT_PER_1M` | `10.00` | indexer | USD per million output tokens, for the cost report only |
| `INDEX_PRICE_CACHE_READ_MULTIPLIER` | `0.05` | indexer | Prompt-cache reads as a fraction of the input price (0.05 on the 5.5 models, 0.1 on most others), for the cost report only |
| `INDEX_EXCLUDE` | built-in list | indexer | Comma-separated exclude patterns; replaces the built-in list (see the comments in `.env.example` for pattern rules) |
| `INDEX_EXCLUDE_EXTRA` | blank | indexer | Extra exclude patterns added to the list |
| `INDEX_MAX_FILE_BYTES` | `1000000` | indexer | Files larger than this are skipped |
| `LENS_ENABLED` | `true` | indexer | Add a Temporal lens record to every summary; when on, each run first checks the Temporal Docs MCP and stops if it fails |
| `LENS_PACK_PATH` | built into the image | indexer | Path to `temporal-pack.yaml` (the knowledge pack the lens is grounded in) |
| `TEMPORAL_DOCS_MCP_URL` | `https://temporal.mcp.kapa.ai` | indexer | Temporal Docs MCP endpoint |
| `TEMPORAL_DOCS_MCP_CREDENTIALS` | `/run/code-atlas/temporal-docs-mcp.json` | indexer | Credentials file from `scripts/mcp_login.py`, as seen inside the container |
| `REPORT_EFFORT` | `medium` | indexer (`report`) | Effort for the Temporal report's process investigation; the report's other calls use the `INDEX_*` settings |
| `REPORT_RUBRIC_PATH` | built into the image | indexer (`report`) | Path to `temporal-rubric.yaml` (how the report scores processes and Temporal code) |
| `DB_PREFIX` | `semv2` | both | Database name prefix; must start with `sem` and match on both sides |
| `APP_PORT` | `8090` | compose | Host port for the UI (bound to localhost) |
| `COMPASS_PORT` | `8096` | compose | Host port for Compass Web (bound to localhost), opened by **Open in Compass** |
| `LOG_LEVEL` | `INFO` | app | Query-side log level |

Set by `compose.yaml` (no need to change): `MONGO_URL=mongodb://mongo:27017`, `SOURCE_DIR=/data/source`, `COMPASS_URL=http://compass:8080`, `COMPASS_PUBLIC_URL=http://localhost:<COMPASS_PORT>`.

## Changing models

Any Anthropic model ID can go in `QUERY_MODEL` and `INDEX_MODEL`. When switching, check the optional settings against the model:

| Model family | `*_EFFORT` | `*_TEMPERATURE` | `*_THINKING` |
| --- | --- | --- | --- |
| `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5` | supported | leave blank (400 error otherwise) | blank, `adaptive`, or `between_tools` |
| `claude-haiku-4-5`, `claude-sonnet-4-5` | leave blank | allowed | leave blank |

If a request fails with a 400, the error names the rejected parameter. Also update the two `INDEX_PRICE_*` values so the cost report matches the model.

## Data locations

| Data | Where |
| --- | --- |
| Summaries | `mongo-data` volume (MongoDB), one database per repository |
| Mirrored source | `source-data` volume, `<SOURCE_DIR>/<name>/<path>`; blob IDs in `<SOURCE_DIR>/.oids/<name>/<path>` |
| Chat history | Browser local storage |
| Temporal Docs MCP sign-in | `~/.config/code-atlas/temporal-docs-mcp.json` on the host (0600), mounted at `/run/code-atlas` into the indexer and, for docs search in chat, into the app (read-write, so a rotated token can be saved) |
| Knowledge pack | `knowledge/temporal-pack.yaml`, built into the image at `/app/knowledge` |
| Temporal reports | `reports/<name>/temporal-report.md` and `.json` in this folder, mounted into the indexer at `/reports` |
| Process cards | `process_cards` collection in the repository's database; reused by later reports while the files they cite are unchanged |
| Stored Temporal report | `reports` collection (the latest report, with its commit and versions); cached report steps in `report_cache` |
| Report rubric | `knowledge/temporal-rubric.yaml`, built into the image at `/app/knowledge` |
