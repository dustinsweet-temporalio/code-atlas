# Code Atlas query agent and web server

Answers questions about indexed repositories. An LLM agent (Strands with the Anthropic provider) navigates the summaries that `semantic_ingress` wrote to MongoDB and reads mirrored source files when it needs implementation detail. A small FastAPI server streams the answers to the browser UI in [../web/](../web/).

The `tools/` and `utils/` packages are imported as top-level modules, so run everything from this directory.

## Layout

| Path | Purpose |
| --- | --- |
| `app.py` | FastAPI server: `GET /api/health`, `GET /api/repos`, `POST /api/chat` (server-sent events), static UI |
| `indexing.py` | Indexing from the UI: folder picker (`GET /api/fs`), the sync check (`POST /api/sync/check`), starting a sync (`POST /api/sync`), progress (`GET /api/syncs`), deleting an index (`DELETE /api/repos/{name}`) |
| `agent.py` | Builds the model from `QUERY_*` settings and streams the answer; conversation history goes in the system prompt (stateless) |
| `config.py` | Environment-driven settings and the agent system prompt |
| `tools/navigation/` | List repositories; repository, folder and file summaries; directory listing |
| `tools/search/` | Text search over summaries |
| `tools/source/` | Source retrieval from the local source store (`get_file_source`, `get_file_source_lines`), plus manual fallbacks that ask the user to paste code |
| `utils/mongo/` | MongoDB client, validators, exceptions |

## Running outside Docker

```bash
python -m pip install -r requirements.txt
export ANTHROPIC_API_KEY=... MONGO_URL=mongodb://localhost:27017 SOURCE_DIR=/path/to/source-store
uvicorn app:app --port 8080
```

All settings are in [docs/CONFIGURATION.md](../docs/CONFIGURATION.md).

## Chat API

`POST /api/chat` with `{"messages": [{"role": "user" | "assistant", "content": "..."}]}` returns `text/event-stream`. Each event is `data: "<json-encoded text chunk>"`, an error is `data: {"error": "..."}`, and the stream ends with `data: [DONE]`.

## Tools the agent can call

| Group | Tools |
| --- | --- |
| Navigation | `list_indexed_repositories`, `get_repo_summary`, `get_folder_summary`, `get_file_summary`, `get_dir_listing` |
| Search | `text_search_summaries` |
| Source | `get_file_source`, `get_file_source_lines`, `request_full_file_source`, `request_file_source_lines` |
| System | `get_agent_version` |

Text search uses MongoDB `$text` over `prose_summary`. The indexer creates the text indexes; the query side verifies them the first time a repository is used.
