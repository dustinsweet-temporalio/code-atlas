# Temporal lens: implementation plan

Status: approved (2026-10-16). Scope: the indexer (`semantic_ingress`), the query tools (`code_atlas`), and the launch path (`scripts/index.sh`, compose, Dockerfile). Report generation and BUILD are separate, later plans; this one produces the index they will read.

## Goals

1. **Full-depth folder summaries.** A folder summary describes the folder and everything beneath it, and stays complete on incremental runs.
2. **Git-driven re-index.** A file is regenerated whenever its git blob changes. The LLM stability check is removed.
3. **Temporal lens.** Every file, folder and the repository get a lens record. It is on by default (`LENS_ENABLED=true`), and incremental runs redo only changed files and their ancestor folders.
4. **Preflight.** Before any spend, the indexer validates the pack and checks Anthropic, MongoDB and the Temporal Docs MCP. If any check fails, the run stops (inside `scripts/index.sh`).
5. **Grounding.** Lens content may reference only pack ids and signal codes. Live Docs MCP use happens in preflight and the repository lens pass.
6. **Temporal-ready structure.** Per-file, per-folder and repo steps are pure functions of their inputs, so the planned move to Temporal workflows makes each one an activity with no redesign. No tree-sitter.

## Decisions (from the design discussion and calibration)

| Topic | Decision |
| --- | --- |
| Call shape | One combined LLM call per file returns summary and lens (the file content is the dominant cost). `LENS_ENABLED=false` sends the summary-only schema with today's instructions. |
| Pack in prompt | A cached system prompt, mode-specific. **Conversion view** (~5k tokens): signals, gates, values, patterns and conversion entries with statements. **Evaluation view** (~12k tokens): signals, patterns, limits and checks with statements, plus the file language's `S-*` entries. Mode per file comes from a Temporal-import regex. |
| Output | Schema-enforced JSON (`output_config.format`), with the schema derived from the Pydantic models. |
| Caps | Narrative at most 35 words for rel 1 and 55 words for rel 2–3; rel 0 uses the fixed "No durable-execution relevance: ..." form. Retry once only when more than 20% over. |
| Normalisation | Done in code, not by retry. Drop unknown signal codes and ids, dedupe findings, strip `hazard.*` unless kind includes ORCH/TWF, drop anchorless signals at rel >= 2. Pin rel for Temporal code: TWF/TWORKER = 3; TACT and options = 2 unless the file orchestrates. |
| Findings (`f`) | Stored as **leads**. Calibration showed about 50% precision, so the report stage must verify each lead against source lines and the pack statement before stating it. |
| Storage vs serving | Store full JSON (with line anchors, which BUILD needs). Serve agents a compact line (about 35% fewer tokens, no anchors). |
| Folder lens | Deterministic roll-up of the whole subtree's file records, plus at most 40 words of prose written in the same folder call, only when a descendant has rel >= 2. |
| Pack version source | `meta.version` in `knowledge/temporal-pack.yaml`. It is bumped by hand when entries change meaning: a new entry, a revised statement, or a changed rubric. Typo-level edits change only the hash, which is recorded but never surfaced as a new version. |
| Repo lens docs search | Required, not optional: the pass searches the Docs MCP for the Temporal pattern behind each key process (up to 6 searches) and cites the returned docs.temporal.io page. Added in Phase 4 after a run where the model skipped optional searches; a run with no searches is recorded as a correction. |
| Repo lens | Tier 0 deterministic detection, then one LLM pass with a Docs MCP search tool. It produces status, archetype, process candidates, anti-signals and a narrative citing pack ids and MCP URLs. |
| MCP auth | One-time host login stores a refresh token. The container mounts it and refreshes access tokens itself. Tested 2026-10-16: refresh works headlessly, the token did not rotate in two runs, and the canary passes. |
| Re-index mode | Incremental by default. `--force` (on `sync` and passed through by `index.sh`) runs a full re-index. An index whose storage schema version is older than the code's also gets a full re-index, with the reason printed. |
| Pack version | Every lens record carries the pack version that produced it, and `index_meta` records the index's pack version. A newer installed pack never triggers regeneration by itself. The run warns, changed files use the new pack, and `--refresh-lens` (user's choice) brings every record up to it. The UI shows each index's pack version and flags a mixed index. |

## Work plan

Each phase leaves the indexer working and is verifiable on its own.

### Phase 0: storage foundation (prerequisite for everything)

- `models/storage.py`: `StoredDocument` gains:
  - `content: dict` (the structured summary, `model_dump()`),
  - `lens: dict | None`,
  - `lens_prose: str | None` (the compact serving line),
  - `schema_version: int`.
- New `index_meta` document per repository (in a `meta` collection), written at the end of every successful sync:
  - `repo_id`, `schema_version`, `index_model`, `commit_sha`, `indexed_at`;
  - `lens_enabled`;
  - `pack`: `{id, version, hash}`, the pack used by this run;
  - `lens_pack_versions`: count of file records per pack version (for example `[{"version": "0.1.0", "files": 1980}, {"version": "0.2.0", "files": 20}]`; a list because dotted versions make poor Mongo keys). One entry means a uniform index; more than one means mixed.
- `generation_orchestrator._convert_to_stored_documents`: persist `content` for files, folders and repo. Lens records carry `pack_version`.
- **`--force`:** a new `sync` option that skips the previous-snapshot load and runs a full index, deleting the repository's existing documents first so removed paths don't linger. `index.sh` already passes extra options through (`./scripts/index.sh ~/code/svc svc --force`); document this in its usage text and the README.
- **Migration:** if the stored `schema_version` is missing or older, print why and run a full index, the same path as `--force`. Old documents have no `content`, so they cannot feed incremental folder regeneration.
- `core/folder_repair.py`: read `content`, which now exists, instead of the never-written field.
- Mongo text index: replace the `prose_summary`-only index with a weighted text index on `prose_summary` and `lens_prose`, for file and folder collections. Index creation must drop the old `text_idx` first, since Mongo allows one text index per collection.

### Phase 1: core fixes (goals 1 and 2)

**Git-driven re-index**
- Delete the stability path: `SEMANTIC_STABILITY_PROMPT`, `SemanticStabilityVerdict`, both `check_semantic_stability*` methods, `SemanticStabilityConfig`, `_check_stability`, `_load_prose_summary_from_index`, and the "CHECKING SEMANTIC STABILITY" block.
- `_sync_incremental`: `files_to_regenerate = added + modified`.

**Full-depth folders**
- *Incremental bug fix.* Before regenerating affected folders, load stored `content` (and `lens`) for every unchanged direct child file and unchanged subfolder of each affected folder, so a folder is always rebuilt from all its children. Add one bulk query per collection, filtered by parent path.
- *Prompt change.* `FOLDER_SUMMARY_PROMPT` stops describing direct children only. The `summary` field becomes a description of the folder's responsibility across its whole subtree, built from the child file roles and the child folders' own full-subtree summaries, which are already recursive because generation is bottom-up. The "do not recurse/flatten" rule stays for the `files`/`subfolders` lists, which still list direct children.
- *New deterministic `subtree` block* on folder content: total files, depth, and file counts by top-level child. No LLM.
- Ancestors already regenerate (`_get_affected_folders` includes every parent). The repo summary regenerates when any folder changed (unchanged).

### Phase 2: lens infrastructure

- **Config** (`config.py`, `.env.example`, `docs/CONFIGURATION.md`):
  - `LENS_ENABLED=true`
  - `LENS_PACK_PATH` (default `/app/knowledge/temporal-pack.yaml`)
  - `TEMPORAL_DOCS_MCP_URL` (default `https://temporal.mcp.kapa.ai`)
  - `TEMPORAL_DOCS_MCP_CREDENTIALS` (default `/run/code-atlas/temporal-docs-mcp.json`)
- **Pack loader and validator** (`semantic_ingress/lens/pack.py`):
  - load the YAML;
  - validate it: unique ids, every `on` code in `signals`, required keys, `v` values;
  - compute a content hash;
  - build the two view texts, deterministic and byte-stable so the prompt cache hits. Views and reports exclude entries with `v: conflict` or `v: stale`, since those contradict the docs.
  - Add a `detection` section to the pack: Temporal SDK markers per language (imports, manifests) and adjacent-orchestrator markers. Tier 0 and the per-file mode regex then come from the pack, not from code.
- **Models** (`semantic_ingress/lens/models.py`):
  - `FileLens`: rel, cov, kind, sig, sym, c, f, n.
  - `FolderLens`: rollup stats, plus `n`.
  - `RepoLens`: status, archetype, detected, processes, anti, n, cites.
  - `FileSummaryWithLens`: the combined call schema.
- **Normaliser and serialiser** (`lens/normalize.py`, `lens/serialize.py`): the rules from the Decisions table; the compact line format exactly as calibrated.
- **LLMClient**:
  - add `system` blocks with `cache_control`;
  - add `output_config.format` (JSON schema from the Pydantic model);
  - when the schema is enforced, skip the regex JSON-repair path;
  - record `cache_read`/`cache_write` usage in the cost report.

### Phase 3: lens generation

- **File:** `ArtifactGenerator.generate_file_summary` picks the view (conversion or evaluation, plus language), sends the combined schema, then normalises. Narrative-cap retry happens here, once.
- **Folder:**
  - The roll-up is computed in code from the subtree's file lens records (fresh plus stored): count by rel, kind histogram, top signals and concepts, and up to 10 rel-3 paths.
  - The folder prompt receives the compact lens lines of direct children and the child folders' roll-ups, and returns the summary plus the lens `n`.
- **Repo:**
  1. Tier 0: scan manifests and imports using the pack `detection` markers.
  2. One LLM call with a client-side tool `search_temporal_docs` (backed by the MCP client) in a bounded tool loop (at most 6 calls).
  3. Input: Tier 0 facts, the root roll-up, and compact lines for rel 3, then rel 2, then rel 1 (budget-capped, about 60k tokens; rel 1 added in Phase 6 so gates such as G-READONLY have evidence paths).
  4. The `RepoLens` validator rejects ids not in the pack and URLs not returned by the tool in this run.
- **Pack version handling.** At the start of a sync, compare the installed pack version with `index_meta.pack.version`:
  - **Same version:** normal incremental run.
  - **Different version, no flag:** print a warning: "index lens built with pack X; installed pack is Y. Changed files will use Y. Run with --refresh-lens to update all files." Then run normally. Changed files get version-Y lens records, `lens_pack_versions` shows the mix, and nothing else is regenerated.
  - **`--refresh-lens`:** the user's explicit choice. It regenerates the lens for every unchanged file through a lens-only schema (no re-summarising, so roughly half the cost of a full index). It then recomputes folder roll-ups and the repo lens and makes `lens_pack_versions` uniform.
  - **`LENS_ENABLED` turned on for an index built without it:** treated like `--refresh-lens`, but only when the flag is given. Otherwise only changed files get a lens, and the index shows as partial.
- `--force` also regenerates everything with the installed pack.
- **Cost estimate:** `estimate` adds the lens cost (cached system-prompt reads plus the extra output per file).

### Phase 4: preflight, MCP client, launch path (goal 4)

- `semantic_ingress/lens/mcp_docs.py`: a minimal streamable-HTTP JSON-RPC client.
  - It handles refresh-token exchange, `initialize`, and `tools/call`.
  - It parses multi-block text content.
  - It writes back a rotated refresh token if the server ever returns one.
- New CLI command `semantic-ingress preflight`; `sync` also calls it first. Checks, in order, all before any LLM spend:
  1. Pack loads and validates.
  2. Anthropic reachable (free `count_tokens` call against `INDEX_MODEL`).
  3. MongoDB reachable.
  4. MCP: token refresh, `initialize`, then a canary search. The canary must return at least 5 chunks, including at least one docs.temporal.io URL, within 30 s.

  Any failure exits with code 2 and names the fix, for example "run scripts/mcp_login.py". MCP checks run only when `LENS_ENABLED=true`.
- `scripts/mcp_login.py`: the tested host-side login, moved from the scratchpad. It uses only the standard library and stores credentials at `~/.config/code-atlas/temporal-docs-mcp.json` with mode 0600.
- `scripts/index.sh`:
  - mount `~/.config/code-atlas` read-write at `/run/code-atlas`;
  - run `preflight` and stop on non-zero;
  - then run `sync`.
- `Dockerfile`: `COPY knowledge /app/knowledge`. `pyproject.toml`: add `pyyaml`.
- **Risk to verify first:** container user `atlas` (uid 1000) reading a 0600 file owned by uid 501 on Docker Desktop bind mounts. Fallback: `index.sh` refreshes on the host and passes a 1-hour access token in an environment variable. That covers preflight; the repo pass then needs to run within the hour or refresh again.

### Phase 5: query side

- `get_file_summary`, `get_folder_summary` and `get_repo_summary` gain `include_lens: bool = False`. When true, a `Temporal lens:` block with the compact line or roll-up is appended.
- `text_search_summaries` searches `lens_prose` through the new weighted index.
- The `config.py` system prompt gets one line: use `include_lens` when the question is about durability, failure handling, orchestration or Temporal. Normal Q&A stays unchanged in token cost.
- **Pack version in the UI.**
  - `GET /api/repos` (`code_atlas/app.py`) adds `pack_version`, `lens` (`"on"`, `"off"`, `"partial"` or `"mixed"`) and `indexed_at`, read from `index_meta`. Indexes built before this change report `lens: "off"` and no pack.
  - `web/app.js` renders the repo row meta as, for example, `1,240 files · pack 0.1.0`. A mixed index shows `pack 0.1.0 + 0.2.0`, with a tooltip giving the per-version counts and the `--refresh-lens` hint.
  - `list_indexed_repositories` and `get_repo_summary` also include the pack version, so answers and reports can state which pack they rest on.

### Phase 6: tests and docs

- Add `pytest` (dev dependency) with unit tests for:
  - pack validation and view byte-stability;
  - normaliser rules (one test per calibration failure);
  - the compact serialiser;
  - folder roll-up math;
  - incremental child loading (unchanged siblings present);
  - delta → regenerate set (no stability call);
  - preflight failure ordering (no LLM call when MCP fails), against a fake MCP server;
  - MCP client content parsing.
- **Dogfood checks** on this repo and the 5 calibration repos:
  - full index with `LENS_ENABLED=true` and `false`;
  - touch one file, re-run, and confirm only it and its ancestors regenerate and the folder summary still lists all siblings;
  - bump the pack version and confirm: a plain run warns and only changed files move to the new version; `--refresh-lens` makes the index uniform; the UI shows the version and the mixed state correctly;
  - `--force` on an indexed repo rebuilds everything and leaves no stale documents;
  - break the MCP credentials and confirm the run stops before spend.
- Update `README.md`, `docs/INDEX_FLOW.md` (new diagram: preflight, then files, folders, repo, each with lens) and `docs/CONFIGURATION.md`.

### Phase 7: report generation

Added 2026-10-16 so the work ends in a demo: index a repository, then produce the suitability report. It reads the repo and file lens records, scores the rubric bars, verifies findings ("leads") against source lines, grounds Temporal claims in pack ids and live Docs MCP results, and writes the Markdown report.

**Scope (approved 2026-10-16)**

Demo: `./scripts/index.sh <repo> <id>`, then `./scripts/report.sh <id>`, which writes `reports/<id>/temporal-report.md` on the host.

Inputs, and nothing else:
- The index: repo lens, folder roll-ups, file lens records, and `index_meta` (commit, pack version), plus the source mirror the indexer keeps (used to verify leads and count lines, so no repo checkout is needed).
- The pack at the version the lens was built with. A mismatch or mixed lens prints a warning and is stated in the report header.
- The Docs MCP. It is required, with the same preflight as indexing.
- The temporal-developer skill reaches the report through the pack: it was distilled there as the `SKL` source.

Grounding rule: the prompts forbid Temporal claims from general knowledge. Code then checks that every bar, process, finding and design element cites pack ids, or docs URLs returned in this run, and that every path exists in the index. A failed check gets one retry; anything still failing is dropped and listed in the appendix.

Mode, from the repo lens status:
- `none` or `adjacent`: suitability (conversion).
- `full`: evaluation.
- `partial`: both, as separate sections.

Steps:
1. **Gather** (code, no LLM): processes, gates, signal hits per bar input with their files, and line counts.
2. **Verify leads** (LLM, batched per file):
   - Each gate's evidence, and in evaluation mode each `f` finding, is checked against the pack statement and the source lines around its anchor.
   - The result is confirmed, rejected or unclear, with one sentence and a line anchor. Only confirmed leads reach the report body.
   - At most 60 leads are verified, relevance 3 first.
3. **Score**:
   - Conversion bars: one structured call picks each bar's level from the rubric anchors, with evidence (paths, signal codes, pack ids). B-GATES must agree with the verified gates. The overall score is computed in code from the rubric formula, including the gate cap.
   - Evaluation bars: computed in code. Each starts at 10 and loses points per confirmed finding mapped to its inputs (high 3, medium 1.5, low 0.5). B-E-COVERAGE comes from conversion signals in non-Temporal files.
4. **Design or remediate**:
   - Conversion, when the overall score is 5 or more: one call with the docs search tool, at least one search per process and at most 8 in total. For each top process it gives the workflow(s), the activities (mapped to files and symbols), signals, queries, timers and schedules, the hand-built reliability code removed, what stays outside Temporal, and the WHY.
   - The % Temporalized is computed in code from that mapping, per `rubric.metrics.pct_temporalized`.
   - Evaluation: a remediation per confirmed finding, grouped by bar, each with its pack id and docs URL.
5. **Not suitable** (overall below 5): no design. The report gives the verdict, the gates with evidence, and what would change the verdict.
6. **Render** (code, fixed template, so reports are consistent across repositories):
   - Header: repo, commit, pack version, lens state, date, cost.
   - One-paragraph verdict.
   - Bars, as `████████░░ 8/10` plus a one-line reason each.
   - Processes, then the design or the findings, then % Temporalized.
   - Open questions from the pack's `questions`.
   - Citations.
   - Appendix: rejected leads and dropped claims.
   - A JSON file of the same data is written next to the Markdown; BUILD will read it later.

Code:
- A `report/` package in semantic_ingress, a `report` CLI command and `scripts/report.sh`.
- It reuses the LLM client, the MCP client, preflight and the Mongo store, with the same model settings as indexing.

Tests (fake LLM):
- the grounding checks;
- the overall formula and gate cap;
- evaluation scoring;
- % Temporalized;
- bar rendering.

Expected cost: about $0.30–1.00 per report.

Not in Phase 7: reports in the UI, an agent tool for reports, alternative designs, BUILD.

**Done (2026-10-16).** Code: `semantic_ingress/semantic_ingress/report/`, the `report` command and `scripts/report.sh`. Decisions applied:
- Severity is set by the verification call, from three fixed definitions.
- Designs are written from an overall score of 5.
- Gates that had leads but were not confirmed anywhere cannot be cited.

Demo runs (Sonnet 5.5, effort low):
- `phase3-check`, partial: 5.8–5.9/10, suitable. The run designed 2 processes and evaluated 7 Temporal files; 2–3 of 10 leads were confirmed. It cost $0.15–0.19.
- `delivery-matching`, none: 1.3/10, not suitable. G-READONLY was confirmed and G-LATENCY rejected. It cost $0.04–0.09.

Follow-up for the rubric: 34 review checks (mostly E-WFO, E-AC, E-SAGA, E-TMR, E-CXL) are not inputs of any evaluation bar. Their findings are reported under "Other findings" without affecting a score.

### Phase 8: process-level understanding and rubric v2

**Status: done 2026-10-17 (see Progress). Approved 2026-10-16, with all three recommended decisions (calibrate freely and record the results here; investigator at effort medium via REPORT_EFFORT; rubric in its own versioned file).**

**Why.** The report on this repository scored the indexer 4.4/10, "not suitable", although a long-running, paid, fan-out indexing run is a textbook Temporal use case. The causes are in the design, not in that one run:

1. **The rubric can't see pipelines.** The LLM-call, batch, flaky-API and CLI-entry signals feed no conversion bar. Durability anchors count only business money, infrastructure and customer-visible state. B-TIME measures waiting, not run length.
2. **A weighted mean punishes a process for not having every reason to use Temporal.** One strong reason (long waits, expensive multi-step work, compensation, human steps, schedules) is enough.
3. **Scale is invisible.** What a crash costs depends on how many units of work a run has and what each costs. Nothing records either.
4. **File-local records can't establish process facts.** The orchestrator's record tagged the pickle file as a checkpoint and said a crash loses "in-flight" work. In fact the pickle is written only after all generation, into the temp directory of a `--rm` container, and `cli.py` persists only at the end. The file prompt asks each file "what is lost if the process dies", which a single file cannot know.
5. **Checking was one-sided.** Gates and review findings were checked against the source; the claims the scores rest on were not.
6. **One score for the whole repository.** The read-only chat side dragged the indexer down.
7. **No known-answer test of the verdict.**

**Approach.** The file lens says where to look; a process pass decides what is true. The file lens stays: it is cheap, sees every file, and suits file-local review checks. A new process pass investigates each process over the index and the source mirror and fills in a fixed set of facts with line references, called a process card. The cards are checked against the source, and each process is scored from its card. Understanding the code is grounded in the source; judging Temporal fit stays grounded in the pack and the Docs MCP, so the investigator needs no Temporal knowledge.

**Work, in order (each step committed when done):**

8a. **Rubric v2, in its own file.**
- Move the rubric to `knowledge/temporal-rubric.yaml` with its own version, so a scoring change does not bump the lens pack and mark indexes as mixed. It is validated against the pack's ids and signal codes on load, and the report header shows both versions.
- Score each process on reasons to use Temporal ("value paths"), each 0-10 with anchors:
  - **Durable pipeline:** expensive, long or many-step work, including LLM and agent pipelines and batch fan-out, whose loss on a crash is costly.
  - **Transaction:** writes across systems that need consistency or compensation.
  - **Long-lived wait:** waits on time, people or external events, entity state.
  - **Scheduled work:** cron, with overlap and backfill concerns.
- **Fit** is the best value path; the others are listed as secondary reasons.
- **Modifiers** are the existing reliability burden, separability, migration ease and the gates.
- **Starting formula:** overall = 0.6 fit + 0.2 burden + 0.1 separability + 0.1 ease, capped at the gates score + 2. The weights are calibrated on the known-answer set (8f).
- Durability anchors count the cost of redoing work (paid calls, compute, run time), not only business money.
- Map the 34 review checks no evaluation bar takes today: E-WFO, E-CXL and E-SAGA to failure handling, E-AC to idempotency, E-TMR to primitive idiom.

8b. **Process discovery and investigation.**
- **Discovery is deterministic first.** Candidates are the repository lens's processes plus files the lens marks as entry points or triggers that no process covers. At most 8 processes are investigated, by relevance; the rest are listed in the report as not investigated.
- **The investigator** is one tool-using call per process, at most 25 tool calls. Its tools are thin reads over the index and the mirror, implemented in the indexer package (no change to code_atlas):
  - file and folder summary with lens;
  - text search over summaries;
  - read source lines;
  - regex search across the mirrored source, which stands in for call-graph following without tree-sitter.
- **Process card**, where every fact carries `path@Lx-y`:
  - trigger;
  - the unit of work and what the count of units scales with (for example, "one LLM call per file of the target repository"), plus the scale assumption made;
  - ordered steps with their side effects (paid, external, irreversible);
  - where state lives between steps, and when results are persisted;
  - what a crash at each stage loses;
  - the reliability code that already exists;
  - waits and run length.
- **Storage:** cards go in a `process_cards` collection with the blob ids of the files they cite. A later report reuses a card if none of those files changed.

8c. **Card checks.**
- Code checks that every reference is an indexed path with lines in range.
- One verification call per card checks each fact against its cited lines, the same way leads are checked. Rejected facts are dropped.
- If the persistence point or the crash-loss fact is rejected, the card is marked low confidence and the report says so.

8d. **Per-process scoring and report.**
- Each process is scored from its card with rubric v2. The repository is "suitable" if at least one process scores 5 or more.
- Designs are written per suitable process; unsuitable processes get their reasons.
- % Temporalized comes from the suitable processes' designs.
- New report layout:
  - a verdict per process, with a bar;
  - a section per process with how it works today and what a crash loses (both with line references);
  - the value-path bars, the fit and the modifiers;
  - the scale assumption, stated next to the verdict;
  - the design, or why not.
- The evaluation of existing Temporal code stays file-based, apart from the new check mapping.

8e. **Slim the file lens.**
- Remove the consequence questions from the file prompt ("how failures and waits are handled", "what is lost or repeated if the process dies"). The narrative then describes only what the file itself does: its steps, its side effects and the reliability mechanisms present in it.
- Existing indexes are not refreshed, because the cards supersede those sentences.

8f. **Known-answer acceptance.** Each case runs twice, and the verdict for each process must match in both runs:
- **This repository:** the indexing sync process is suitable (5 or more) with a design, and the chat agent is not suitable.
- **delivery-matching:** no process is suitable.
- **phase3-check:** the lead follow-up process is suitable; the existing Temporal batch code is evaluated.

**Cost:** about $0.10–0.50 per investigated process, so roughly $1–4 per report, up from $0.05–0.30. Cards reused from an earlier report cost nothing. Indexing cost does not change.

**Not in Phase 8:**
- moving the process pass into indexing;
- an agent tool or UI for cards and reports;
- refreshing existing file lenses;
- BUILD.

**Progress (2026-10-16).** 8a–8e are done. 8f was run four times, each time on all three known-answer cases, twice each with fresh investigation (about $3–4 per round).

Calibration changes, each prompted by a round's results:
- **Round 1, hard threshold at 5.** All three known answers held. Processes scoring near 5 flipped (4.7→5.4, 4.3→5.0). delivery-matching had no process to assess and gave an empty report.
- **Round 2.**
  - Added a "borderline" band of ±0.75 around 5 (`margin` in the rubric). It is sized from the measured run-to-run differences of up to 0.7.
  - Borderline processes get a design, labelled borderline, with what would tip them either way.
  - Discovery falls back to up to 3 relevance-1 entry points when it finds nothing else.
  - Result: every verdict matched across runs. But the delivery-matching card stayed inside its bootstrap file and missed the verified gate.
- **Round 3.** The investigator was told to follow the process past the entry file. Cards were still built from 1–2 tool calls, and neighbouring verdicts flipped.
- **Round 4.** A minimum investigation depth was added: an answer before 8 tool calls is sent back once to read the code behind steps, persistence and crash loss. Cards then used 4–17 tool calls.
  - The delivery-matching card covers 7 files and applies G-READONLY.
  - The code-atlas indexing card found that `--rm` removes the temp-dir pickle.

Round 4 results:

| Case | Run 1 | Run 2 |
|---|---|---|
| code-atlas: indexing sync | 7.0 suitable | 7.1 suitable |
| code-atlas: report pipeline | 5.6 borderline | 4.9 borderline |
| code-atlas: chat agent | 3.7 not suitable | 4.3 borderline |
| code-atlas: folder repair | 4.4 borderline | 3.6 not suitable |
| code-atlas: source mirror | 3.6 not suitable | 3.7 not suitable |
| delivery-matching: request path | 1.0 not suitable | 0.9 not suitable |
| phase3: lead follow-up | 6.8 suitable | 6.2 suitable |
| phase3: LLM client | 5.7 borderline | 5.7 borderline |
| phase3: source mirroring | 3.3 not suitable | 3.8 not suitable |

**8f status: not passed as written.** The three known answers held, but in round 4 the chat agent and folder repair each moved between borderline and not suitable. No round, after the borderline band was added, moved a process between suitable and not suitable. Reports cost $0.16 (delivery-matching), $0.62–0.66 (phase3) and $1.27–1.40 (code-atlas).

**8f decision (2026-10-17): passed under an amended criterion.** The criterion is now: the known answers hold, and no process moves between suitable and not suitable across runs. Round 4 meets it. A move between borderline and a neighbouring verdict is accepted, because the borderline label already tells readers a verdict is close. Cached cards make repeated reports on an unchanged index identical. Not adopted: scoring each card three times and taking the median, and widening the band (that would be tuning to three repositories). **Phase 8 is done.**

**Decisions needed:**
1. **Calibration:** may I adjust the formula weights and anchors during 8f without coming back, provided the final values and the known-answer results are recorded here? (Recommended.)
2. **Investigator effort:** run the investigator at effort `medium`, through a new `REPORT_EFFORT` setting, while other report calls keep the indexing settings? (Recommended: investigation is where depth pays off.)
3. **Rubric file:** move the rubric out of the pack into its own versioned file? (Recommended.)

### Phase 9: the report in the index, in chat, and in the UI

**Status: approved 2026-10-17, with all three recommended decisions (report on by default with --no-report; credentials mounted read-write in the app; `gains` re-runs write-ups once).**

**Why.** The report is the system's best evidence about Temporal suitability: process cards with checked facts and line references, scores, verified gates and designs. Today it is a file nobody reads, while chat sees only the weaker file-level lens records. Phase 9 makes the report part of the index, and makes it available in chat and in the UI. Chat gets the Temporal Docs search as well, so follow-up questions about Temporal are grounded too.

**Work, in order (each step committed when done):**

9a. **The report runs as part of indexing.**
- When the lens is on, `sync` builds the report after the index is saved. `--no-report` skips it.
- Preflight also loads and validates the rubric, so a bad rubric stops the run before anything is spent.
- **Storage:** one `reports` document per repository holds:
  - the report data (the same JSON as `temporal-report.json`) and the Markdown;
  - the commit, pack and rubric versions it was built from;
  - when it was generated, its cost, and its status (`ok` or `failed`, with the error).
- **A failed report never fails the index.** The run warns, and the document records the failure.
- `report.sh` stays for on-demand runs and `--fresh`, and now also updates the stored report.
- `estimate` includes the report on a first index.

9b. **Stable, nearly free re-runs.** Everything that used a model call is cached, keyed on its inputs:
- **lead verdicts:** on the file's blob, the lead and the pack version;
- **process cards:** on their files' blobs (as today);
- **each process's scores and write-up:** on the card's content, the verified gates for the process, and the rubric, pack and model versions;
- **the evaluation write-up:** on its set of confirmed findings.

An unchanged repository then gives an identical report with no model calls. A changed file re-runs only the processes and leads that depend on it.

9c. **Chat reads the report.**
- **New tool**, `get_temporal_report(repo_id, view, process)`, returning compact text, not the 12 KB document:
  - `overview`: verdict table, % Temporalized, gates, generation date and commit, and whether the report is older than the index;
  - `process`: the card's facts with line references, the scores with reasons, and the design (with what it removes and what stays) or the reasons it isn't a fit;
  - `evaluation`: the bars and findings for existing Temporal code, with fixes.
- **SELECTED REPOSITORY block:** gains one line saying whether a report exists, its date, and its headline verdicts, so the model knows to use the tool.
- **System prompt guidance:**
  - Use the report first for questions about Temporal suitability or design.
  - Back Temporal claims with the report, pack ids or Docs search results, and say when going beyond them.
  - When a finding is challenged, open the cited source lines.

9d. **Temporal Docs search in chat, with a status light.**
- **Credentials:** `compose.yaml` mounts the Docs MCP credentials directory into the app container, read-write because the client writes back rotated tokens. The app reuses the indexer's Docs MCP client, which is in the same image.
- **New chat tool,** `search_temporal_docs(query)`, returning passages with their source URLs.
- **Health check:** `/api/health` reports `temporal_docs` as `connected`, `unavailable` (with the reason and the fix) or `not configured`. Connected means a token refresh and a real search returning at least one docs.temporal.io result. The result is cached for 10 minutes so health polling doesn't search continuously.
- **Status light in the sidebar,** labelled "Temporal Docs MCP":
  - green dot when connected;
  - red when unavailable, with the reason and `python3 scripts/mcp_login.py` in its tooltip;
  - grey when not configured.

  It's checked on page load and every 5 minutes, and clicking it re-checks.
- **When search is unavailable,** the tool says so, and the agent tells the user rather than falling back silently.

9e. **The report view.** A **Temporal report** button in the chat header opens the report for the conversation's index in the main pane. It's built from the report data (`/api/report/{repo_id}`), not the Markdown, and leads with opportunities and the benefit to the user:
1. **Header:** index, generated date and commit, Temporal status, versions, and a stale-report notice if the index is newer.
2. **Opportunities**, suitable first then borderline. One card per process, showing:
   - the score and verdict;
   - the strongest reason, as a feature: "Durable pipeline", "Long-lived waits", …;
   - **What you gain:** at most three short benefit statements;
   - the share of durable work it moves to Temporal.

   If there are none, a clear "No Temporal opportunities", with the main reasons.
3. **At a glance:** durable work on Temporal today and with the designs, the verified gates, and how many processes were assessed.
4. **Not a fit:** collapsed, with one line per process (score and main reason).
5. **Existing Temporal code,** if any: evaluation bars and the top findings with their fixes.
6. **Drill-down,** opened from each card:
   - how it runs today: card facts with line references;
   - what a crash loses;
   - the score breakdown: value paths, modifiers, overall;
   - the design: elements, what it removes, what stays;
   - its sources.
7. **Method and sources,** collapsed.

Each card has **Discuss in chat**, which fills in a question about that process in the composer.
- **To support "What you gain",** the design write-up gains a `gains` field: up to three statements of at most about 12 words, each citing a card fact or pack id. Code checks them like other claims.
- **Implementation:** plain JavaScript and CSS in the existing UI, light and dark mode, no build step.

**Acceptance:**
- **Unchanged repository:** re-indexing this repository unchanged gives identical report data (apart from date and cost), with report model cost under $0.02.
- **One changed file:** changing a file used by one process re-investigates only that process.
- **Chat:**
  - "Which parts of this repository should use Temporal, and why?" is answered from the stored report and cites it.
  - A follow-up about a Temporal feature uses Docs search and cites docs URLs.
- **Status light:** green when connected; red with the fix when the credentials are missing (tested by pointing at a bad credentials path).
- **Report view,** checked in Chromium with screenshots:
  - code-atlas: opportunities;
  - delivery-matching: none, with reasons;
  - phase3-check: opportunities plus existing Temporal code.

**Cost:**
- **Indexing:** a first index with the report adds about $0.15–0.30 per investigated process ($1–1.40 for this repository). Unchanged re-indexes add about nothing.
- **Chat Docs searches:** free.
- **Health check:** one search every 10 minutes while the app is in use.

**Not in Phase 9:**
- report history (only the latest report is kept);
- regenerating reports from chat;
- clicking line references to open source in the UI;
- building the designs.

**Decisions needed (my recommendation first):**
1. **Report by default:** run it on every index when the lens is on, with `--no-report` to skip? (Yes.)
2. **Credentials in the app container:** mount them read-write, so token rotation keeps working? (Yes; it's your personal token, on your machine.)
3. **`gains` field:** adding it changes the write-up, so the first report after Phase 9 re-runs every write-up once (about $0.05 per process). (Accept.)

**Done (2026-10-17).** Steps 9a–9e committed separately.

Acceptance:
- **Unchanged repository:** the phase3-check report re-ran at $0.00, reusing all 7 steps, with identical data apart from date and cost. ✓
- **One changed file:** a file used only by the LLM process was changed and committed. Only that process was investigated again ($0.16); the other processes kept their names, cards and assessments. ✓

  The first attempt failed this test. The repository lens renamed and regrouped processes on the incremental run, which invalidated every card. Two fixes made it pass:
  - the repository lens pass now gets the previous processes and the changed files;
  - cards are keyed on the entry point.
- **Chat:**
  - "Which parts of this repository should use Temporal, and why?" was answered from the stored report (one overview call), with verdicts, scores and gains. ✓
  - A follow-up about signals and durable timers searched the Docs MCP and cited the page it used, from Temporal's course material on GitHub, which the Docs MCP indexes. ✓
  - The first attempt searched the docs but cited nothing. The tool output and system prompt now require a link per passage used.
- **Status light:**
  - green from the live check;
  - "not configured" with a missing credentials path, and "unavailable" with a refused refresh token, each checked in the app container;
  - red in the UI. ✓

  This also found and fixed a race: a slow earlier health check could overwrite a newer result.
- **Report view:** checked in Chromium, light and dark, with screenshots, on all three indexes. ✓
  - code-atlas: 2 suitable, 1 borderline;
  - phase3-check: 1 suitable, 2 borderline, plus existing Temporal code;
  - delivery-matching: no opportunities, with the reason.

  Found and fixed during this: the chat input showed under the report (CSS overrode `hidden`); cards were too wordy (the scale assumption moved to the details); repeated gates are now grouped.

Costs: storing reports for the three indexes cost $1.49 (code-atlas), $0.30–0.63 (phase3-check) and $0.15 (delivery-matching). Most of that was investigating again under the new card keys, plus the one-time `gains` write-ups.

## Cost expectations (from calibration, Sonnet 5.5, effort low)

- Lens adds about $0.004–0.006 per file on top of the summary when combined (cached pack reads plus about 300 output tokens), so roughly $10 per 2,000 files.
  - Measured in Phase 3 (16-file mixed repository, full index with lens): $0.77 total, of which about $0.40 is a fixed first-wave cost. Parallel workers all write the ~11k-token system prompt to cache before any call can read it. This is negligible on large repositories and dominant on tiny ones. `estimate` models it.
- Folder lens prose adds about 60 output tokens per folder with rel >= 2 descendants.
- The repo pass is one call plus at most 6 MCP searches, about $0.10–0.30.

## Out of scope here

BUILD, and the Temporal-workflow conversion of the indexer itself. (Pack verification is complete: all 341 entries checked against the Docs MCP on 2026-10-16.)

## Resolved decisions (2026-10-16)

1. **Existing indexes:** a full re-index on first run after the upgrade is accepted. Incremental stays the default, and `--force` gives a full re-index on demand.
2. **Pack changes:** the lens is refreshed only on a pack version change, and only when the user asks (`--refresh-lens`). The pack version is stored with the index and shown in the UI.
