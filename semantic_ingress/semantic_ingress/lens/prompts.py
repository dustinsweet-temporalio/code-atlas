"""
Prompts for the Temporal lens.

File calls are combined: one call returns the semantic summary (with today's
summary instructions, unchanged) and the lens record. Everything except the file
itself goes in the system prompt, so the instructions and the pack view are read
from cache on every call after the first.
"""
from semantic_ingress.prompts import FILE_SUMMARY_PROMPT

# The file-summary instructions without the trailing file placeholders.
_SUMMARY_INSTRUCTIONS = (
    FILE_SUMMARY_PROMPT.split("FILE PATH: {file_path}")[0].replace("{{", "{").replace("}}", "}").strip()
)

FILE_LENS_INSTRUCTIONS = """The lens record is stored in a code index and read later by agents that assess whether a codebase should use Temporal (durable execution), or that review existing Temporal code. Records must be short, accurate, and grounded only in what this file shows. Omit rather than guess. Do not describe how other files use this one.

Fields:

rel (0-3): durable-execution relevance of this file.
  0 none: no side effects, orchestration, waits, reliability logic, or Temporal code (types, constants, markup, pure utilities, config).
  1 incidental: touches a signal (e.g. reads env, one read-only query) but implements no step of failure-sensitive or multi-step work.
  2 contributes: implements a step a durable process would rely on: an external call or write, a wait, a retry/checkpoint/compensation mechanism, a trigger.
  3 core: orchestrates multi-step work, owns process state, waits or compensation; or is Temporal workflow/worker code.

cov: "full", or "partial" if the content is truncated, generated or minified.

kind: one or two of ORCH (sequences steps), ACT (performs side effects), PURE (deterministic logic), ENTRY (trigger: handler, consumer, CLI, scheduler), STATE (persists or holds process state), NONE; for existing Temporal code use TWF (workflow definition, or options/constants used by workflow code), TACT (activity definition/implementation), TWORKER (worker setup), TCLIENT (starts or messages workflows).

sig: signals present in this file, only codes from SIGNALS. Optional ':detail' of at most 3 words (e.g. effect.http:stripe). When rel >= 2 every signal carries a line anchor of its strongest evidence, e.g. effect.http:stripe@L40-62. At most 8, strongest first. Empty when rel is 0. Emit hazard.* only when kind includes ORCH or TWF (hazards matter only in orchestration or workflow code).

sym: only when rel >= 2: up to 6 public symbols, each classified ORCH, ACT or PURE.

c: up to 5 CONCEPT ids whose statement this file's own code directly evidences. Non-Temporal code: gates (G-), values (V-), patterns (P-), conversion signals (X-). Temporal code: patterns, limits (L-) and checks (E-, S-). Empty when rel is 0.

f: only for existing Temporal code: up to 6 check ids (E-, S-) the code violates or is clearly at risk of, each listed once with one line anchor, e.g. E-AO-STC@L88. Cite an id only when the code matches that entry's statement, not just its title. Never list checks the code satisfies. Empty otherwise.

n: narrative, plain prose, no lists.
  rel 0: exactly "No durable-execution relevance: " plus at most 8 words naming what the file is.
  rel 1: at most 50 words.
  rel 2-3: at most 110 words: what starts the work in this file, its steps and side effects, and the reliability mechanisms present in this file (retries, checkpoints, locks, waits).
  Describe only what this file's code does. Do not judge what a crash would lose or how the wider process behaves: that depends on other files and is established separately. Do not recommend, and do not make claims about Temporal features (concept ids carry that). For existing Temporal code, describe what the workflow/activity does and its most important risk.

Use only ids and codes from the lists below."""


def file_system_prompt(pack_view: str, lens_only: bool = False) -> str:
    """System prompt for a combined (or lens-only) file call in one mode/language."""
    if lens_only:
        head = (
            "You write the Temporal lens record for one source file. "
            "Return a JSON object with a single field `lens`.\n\n"
        )
        return head + "## Lens record\n\n" + FILE_LENS_INSTRUCTIONS + "\n\n" + pack_view
    head = (
        "You produce two things for one source file and return them as one JSON object: "
        "`summary` (the semantic summary) and `lens` (the Temporal lens record).\n\n"
    )
    return (
        head
        + "## Semantic summary (the `summary` field)\n\n" + _SUMMARY_INSTRUCTIONS
        + "\n\n## Lens record (the `lens` field)\n\n" + FILE_LENS_INSTRUCTIONS
        + "\n\n" + pack_view
    )


def file_user_prompt(file_path: str, content: str, cap_reminder: str = "") -> str:
    """The per-file message: the path and the numbered content (line anchors need numbers)."""
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(content.splitlines(), 1))
    reminder = f"\n\n{cap_reminder}" if cap_reminder else ""
    return f"FILE PATH: {file_path}{reminder}\n\nFILE CONTENT:\n{numbered}"


FOLDER_LENS_SECTION = """
---

TEMPORAL LENS (the `lens_n` field)

Below are the lens records of this folder's direct child files (one compact line
each: relevance, kind, signals, concept ids, narrative) and the lens roll-ups of
its child folders. Write `lens_n`: at most 40 words, plain prose, describing the
durable-execution picture of this folder's whole subtree: where multi-step or
failure-sensitive work happens, what triggers it, and which reliability
mechanisms the records show. Describe; do not judge what a crash would lose;
do not recommend; do not make claims about Temporal.

CHILD FILE LENS RECORDS:
{file_lenses}

CHILD FOLDER LENS ROLL-UPS:
{folder_lenses}"""


REPO_LENS_SYSTEM = """You write the repository-level Temporal lens for a code index. Readers use it to judge whether the repository should use Temporal (durable execution), or how well it already does. Ground every statement in the inputs: the detection facts, the lens roll-up, the file lens records, the pack entries below, and results of the `search_temporal_docs` tool. Do not use general knowledge of Temporal that you cannot cite.

Fields:

status: "full" if the durable processes run on a Temporal SDK; "partial" if some do and others use other mechanisms; "adjacent" if another orchestrator or job system (Airflow, Celery, Step Functions, ...) is used and no Temporal SDK; "none" otherwise.

archetype: the dominant kind of repository.

detected: the detection facts as given (Temporal SDK and adjacent technologies with evidence paths). Do not add technologies the facts do not show.

processes: up to 8 business processes worth assessing: multi-step, failure-sensitive, waiting, or long-running work. Each names its entry-point path, the main files (from the records), signal codes, pack concept ids, and a narrative of at most 40 words. Only paths from the records.
If PREVIOUS PROCESSES are given (from the last index run), keep each one's name, entry point and grouping exactly, unless the CHANGED FILES alter that process; update its files, signals, concepts and narrative only where the changes call for it. Drop a previous process only if its entry point no longer exists, and add a new one only for work that appears in the changed files.

anti: gate ids (G-) that apply, each with the evidence paths (from the records) that show it.

n: at most 120 words: what the repository does in durable-execution terms, where its multi-step work is, how failures and waits are handled today, and the Temporal status. Describe; do not recommend.

cites: the pack ids and docs URLs your statements rely on, each with what it supports (at most 15 words). Only pack ids from the list below, and only URLs that `search_temporal_docs` returned in this conversation.
{search_instructions}
Return the final JSON when done."""

REPO_SEARCH_INSTRUCTIONS = """
Docs search is required. Before answering, use `search_temporal_docs` once for each of your most important processes (at most {max_searches} searches in total): ask the docs about the Temporal pattern that process's concept ids point to (for example the saga pattern, entity workflows, schedules, or activity retries), phrased as a complete question. For each process, cite the most relevant docs.temporal.io URL the search returned, saying which process and pattern it supports."""

REPO_NO_SEARCH_INSTRUCTIONS = """
No docs search is available in this run; cite pack ids only."""


def repo_user_prompt(tier0: str, rollup: str, records: str, omitted: int,
                     previous: list[dict] | None = None, changed: list[str] | None = None) -> str:
    more = f"\n({omitted} lower-relevance records omitted for length.)" if omitted else ""
    prompt = (
        f"DETECTION FACTS (deterministic scan):\n{tier0}\n\n"
        f"ROOT LENS ROLL-UP:\n{rollup}\n\n"
        f"FILE LENS RECORDS (path: compact record; relevance 3 first, then 2, then 1):\n{records}{more}"
    )
    if previous:
        rows = "\n".join(f"- {p['name']} | entry {p['entry']} | files {', '.join(p['files'])}" for p in previous)
        prompt += f"\n\nPREVIOUS PROCESSES (keep stable):\n{rows}\n\nCHANGED FILES:\n" + ("\n".join(changed or []) or "(none)")
    return prompt
