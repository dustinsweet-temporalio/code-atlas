"""
Tier 0: deterministic repository scan (no LLM).

Finds Temporal SDK use and adjacent orchestration/job technologies from the
pack's detection markers. Dependency manifests are matched in full; source files
only on import-like lines, so a comment mentioning a technology is not evidence.
"""
import fnmatch
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from semantic_ingress.lens.pack import Pack

MAX_EVIDENCE = 10
_IMPORT_LINE = re.compile(
    r"^\s*(import\b|from\s+\S+\s+import\b|require\b|.*\brequire\(|using\b|use\b|#include\b|@Grab|implementation\b)"
)


@dataclass
class Tier0Facts:
    languages: dict[str, int] = field(default_factory=dict)          # language -> files
    temporal_sdk: dict[str, list[str]] = field(default_factory=dict)  # language -> evidence paths (up to 10)
    temporal_counts: dict[str, int] = field(default_factory=dict)     # language -> all matching files
    temporal_manifests: list[str] = field(default_factory=list)
    adjacent: dict[str, list[str]] = field(default_factory=dict)      # technology -> evidence paths

    @property
    def temporal_file_count(self) -> int:
        return sum(self.temporal_counts.values())

    def to_text(self) -> str:
        lines = ["languages: " + (", ".join(f"{k} {v}" for k, v in sorted(self.languages.items(), key=lambda kv: -kv[1])) or "none")]
        if self.temporal_sdk or self.temporal_manifests:
            for lang, paths in sorted(self.temporal_sdk.items()):
                lines.append(f"temporal_sdk {lang}: {self.temporal_counts[lang]} files, e.g. {', '.join(paths[:5])}")
            if self.temporal_manifests:
                lines.append("temporal_sdk in manifests: " + ", ".join(self.temporal_manifests[:5]))
        else:
            lines.append("temporal_sdk: none found")
        if self.adjacent:
            for name, paths in sorted(self.adjacent.items()):
                lines.append(f"adjacent {name}: {', '.join(paths[:5])}")
        else:
            lines.append("adjacent: none found")
        return "\n".join(lines)


def _is_manifest(path: str, patterns: list[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def scan(pack: Pack, files: dict[str, str]) -> Tier0Facts:
    """Scan file contents (path -> text) with the pack's detection markers."""
    det = pack.detection
    adjacent_patterns = {name: [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in ps] for name, ps in det["adjacent"].items()}
    temporal_manifest = [re.compile(p) for p in det["temporal_manifest"]]

    languages: Counter = Counter()
    temporal: dict[str, list[str]] = defaultdict(list)
    manifests_hit: list[str] = []
    adjacent: dict[str, list[str]] = defaultdict(list)

    for path in sorted(files):
        text = files[path]
        lang = pack.language_of(path)
        if lang:
            languages[lang] += 1
            if pack.file_mode(path, text) == "eval":
                temporal[lang].append(path)

        if _is_manifest(path, det["manifests"]):
            haystack = text
            if any(p.search(text) for p in temporal_manifest):
                manifests_hit.append(path)
        elif lang:
            haystack = "\n".join(line for line in text.splitlines() if _IMPORT_LINE.match(line))
        else:
            continue

        for name, patterns in adjacent_patterns.items():
            if len(adjacent[name]) < MAX_EVIDENCE and any(p.search(haystack) for p in patterns):
                adjacent[name].append(path)

    return Tier0Facts(
        languages=dict(languages),
        temporal_sdk={k: v[:MAX_EVIDENCE] for k, v in temporal.items()},
        temporal_counts={k: len(v) for k, v in temporal.items()},
        temporal_manifests=manifests_hit[:MAX_EVIDENCE],
        adjacent={k: v for k, v in adjacent.items() if v},
    )
