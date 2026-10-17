"""
Temporal lens: how it is shown to the agent and in the UI.

The indexer stores a compact lens line (`lens_prose`) on file and folder
documents, a repository lens on the repo summary document, and an `index_meta`
document per repository recording the knowledge pack the lens was built with.
"""
from typing import Any

INDEX_META_COLLECTION = "index_meta"


def lens_block(document: dict[str, Any]) -> str:
    """Lens section appended to a file or folder summary when include_lens is set."""
    prose = document.get("lens_prose")
    if not prose:
        return "\n\nTemporal lens: not available (this item was indexed without the lens)."
    version = (document.get("lens") or {}).get("pack_version", "?")
    return f"\n\nTemporal lens (pack {version}): {prose}"


def repo_lens_block(document: dict[str, Any]) -> str:
    """Lens section appended to the repository summary when include_lens is set."""
    lens = document.get("lens")
    if not lens:
        return "\n\nTemporal lens: not available (this repository was indexed without the lens)."
    lines = [
        f"\n\nTemporal lens (pack {lens.get('pack_version', '?')}):",
        f"- Status: {lens['status']} | archetype: {lens['archetype']}",
    ]
    for d in lens.get("detected", []):
        lines.append(f"- Detected {d['kind']}: {d['name']} (e.g. {', '.join(d['evidence'][:3])})")
    for p in lens.get("processes", []):
        lines.append(f"- Process: {p['name']} (entry {p['entry']}; concepts {' '.join(p['concepts'])}): {p['narrative']}")
    for a in lens.get("anti", []):
        lines.append(f"- Gate {a['id']}: {', '.join(a['evidence'][:3])}")
    lines.append(f"- Summary: {lens['n']}")
    if lens.get("cites"):
        lines.append("- Sources: " + "; ".join(f"{c['ref']} ({c['supports']})" for c in lens["cites"]))
    return "\n".join(lines)


def index_lens_info(db) -> dict[str, Any]:
    """
    Pack version and lens state for one repository database.

    lens: "off" (no lens), "partial" (some files lack one), "mixed" (files on more
    than one pack version), or "on". indexed_at and pack_version may be None.
    """
    meta = db[INDEX_META_COLLECTION].find_one({}) or {}
    versions = [v for v in meta.get("lens_pack_versions", []) if v.get("files")]
    with_lens = [v for v in versions if v.get("version")]
    if not with_lens:
        # Lens switched on for an index built without it: no file has one until it changes.
        state = "partial" if meta.get("lens_enabled") else "off"
    elif len(with_lens) < len(versions):
        state = "partial"
    elif len(with_lens) > 1:
        state = "mixed"
    else:
        state = "on"
    indexed_at = meta.get("indexed_at")
    return {
        "pack_version": (meta.get("pack") or {}).get("version"),
        "pack_versions": [{"version": v["version"], "files": v["files"]} for v in with_lens],
        "lens": state,
        "indexed_at": indexed_at.isoformat() if hasattr(indexed_at, "isoformat") else indexed_at,
        # Summary prompt versions (None = written before versions were recorded); more than one = mixed.
        "prompt_versions": [
            {"version": v.get("version"), "files": v["files"]} for v in meta.get("prompt_versions", []) if v.get("files")
        ],
    }


def describe_lens_info(info: dict[str, Any]) -> str:
    """One line for listings, e.g. 'Temporal lens: on (pack 0.1.0)'."""
    if info["lens"] == "off":
        return "Temporal lens: off"
    versions = " + ".join(v["version"] for v in info["pack_versions"]) or info["pack_version"] or "?"
    note = {
        "partial": "; some files have no lens yet (index with --refresh-lens)",
        "mixed": "; files on different pack versions (index with --refresh-lens)",
    }.get(info["lens"], "")
    return f"Temporal lens: {info['lens']} (pack {versions}{note})"
