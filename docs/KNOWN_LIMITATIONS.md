# Known limitations

These come from the original indexing and query design and were deliberately left unchanged when the system moved to a fully local setup.

## 1. Input is a local git repository at HEAD

The indexer reads the committed tree at `HEAD`. Uncommitted edits and untracked files are not summarised. The source copy is made from the working tree, so uncommitted edits to tracked files can appear in retrieved source while the summaries describe `HEAD`. Keep the working tree clean when indexing.

Eligibility checks exclude non-regular files, detected binaries, files over `INDEX_MAX_FILE_BYTES` (1 MB by default), and files matching `INDEX_EXCLUDE` / `INDEX_EXCLUDE_EXTRA` in `.env`. `.gitignore` is not consulted: anything committed is a candidate unless excluded. File content beyond 525,000 characters is truncated before analysis.

## 2. Questions are repository-scoped

The agent is directed to identify one repository first. `text_search_summaries` searches one repository's file and folder summaries. There is no automatic search or aggregation across all repositories and no corpus-level root summary.

Search is MongoDB `$text` over the LLM-generated `prose_summary` field. There is no embedding or vector search.

## 3. Repository names

Names may contain only letters, numbers, hyphens and underscores. Each name becomes a database `<DB_PREFIX>_<name>`; MongoDB limits database names to 63 bytes, so keep names reasonably short. The indexer and web app must use the same `DB_PREFIX`.

## 4. Very large folders

Folders with more than 500 direct file summaries get a mechanical stub instead of an ordinary LLM summary, and large-folder processing may condense child details.

The repository summary (`repo_summary`, read by `get_repo_summary`) is generated from the root-folder summary and the top-level folder summaries only, capped at 400,000 characters of input. In a repository with a very large number of top-level folders, the extras are left out of that summary. It is regenerated whenever a folder summary changes, and added on the next run if an older index lacks one.

## 5. Writes are not one transaction

A failed run can leave the index partially updated. Re-running `sync` resumes from the current state.

## 6. Stateless conversations

The agent is stateless: each request carries the whole conversation, and the server keeps no session. Very long conversations cost more input tokens on every turn.

## 7. Temporal lens

- **Review findings are leads.** For code that already uses a Temporal SDK, each file's lens lists review checks it may violate (`f`). In calibration about half were accurate; they point at where to look and must be confirmed against the source before they are reported.
- **The Docs MCP sign-in is personal.** The stored refresh token belongs to whoever ran `scripts/mcp_login.py`. That is fine on a workstation; a shared deployment needs a service credential.
- **Fixed cost per run.** Parallel workers each write the cached system prompt before any can read it, which adds roughly $0.40 per run; noticeable on small repositories only.
- **Tier 0 detection is pattern-based.** Temporal SDK use is detected from imports per language, and other orchestrators from dependency manifests and import lines, using the markers in the pack. Unusual layouts (vendored SDKs, dynamic imports) can be missed.
- **Repository pass reads a budget.** It sees relevance-3 file records first, then relevance-2, then relevance-1, up to about 60k tokens; in very large repositories lower-relevance records are left out (the run says how many).

## 8. Temporal report

- **Leads are checked one file at a time.** A check that depends on another file (for example, whether activities registered in the worker are synchronous) comes back "unclear" and is listed in the appendix, not in the findings.
- **At most 60 leads per report.** Gates come first, then findings by relevance; the appendix says how many were not checked.
- **At most 8 processes per report.** Further processes, and relevance-1 entry points no process covers, are listed but not investigated.
- **Investigation is bounded.** Each process gets at most 25 tool calls. A card can miss a fact that needs more reading (for example, a launch script that decides where a cache file lives); facts it cannot establish say so, and a card without a persistence or crash-loss fact is marked low confidence.
- **The scale is assumed.** For a tool, how many units a run has depends on its input, which the code does not show. Each card states its assumption, and the verdict depends on it.
- **Verdicts near the threshold can change between runs.** Each run investigates and scores again, and a process's overall score moved by up to 0.8 between runs in calibration. Scores within 0.75 of the threshold are labelled borderline. A process near the edge of that band can move between borderline and a neighbouring verdict; calibration saw no move between suitable and not suitable. Cards are reused while their files are unchanged, so repeated reports on an unchanged index give the same verdicts.
- **Uncited prose.** Bar reasons, design elements and reasons for unsuitability must cite a pack entry or a docs page, or they are dropped. The verdict paragraphs and the removes/stays lists are not cited item by item.
- **Only the latest report is kept.** Rebuilding replaces it; earlier reports are not stored.
- **Docs search in chat uses your personal sign-in.** The app container reads the same refresh token as the indexer. If the sign-in expires, the sidebar light turns red and chat says it cannot search the docs; run `python3 scripts/mcp_login.py`, then restart the app.
- **Chat is guided, not checked.** The report's claims are checked in code. The chat assistant is told to rely on the report and the docs and to cite them, but its answers are not checked the same way.
