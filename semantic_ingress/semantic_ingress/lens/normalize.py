"""
Normalize a file lens record written by the index model.

Calibration (2026-10-16) showed the model occasionally overshoots caps, invents an
id, repeats a finding, tags hazards on plain adapters, or leaves a signal without
a line anchor. These are fixed here in code rather than by retrying; only a
narrative far over its cap asks for one retry.
"""
import re
from dataclasses import dataclass, field

from semantic_ingress.lens.models import TEMPORAL_KINDS, FileLens

REL0_PREFIX = "No durable-execution relevance: "
NARRATIVE_CAPS = {1: 50, 2: 110, 3: 110}  # words
REL0_MAX_WORDS = 8  # after the prefix
RETRY_OVERSHOOT = 1.2  # retry once when a narrative is more than 20% over its cap
MAX_SIG, MAX_SYM, MAX_C, MAX_F = 8, 6, 5, 6
ANCHOR = re.compile(r"@L\d+(-\d+)?$")


@dataclass
class NormalizeResult:
    lens: FileLens
    problems: list[str] = field(default_factory=list)  # what was corrected (for logs)
    retry: bool = False  # narrative far over its cap: ask the model once more


def _code(signal: str) -> str:
    return re.split(r"[:@]", signal, maxsplit=1)[0]


def _dedupe(items: list[str], key=lambda x: x) -> list[str]:
    seen, out = set(), []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


def _words(text: str) -> int:
    return len(text.split())


def normalize(lens: FileLens, signal_codes: set[str], entry_ids: set[str]) -> NormalizeResult:
    """
    Apply the lens rules.

    Args:
        lens: record as written by the model
        signal_codes: signal codes allowed in this file's mode
        entry_ids: pack ids allowed in this file's mode (and language)
    """
    problems: list[str] = []
    data = lens.model_dump()

    # kind: one or two roles
    kind = _dedupe(data["kind"]) or ["NONE"]
    if len(kind) > 2:
        problems.append(f"kind trimmed from {len(kind)}")
        kind = kind[:2]
    data["kind"] = kind
    temporal = bool(set(kind) & TEMPORAL_KINDS)

    # rel is pinned by kind for existing Temporal code, for consistency across runs
    if temporal:
        if {"TWF", "TWORKER"} & set(kind) or ("TACT" in kind and "ORCH" in kind):
            pinned = 3
        elif "TACT" in kind:
            pinned = 2
        else:  # TCLIENT
            pinned = max(2, data["rel"])
        if pinned != data["rel"]:
            problems.append(f"rel {data['rel']} pinned to {pinned} for {'+'.join(kind)}")
            data["rel"] = pinned
    rel = data["rel"]

    if rel == 0:
        for key in ("sig", "sym", "c", "f"):
            if data[key]:
                problems.append(f"{key} cleared at rel 0")
                data[key] = []
        n = data["n"].strip()
        tail = n[len(REL0_PREFIX):] if n.startswith(REL0_PREFIX) else n
        tail_words = tail.split()
        if not n.startswith(REL0_PREFIX) or len(tail_words) > REL0_MAX_WORDS:
            problems.append("rel 0 narrative reformatted")
        data["n"] = REL0_PREFIX + " ".join(tail_words[:REL0_MAX_WORDS]).rstrip(".")
        return NormalizeResult(FileLens.model_validate(data), problems)

    # signals: known codes only, hazards only in orchestration/workflow code,
    # anchors required at rel >= 2
    sig = []
    for s in _dedupe(data["sig"]):
        code = _code(s)
        if code not in signal_codes:
            problems.append(f"unknown signal {code}")
            continue
        if code.startswith("hazard.") and not {"ORCH", "TWF"} & set(kind):
            problems.append(f"hazard {code} dropped ({'+'.join(kind)})")
            continue
        if rel >= 2 and not ANCHOR.search(s):
            problems.append(f"signal without anchor dropped: {s}")
            continue
        sig.append(s)
    if len(sig) > MAX_SIG:
        problems.append(f"sig trimmed from {len(sig)}")
    data["sig"] = sig[:MAX_SIG]

    # symbols only at rel >= 2
    if rel < 2 and data["sym"]:
        problems.append("sym cleared at rel < 2")
        data["sym"] = []
    data["sym"] = data["sym"][:MAX_SYM]

    # concepts: known ids only
    c = []
    for cid in _dedupe(data["c"]):
        if cid in entry_ids:
            c.append(cid)
        else:
            problems.append(f"unknown concept {cid}")
    data["c"] = c[:MAX_C]

    # findings: Temporal code only, review-check ids only, one per id
    f = []
    if data["f"] and not temporal:
        problems.append("findings cleared on non-Temporal file")
    elif data["f"]:
        for lead in _dedupe(data["f"], key=lambda x: x.split("@", 1)[0]):
            base = lead.split("@", 1)[0]
            if base in entry_ids and base.startswith(("E-", "S-")):
                f.append(lead)
            else:
                problems.append(f"unknown finding {base}")
        if len(data["f"]) != len(f):
            problems.append(f"findings {len(data['f'])} -> {len(f)}")
    data["f"] = f[:MAX_F]

    # narrative cap: accept small overshoots, retry once when far over
    cap = NARRATIVE_CAPS[rel]
    words = _words(data["n"])
    retry = words > cap * RETRY_OVERSHOOT
    if words > cap:
        problems.append(f"narrative {words} words (cap {cap})")

    return NormalizeResult(FileLens.model_validate(data), problems, retry)
