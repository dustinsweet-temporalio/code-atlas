"""
A sync run's progress as the web UI shows it: one line saying what the run is doing
and an overall percentage, from the workflow's `progress` query.

Generation (file summaries, lens refresh, folder summaries) takes most of the bar,
weighted by item counts; a folder counts double, because its call is bigger. The
phases after it get fixed bands. The Temporal report, when it runs, gets a wide band
near the end because it takes minutes and reports no counts of its own. Folders are
only counted once the folder phase starts, so before then their number is estimated
and the percentage can step a little when the real count arrives.
"""
from math import ceil

from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.temporal.models import Progress, UsageTotals

PLAN_END = 3          # percent reached once the plan is known
FOLDER_WEIGHT = 2

# Where each phase after generation starts on the bar, with and without the report.
_BANDS = {
    True: {"REPO": 70, "DELETE": 75, "PERSIST": 76, "FINALIZE": 80, "MIRROR": 82, "REPORT": 85,
           "DISK_OUTPUT": 98, "CLEANUP": 99, "DONE": 100},
    False: {"REPO": 85, "DELETE": 90, "PERSIST": 91, "FINALIZE": 95, "MIRROR": 96, "REPORT": 99,
            "DISK_OUTPUT": 99, "CLEANUP": 99, "DONE": 100},
}
_NEXT = {"REPO": "DELETE", "DELETE": "PERSIST", "PERSIST": "FINALIZE", "FINALIZE": "MIRROR",
         "MIRROR": "REPORT", "REPORT": "DISK_OUTPUT", "DISK_OUTPUT": "CLEANUP", "CLEANUP": "DONE"}

_FIXED = {
    "STARTING": "Starting",
    "PLAN": "Comparing the repository with the index",
    "DELETE": "Removing deleted files from the index",
    "FINALIZE": "Building the search indexes",
    "MIRROR": "Copying the source files",
    "REPORT": "Building the Temporal report (this takes a few minutes)",
    "DISK_OUTPUT": "Writing files to disk",
    "CLEANUP": "Cleaning up",
    "DONE": "Done",
}


def usage_cost(usage: UsageTotals) -> float:
    """Estimated cost at the configured prices (see llm_client.set_pricing)."""
    return LLMUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    ).total_cost_usd


def _folder_estimate(p: Progress) -> int:
    plan = p.plan
    if plan.levels:
        return sum(level.count for level in plan.levels)
    return ceil(plan.n_files / 4)


def percent(p: Progress, report: bool) -> int:
    """Overall position on the bar, 0 to 100. `report`: the run builds the Temporal report."""
    bands = _BANDS[report]
    if p.phase == "STARTING" or p.plan is None:
        return 1 if p.phase == "PLAN" else 0
    if p.phase in ("FILES", "LENS_REFRESH", "FOLDERS"):
        plan = p.plan
        folders = (p.total if p.phase == "FOLDERS" else _folder_estimate(p)) * FOLDER_WEIGHT
        total = plan.n_files + plan.n_lens_only + folders
        if p.phase == "FILES":
            done = p.done
        elif p.phase == "LENS_REFRESH":
            done = plan.n_files + p.done
        else:
            done = plan.n_files + plan.n_lens_only + p.done * FOLDER_WEIGHT
        share = done / total if total else 1.0
        return round(PLAN_END + (bands["REPO"] - PLAN_END) * min(share, 1.0))
    start = bands.get(p.phase)
    if start is None:
        return 0
    if p.phase == "PERSIST":
        # Staged documents: the generated summaries plus the repository and dir-list documents.
        expected = p.plan.n_files + _folder_estimate(p) + 2
        share = min(p.done / expected, 1.0) if expected else 1.0
        return round(start + (bands[_NEXT["PERSIST"]] - start) * share)
    return start


def describe(p: Progress, lens: bool = True) -> str:
    """One line: what the run is doing now, with counts."""
    if p.phase == "FILES":
        what = "changed files" if p.plan is not None and p.plan.mode == "incremental" else "files"
        text = f"Summarising {what}: {p.done:,} of {p.total:,}"
    elif p.phase == "LENS_REFRESH":
        text = f"Refreshing the Temporal lens: {p.done:,} of {p.total:,} files"
    elif p.phase == "FOLDERS":
        text = f"Summarising folders: {p.done:,} of {p.total:,}"
    elif p.phase == "REPO":
        text = "Writing the repository summary" + (" and its Temporal lens" if lens else "")
    elif p.phase == "PERSIST":
        text = f"Writing to the index: {p.done:,} documents"
    else:
        text = _FIXED.get(p.phase, p.phase.replace("_", " ").capitalize())
    if p.failed:
        text += f" · {p.failed:,} failed"
    return text
