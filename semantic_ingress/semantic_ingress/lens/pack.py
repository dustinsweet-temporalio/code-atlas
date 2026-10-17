"""
Temporal knowledge pack: loading, validation, and the prompt views the lens uses.

The pack (knowledge/temporal-pack.yaml) is the only source of Temporal knowledge
for the lens. Two views are built from it and sent as a cached system prompt:

- conversion view: for files that do not use a Temporal SDK (gates, values,
  patterns and conversion signals, with statements; limit titles)
- evaluation view: for files that use a Temporal SDK (patterns, limits and review
  checks with statements, plus the SDK entries for the file's language)

Entries whose `v` is `conflict` or `stale` contradict the docs and are excluded.
Views are built deterministically so the cached prefix is byte-identical per run.
"""
import hashlib
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ENTRY_SECTIONS = ["gates", "values", "patterns", "limits", "conversion", "checks", "sdk_specific"]
ALL_SECTIONS = ENTRY_SECTIONS + ["questions"]
EXCLUDED_STATUSES = {"conflict", "stale"}
VALID_STATUSES = {"verified", "pending", "guide-only", "conflict", "stale"}
VALID_MODES = {"conv", "eval", "both"}

CONV_FULL = ["gates", "values", "patterns", "conversion"]
CONV_TITLES = ["limits"]
EVAL_FULL = ["patterns", "limits", "checks"]


class PackError(Exception):
    """The pack is missing or invalid."""


@dataclass
class Pack:
    """A loaded, validated knowledge pack."""

    path: Path
    data: dict[str, Any]
    hash: str
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.data["meta"]["id"]

    @property
    def version(self) -> str:
        return str(self.data["meta"]["version"])

    @cached_property
    def signal_codes(self) -> set[str]:
        return {code for family in self.data["signals"].values() for code in family}

    @property
    def detection(self) -> dict[str, Any]:
        return self.data["detection"]

    @cached_property
    def _temporal_patterns(self) -> dict[str, list[re.Pattern]]:
        return {
            lang: [re.compile(p) for p in patterns]
            for lang, patterns in self.detection["temporal_sdk"].items()
        }

    @cached_property
    def _import_lines(self) -> dict[str, re.Pattern]:
        return {lang: re.compile(p) for lang, p in self.detection["import_lines"].items()}

    def usable(self, entry: dict[str, Any]) -> bool:
        return entry.get("v") not in EXCLUDED_STATUSES

    def language_of(self, path: str) -> str | None:
        """Language of a file from its extension, or None for non-code files."""
        return self.detection["languages"].get(PurePosixPath(path).suffix.lower())

    def file_mode(self, path: str, content: str) -> str:
        """
        'eval' if this is code in a language whose Temporal SDK markers match the
        content, else 'conv'. Non-code files (docs, manifests) are always 'conv'.
        """
        lang = self.language_of(path)
        patterns = self._temporal_patterns.get(lang or "", [])
        import_line = self._import_lines.get(lang or "")
        if not patterns or import_line is None:
            return "conv"
        for line in content.splitlines():
            if import_line.search(line) and any(p.search(line) for p in patterns):
                return "eval"
        return "conv"

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def view(self, mode: str, lang: str | None) -> tuple[str, set[str], set[str]]:
        """
        The prompt text for a mode (and language, for evaluation mode), plus the
        signal codes and entry ids that a lens record in that mode may use.
        """
        if mode not in ("conv", "eval"):
            raise ValueError(f"unknown lens mode {mode!r}")

        codes: list[str] = []
        lines = ["SIGNALS (closed vocabulary; code | meaning)"]
        for family in self.data["signals"].values():
            for code, meaning in family.items():
                if mode == "conv" and code.startswith("tmp."):
                    continue
                codes.append(code)
                lines.append(f"{code} | {meaning}")

        ids: list[str] = []
        lines += ["", "CONCEPTS (id | title | statement)"]
        full = CONV_FULL if mode == "conv" else EVAL_FULL
        titles_only = CONV_TITLES if mode == "conv" else []
        for section in full + titles_only:
            for entry in self.data.get(section) or []:
                if not self.usable(entry) or entry.get("mode") not in ("both", mode):
                    continue
                ids.append(entry["id"])
                line = f"{entry['id']} | {entry['t']}"
                if section in full:
                    line += f" | {entry['s']}"
                lines.append(line)

        if mode == "eval" and lang:
            for entry in self.data.get("sdk_specific") or []:
                if not self.usable(entry) or lang not in entry.get("sdk", []):
                    continue
                ids.append(entry["id"])
                lines.append(f"{entry['id']} | {entry['t']} | {entry['s']} Detect: {entry['detect']}")

        return "\n".join(lines), set(codes), set(ids)


REPO_SECTIONS = ["gates", "values", "patterns", "conversion", "limits"]


def repo_view(pack: "Pack") -> tuple[str, set[str]]:
    """Pack entries for the repository-level lens pass (id | title | statement), and their ids."""
    lines = ["PACK ENTRIES (id | title | statement)"]
    ids: set[str] = set()
    for section in REPO_SECTIONS:
        for entry in pack.data.get(section) or []:
            if pack.usable(entry):
                ids.add(entry["id"])
                lines.append(f"{entry['id']} | {entry['t']} | {entry['s']}")
    return "\n".join(lines), ids


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def validate(data: dict[str, Any]) -> list[str]:
    """Structural problems with a pack (empty list = valid)."""
    problems: list[str] = []

    meta = data.get("meta") or {}
    for key in ("id", "version"):
        if not meta.get(key):
            problems.append(f"meta.{key} is missing")

    signals = data.get("signals")
    if not isinstance(signals, dict) or not signals:
        problems.append("signals section is missing")
        signals = {}
    codes = {code for family in signals.values() for code in (family or {})}

    detection = data.get("detection") or {}
    for key in ("languages", "import_lines", "temporal_sdk", "manifests", "temporal_manifest", "adjacent"):
        if key not in detection:
            problems.append(f"detection.{key} is missing")
    for lang, pattern in (detection.get("import_lines") or {}).items():
        try:
            re.compile(pattern)
        except re.error as e:
            problems.append(f"detection.import_lines.{lang}: bad regex {pattern!r}: {e}")
    for group in ("temporal_sdk", "adjacent"):
        for name, patterns in (detection.get(group) or {}).items():
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as e:
                    problems.append(f"detection.{group}.{name}: bad regex {pattern!r}: {e}")

    seen: set[str] = set()
    for section in ALL_SECTIONS:
        entries = data.get(section)
        if entries is None:
            problems.append(f"section {section} is missing")
            continue
        for entry in entries:
            entry_id = entry.get("id", "<no id>")
            if entry_id in seen:
                problems.append(f"duplicate id {entry_id}")
            seen.add(entry_id)
            required = ("id", "k", "t") if section == "questions" else ("id", "k", "t", "s", "src")
            for key in required:
                if not entry.get(key):
                    problems.append(f"{entry_id}: missing {key}")
            if section == "questions":
                continue
            if entry.get("v") not in VALID_STATUSES:
                problems.append(f"{entry_id}: invalid v {entry.get('v')!r}")
            if entry.get("mode") not in VALID_MODES:
                problems.append(f"{entry_id}: invalid mode {entry.get('mode')!r}")
            for code in entry.get("on") or []:
                if code not in codes:
                    problems.append(f"{entry_id}: unknown signal code {code!r}")
            if section == "sdk_specific" and not entry.get("detect"):
                problems.append(f"{entry_id}: missing detect")
    return problems


def load_pack(path: Path) -> Pack:
    """Load and validate the pack; raises PackError with every problem found."""
    if not path.exists():
        raise PackError(f"knowledge pack not found at {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise PackError(f"knowledge pack {path} is not valid YAML: {e}") from e
    problems = validate(data or {})
    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise PackError(f"knowledge pack {path} is invalid:\n  {shown}{more}")
    pack = Pack(path=path, data=data, hash=_hash_file(path))
    pack.entries = {e["id"]: e for section in ALL_SECTIONS for e in data[section]}
    return pack
