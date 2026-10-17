"""
Folder lens roll-ups (deterministic) and their compact serving form.

A folder's roll-up is the sum of its direct files' lens records and its child
folders' roll-ups, so every roll-up covers the folder's whole subtree, the same
way folder summaries do.
"""
import re
from collections import Counter

from semantic_ingress.lens.models import TEMPORAL_KINDS, CountEntry, FileLens, FolderLensRollup

MAX_CORE_FILES = 25
_CODE = re.compile(r"[:@]")


def _counts(counter: Counter) -> list[CountEntry]:
    return [CountEntry(key=k, files=n) for k, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]


def compute_rollup(
    direct_files: dict[str, FileLens],
    child_rollups: list[FolderLensRollup],
    pack_version: str,
) -> FolderLensRollup:
    """Roll up direct file lens records (path -> record) and child folder roll-ups."""
    rel = [0, 0, 0, 0]
    kinds: Counter = Counter()
    signals: Counter = Counter()
    concepts: Counter = Counter()
    core: list[str] = []
    temporal = 0

    for path, lens in sorted(direct_files.items()):
        rel[lens.rel] += 1
        kinds.update(set(lens.kind))
        signals.update({_CODE.split(s, 1)[0] for s in lens.sig})
        concepts.update(set(lens.c))
        if lens.rel == 3:
            core.append(path)
        if set(lens.kind) & TEMPORAL_KINDS:
            temporal += 1

    for child in child_rollups:
        rel = [a + b for a, b in zip(rel, child.rel_counts)]
        kinds.update({e.key: e.files for e in child.kind_counts})
        signals.update({e.key: e.files for e in child.signal_counts})
        concepts.update({e.key: e.files for e in child.concept_counts})
        core.extend(child.core_files)
        temporal += child.temporal_files

    return FolderLensRollup(
        files=sum(rel),
        rel_counts=rel,
        kind_counts=_counts(kinds),
        signal_counts=_counts(signals),
        concept_counts=_counts(concepts),
        core_files=sorted(core)[:MAX_CORE_FILES],
        temporal_files=temporal,
        pack_version=pack_version,
    )


def max_rel(rollup: FolderLensRollup) -> int:
    return max((r for r in range(4) if rollup.rel_counts[r]), default=0)


def compact_rollup(rollup: FolderLensRollup, top: int = 8) -> str:
    """One-line serving form, e.g. 'files 42 rel3:3 rel2:9 rel1:12 rel0:18 | kind ... | sig ... | c ... | core ... | n'."""
    r = rollup.rel_counts
    parts = [f"files {rollup.files} rel3:{r[3]} rel2:{r[2]} rel1:{r[1]} rel0:{r[0]}"]
    if rollup.temporal_files:
        parts[0] += f" temporal:{rollup.temporal_files}"
    for label, entries in (("kind", rollup.kind_counts), ("sig", rollup.signal_counts), ("c", rollup.concept_counts)):
        if entries:
            parts.append(f"{label} " + " ".join(f"{e.key}:{e.files}" for e in entries[:top]))
    if rollup.core_files:
        parts.append("core " + " ".join(rollup.core_files[:10]))
    if rollup.n:
        parts.append(rollup.n)
    return " | ".join(parts)
