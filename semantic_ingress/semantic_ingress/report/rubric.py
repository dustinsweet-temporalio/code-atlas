"""
The report rubric (knowledge/temporal-rubric.yaml): loading, validation against
the knowledge pack, and the scoring rules that need no model.

Suitability: each process is scored on value paths (reasons to use Temporal) and
modifiers; fit is the best value path, and the overall score is the weighted fit
and modifiers, capped by the gates. Evaluation: each confirmed review finding
counts against one bar.
"""
import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from semantic_ingress.lens.pack import Pack


class RubricError(Exception):
    """The rubric is missing or invalid."""


@dataclass
class Rubric:
    path: Path
    data: dict[str, Any]
    hash: str

    @property
    def id(self) -> str:
        return self.data["meta"]["id"]

    @property
    def version(self) -> str:
        return str(self.data["meta"]["version"])

    @property
    def value_paths(self) -> list[dict]:
        return self.data["value_paths"]

    @property
    def modifiers(self) -> list[dict]:
        return self.data["modifiers"]

    @property
    def scored(self) -> list[dict]:
        """Every bar a process is scored on: value paths, then modifiers."""
        return self.value_paths + self.modifiers

    @property
    def suitable_at(self) -> float:
        return float(self.data["overall"]["suitable_at"])

    @property
    def gates_bar(self) -> str:
        return self.data["overall"]["cap"]["bar"]

    def titles(self) -> dict[str, str]:
        bars = self.scored + self.evaluate_bars + [self.data["evaluate"]["coverage"]]
        return {b["id"]: b["t"] for b in bars}

    # --- suitability ----------------------------------------------------------

    def overall(self, scores: dict[str, float]) -> dict:
        """Fit, overall, the weighted score before the cap, and whether the cap applied."""
        best = max(self.value_paths, key=lambda p: (scores.get(p["id"], 0), -self.value_paths.index(p)))
        fit = scores.get(best["id"], 0)
        weights = self.data["overall"]["weights"]
        weighted = sum(w * (fit if k == "fit" else scores.get(k, 0)) for k, w in weights.items()) / sum(weights.values())
        weighted = round(weighted, 1)
        cap = self.data["overall"]["cap"]
        limit = scores.get(cap["bar"], 10) + float(cap["plus"])
        overall = min(weighted, limit)
        margin = self.margin
        if overall >= self.suitable_at + margin:
            verdict = "suitable"
        elif overall <= self.suitable_at - margin:
            verdict = "not suitable"
        else:
            verdict = "borderline"
        return {"fit": fit, "best_path": best["id"], "weighted": weighted, "overall": overall,
                "gate_capped": limit < weighted, "verdict_class": verdict,
                "suitable": verdict == "suitable", "design": verdict != "not suitable"}

    @property
    def margin(self) -> float:
        return float(self.data["overall"].get("margin", 0))

    # --- evaluation -------------------------------------------------------------

    @property
    def evaluate_bars(self) -> list[dict]:
        return self.data["evaluate"]["bars"]

    @property
    def deductions(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.data["evaluate"]["deductions"].items()}

    def bar_for_check(self, check_id: str) -> str | None:
        """The evaluation bar a check counts against: an exact id first, then the first matching wildcard."""
        for bar in self.evaluate_bars:
            if check_id in bar["checks"]:
                return bar["id"]
        for bar in self.evaluate_bars:
            if any("*" in p and fnmatch.fnmatchcase(check_id, p) for p in bar["checks"]):
                return bar["id"]
        return None


def validate(data: dict, pack: Pack) -> list[str]:
    problems = []
    for key in ("id", "version"):
        if not (data.get("meta") or {}).get(key):
            problems.append(f"meta.{key} is missing")
    codes = pack.signal_codes
    gates = {i for i in pack.entries if i.startswith("G-")}
    for section in ("value_paths", "modifiers"):
        for bar in data.get(section) or []:
            bid = bar.get("id", "<no id>")
            for key in ("id", "t", "q", "anchors"):
                if not bar.get(key):
                    problems.append(f"{section}.{bid}: missing {key}")
            for code in bar.get("signals") or []:
                if code.endswith(".*"):
                    if not any(c.startswith(code[:-1]) for c in codes):
                        problems.append(f"{bid}: signal pattern {code!r} matches no pack signal")
                elif code not in codes:
                    problems.append(f"{bid}: unknown signal code {code!r}")
            for gate in bar.get("gates") or []:
                if gate not in gates:
                    problems.append(f"{bid}: unknown gate {gate!r}")
            if any(not 0 <= int(k) <= 10 for k in (bar.get("anchors") or {})):
                problems.append(f"{bid}: anchors must be keyed 0-10")
    if not data.get("value_paths"):
        problems.append("value_paths is empty")

    overall = data.get("overall") or {}
    modifier_ids = {m.get("id") for m in data.get("modifiers") or []}
    weights = overall.get("weights") or {}
    if "fit" not in weights:
        problems.append("overall.weights needs a 'fit' weight")
    for key in weights:
        if key != "fit" and key not in modifier_ids:
            problems.append(f"overall.weights: {key!r} is not a modifier")
    if (overall.get("cap") or {}).get("bar") not in modifier_ids:
        problems.append("overall.cap.bar must be a modifier")
    if "suitable_at" not in overall:
        problems.append("overall.suitable_at is missing")

    evaluate = data.get("evaluate") or {}
    checks = [i for i, e in pack.entries.items() if e.get("k") in ("check", "sdk") and pack.usable(e)]
    patterns = [p for bar in evaluate.get("bars") or [] for p in bar.get("checks") or []]
    for p in patterns:
        if not any(fnmatch.fnmatchcase(c, p) for c in checks):
            problems.append(f"evaluate: check pattern {p!r} matches no usable pack check")
    unmapped = [c for c in checks if not any(fnmatch.fnmatchcase(c, p) for p in patterns)]
    if unmapped:
        problems.append(f"evaluate: {len(unmapped)} checks map to no bar, e.g. {', '.join(unmapped[:5])}")
    if set((evaluate.get("deductions") or {})) != {"high", "medium", "low"}:
        problems.append("evaluate.deductions needs high, medium and low")
    return problems


def load_rubric(path: Path, pack: Pack) -> Rubric:
    if not path.exists():
        raise RubricError(f"report rubric not found at {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise RubricError(f"report rubric {path} is not valid YAML: {e}") from e
    problems = validate(data, pack)
    if problems:
        raise RubricError(f"report rubric {path} is invalid:\n  " + "\n  ".join(problems[:20]))
    return Rubric(path, data, hashlib.sha256(path.read_bytes()).hexdigest()[:16])
