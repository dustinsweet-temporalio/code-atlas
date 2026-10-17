"""
Prompts for the report.

Every prompt confines the model to the inputs (index records, pack entries, docs
search results). Code checks the answers afterwards (see grounding.py), so these
instructions are the first line of grounding, not the only one.
"""

GROUNDING = (
    "Ground every statement in the inputs and the pack entries below. Do not use general "
    "knowledge of Temporal that you cannot cite. Cite with pack ids, or with docs URLs that "
    "`search_temporal_docs` returned in this conversation or that are listed with a pack entry."
)

VERIFY_SYSTEM = """You verify leads that an automated code index produced. A lead claims that a pack entry (a gate, or a review check for Temporal code) applies to a file. Decide each lead from the entry's statement and the numbered source lines only; use no other knowledge.

verdict:
  confirmed: the code clearly matches the entry's statement (not just its title). Give the line anchor of the deciding evidence, e.g. L88 or L80-95.
  rejected: the code does not match the statement, or the lead's line shows something else.
  unclear: the decision depends on code that is not shown.

severity, for confirmed review checks (ids starting E- or S-); n/a otherwise:
  high: can lose or corrupt data, cause non-determinism errors on replay, or leave executions stuck or failing with no recovery.
  medium: causes avoidable failures, retries, cost or operational risk.
  low: idiom, maintainability or hygiene.

reason: one sentence of at most 30 words naming what the code does.

Return exactly one verdict per lead, with the lead key exactly as given."""


def verify_user_prompt(path: str, leads_text: str, source_text: str) -> str:
    return f"FILE PATH: {path}\n\nLEADS (key | entry | title | statement):\n{leads_text}\n\nSOURCE:\n{source_text}"


SCORE_SYSTEM = """You score one process's suitability for Temporal (durable execution) on fixed rubric bars, for a report read by engineers. Use only the inputs: the process card (facts established from the source, numbered F1, F2, ...), the file lens records, the verified gates, the index's signal hits, and the pack entries. Do not use general knowledge of Temporal.

Score every value path and every modifier exactly once. Choose each score from the bar's anchors; use a value between two anchors only when the evidence sits between them. A process needs only one strong value path; score each path on its own, without regard to the others. Base the pipeline path on the card's unit of work, scale assumption and crash-loss facts.

For each bar give a reason of at most 20 words, the evidence (card fact ids, or indexed paths), the signal codes, and the pack ids the reason relies on. A score above 0 needs evidence. The fit-clearance bar reflects only gates verified for this process."""


WRITEUP_SYSTEM = """You write one process's part of a Temporal suitability report. Readers are engineers deciding whether to adopt Temporal; be concise, concrete and accurate. The process card holds facts established from the source; rely on it for how the process works today. {grounding}

{task}

Return the final JSON when done."""

DESIGN_TASK = """This process is {label} (overall {overall}/10). Design how it would move to Temporal.
- designs: exactly one, with the process name exactly as given. Its elements are the workflows, activities, child workflows, signals, queries, updates, timers and schedules. Name each after the code it comes from, list the indexed files it is built from, say what it does (at most 20 words) and cite.
- Activities wrap side effects (the card's steps with effects; records of kind ACT); workflows hold the orchestration (kind ORCH). Workflow code must stay deterministic.
- removes: hand-built reliability code the design makes unnecessary (retries, checkpoints, status flags, polling, locks, failure collection), each as 'path: what'.
- stays: what remains outside Temporal and why.
- why: what goes wrong today when the process fails or waits (use the card's crash-loss facts), and how the design addresses it. This is the reason to use Temporal.
- gains: up to 3 concrete benefits for the team, each at most about 12 words in plain terms (for example "A crashed 2,000-file index resumes instead of re-paying every call"). Each cites the card fact ids (F1, F2, ...) or pack ids it rests on.
- verdict: at most 80 words: the recommendation, the main reasons, and the scale assumption it depends on. If the process is borderline, say so and say what would tip it either way.
- reasons and would_change stay empty.
Docs search is required: before answering, use `search_temporal_docs` for the pattern the design uses (at most {max_searches} searches), phrased as a complete question, and cite the most relevant docs.temporal.io URL it returned."""

UNSUITABLE_TASK = """This process is not a good fit for Temporal (overall {overall}/10{gate_note}). Explain why; do not design.
- reasons: up to 4, each at most 30 words, with the indexed paths that show it and citations. Verified gates and the weakest bars first.
- would_change: up to 3 changes to the process or its requirements that would make Temporal worthwhile, each at most 20 words.
- verdict: at most 60 words: the recommendation and the main reasons.
- designs stays empty.
You may use `search_temporal_docs` (at most {max_searches} searches) to find docs to cite."""


EVAL_SYSTEM = """You write the evaluation section of a report on existing Temporal code, for the engineers who own it. Be concise, concrete and accurate. {grounding}

- remediations: one per confirmed finding, with the finding key exactly as given: the fix in at most 30 words, specific to the file, citing the finding's pack id and, where useful, a docs URL.
- verdict: at most 80 words: the overall state of the Temporal code, the most serious risks, and what to fix first. If there are no confirmed findings, say what the review covered.
You may use `search_temporal_docs` (at most {max_searches} searches) for details of a fix.

Return the final JSON when done."""


INVESTIGATE_SYSTEM = """You investigate one process in a codebase and record how it runs today, for engineers assessing whether its work survives failures. You have tools over a code index (summaries of files and folders, which are guides only) and over the source. Use the summaries to navigate; read the source to establish facts.

The process is the work the entry point starts, not the entry file. Follow it from the entry point through the code it calls (handlers, services, repositories, clients, launch scripts), using grep_source to find definitions and callers, until the facts below are established. The files the index associates with the process are a starting point, not a boundary; list every file you relied on in `files`.

Every fact cites the source lines that show it, as path@Lstart-end (for example src/sync.py@L120-148). Cite only lines you have read with read_source or seen in grep_source results. If something cannot be established from the code, write "Not established from the code." with no refs. Never guess.

Establish:
- trigger: what starts the process.
- unit_of_work: what one unit of work is (for example "one LLM call per source file"). scales_with: what the number of units per run depends on. scale_assumption: a typical run, stated as an assumption (for example "Assumes a 2,000-file repository: about 2,000 LLM calls").
- steps: the steps in order. effects: paid_call (LLM or metered API), external_call, db_write, file_write, message, irreversible.
- state_between_steps: where intermediate results live (memory, files, database).
- persistence: when and where results become durable. Trace it to the code that writes them.
- crash_loss: for each stage, what a crash at that stage loses or forces to repeat, given where state lives and when it is persisted. Count a cache or resume file only if the code reads it back on a restart, and check where it is written: for example, the temporary directory of a container that is removed after the run does not survive. Launch scripts and container files are in the index too.
- reliability: retries, checkpoints, resume, idempotency, locks, timeouts and failure handling present in the code.
- waits: waits on time, people or external events (empty if none).
- run_length: how long a run takes, or what drives its length.
- files: the indexed files that make up the process.

Describe the code only. Do not assess or mention Temporal or any technology to adopt. You have at most {max_calls} tool calls; read the parts that decide the facts above."""


INVESTIGATE_NUDGE = """You answered after {calls} tool calls. That is not enough to establish these facts from the source. Before answering again:
- follow the process from the entry point into the code it calls, past the entry file;
- read the code that makes each step's side effects;
- read the code that writes the results (persistence) and anything that reads state back on a restart (crash loss);
- check how the process is launched (scripts, container settings) if that decides where state survives.
Then give the full answer again; cite only lines you have now seen."""


def investigate_user_prompt(name: str, entry: str, files: list[str], narrative: str, records: str) -> str:
    return (
        f"PROCESS: {name}\nENTRY POINT: {entry}\nFILES THE INDEX ASSOCIATES WITH IT: {', '.join(files)}\n"
        f"INDEX NARRATIVE (a lead, not a fact): {narrative}\n\n"
        f"LENS RECORDS OF RELEVANT FILES (path: record; guides only):\n{records}"
    )


VERIFY_CARD_SYSTEM = """You check facts about how a process runs against the source lines they cite. Use only the cited lines; use no other knowledge.

verdict:
  supported: the cited lines show the fact (for a fact about what a crash loses, the lines show where the state lives and when it is persisted, and the conclusion follows).
  unsupported: the lines contradict the fact or do not show it.
  unclear: the lines show part of it and the rest depends on code that is not cited.

Return exactly one verdict per fact, with the fact id exactly as given, and a reason of at most 25 words."""


RETRY_NOTE = """

YOUR PREVIOUS ANSWER:
{previous}

It failed these checks:
{problems}

Return the corrected answer. Fix only these problems and keep everything else. No more docs searches are available; the URLs you may cite are the pack entry URLs and these returned ones:
{urls}"""
