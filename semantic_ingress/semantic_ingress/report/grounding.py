"""
Grounding checks: what a report may cite and name.

A citation is valid if it is a usable pack id, a URL listed with a pack entry
(those were confirmed through the Docs MCP), or a URL the docs search returned in
this run. Gates the index raised but verification did not confirm anywhere are
not citable. A path is valid if the index has it. Everything dropped is recorded
and shown in the report's appendix.
"""
from dataclasses import dataclass, field

from semantic_ingress.lens.pack import Pack


@dataclass
class Grounding:
    pack_ids: set[str]
    pack_urls: set[str]
    paths: set[str]
    returned_urls: set[str] = field(default_factory=set)
    rejected_gates: dict[str, str] = field(default_factory=dict)  # gate id -> verifier's reason
    dropped: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, pack: Pack, paths: set[str]) -> "Grounding":
        usable = {i: e for i, e in pack.entries.items() if pack.usable(e)}
        urls = {u for e in usable.values() for u in (e.get("url") or [])}
        return cls(pack_ids=set(usable), pack_urls=urls, paths=paths)

    def fork(self) -> "Grounding":
        """Same rules, with its own returned URLs and dropped list (one per process)."""
        return Grounding(self.pack_ids, self.pack_urls, self.paths, set(self.returned_urls),
                         dict(self.rejected_gates))

    def pack_id_ok(self, ref: str) -> bool:
        return ref in self.pack_ids and ref not in self.rejected_gates

    def cite_ok(self, ref: str) -> bool:
        return self.pack_id_ok(ref) or ref in self.pack_urls or ref in self.returned_urls

    def rejected_text(self) -> str:
        return "\n".join(f"{g}: {reason}" for g, reason in self.rejected_gates.items()) or "(none)"

    def problems_cites(self, refs: list[str], where: str) -> list[str]:
        return [
            f"{where}: citation {r!r} is a gate that verification did not confirm; do not rely on it"
            if r in self.rejected_gates else f"{where}: citation {r!r} is not a pack id or an allowed URL"
            for r in refs if not self.cite_ok(r)
        ]

    def problems_paths(self, paths: list[str], where: str) -> list[str]:
        return [f"{where}: path {p!r} is not in the index" for p in paths if p not in self.paths]

    def cites(self, refs: list[str], where: str) -> list[str]:
        """Valid citations, deduplicated; the rest are recorded as dropped."""
        kept = []
        for r in dict.fromkeys(refs):
            if self.cite_ok(r):
                kept.append(r)
            elif r in self.rejected_gates:
                self.dropped.append(f"{where}: citation dropped ({r}: not confirmed by verification)")
            else:
                self.dropped.append(f"{where}: citation dropped ({r})")
        return kept

    def keep_paths(self, paths: list[str], where: str) -> list[str]:
        kept = []
        for p in dict.fromkeys(paths):
            if p in self.paths:
                kept.append(p)
            else:
                self.dropped.append(f"{where}: unknown path dropped ({p})")
        return kept
