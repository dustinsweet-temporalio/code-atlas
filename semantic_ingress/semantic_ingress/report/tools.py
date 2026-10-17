"""
Tools the process investigator uses: thin reads over the index (summaries and
lens records) and the mirrored source. A regex search over the source stands in
for call-graph following (there is no parser).

Every source line the investigator sees (read or matched) is recorded, because a
process card may cite only lines that were actually seen.
"""
import re
from collections import defaultdict

from semantic_ingress.report.gather import IndexData

MAX_READ_LINES = 250
MAX_MATCHES = 50
MAX_LINE_CHARS = 200
SUMMARY_CHARS = 2500

TOOLS = [
    {
        "name": "read_source",
        "description": f"Read numbered source lines of an indexed file (at most {MAX_READ_LINES} lines per call).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative path"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path", "start_line", "end_line"],
            "additionalProperties": False,
        },
    },
    {
        "name": "grep_source",
        "description": (
            f"Search the source of every indexed file for a Python regular expression. Returns up to "
            f"{MAX_MATCHES} matching lines as path:line: text. Use it to find callers, definitions and writes."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression"},
                "path_prefix": {"type": "string", "description": "Only files under this prefix; empty for all"},
            },
            "required": ["pattern", "path_prefix"],
            "additionalProperties": False,
        },
    },
    {
        "name": "file_summary",
        "description": "The index's summary and Temporal lens record of one file (a guide; confirm facts in the source).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "folder_summary",
        "description": "The index's summary of a folder and everything beneath it ('.' for the repository root).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_summaries",
        "description": "Full-text search over the index's file summaries; returns the best-matching paths with a snippet.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]


class InvestigationTools:
    def __init__(self, data: IndexData, store, repo_id: str):
        self.data = data
        self.store = store
        self.repo_id = repo_id
        self.seen: dict[str, set[int]] = defaultdict(set)  # path -> line numbers seen

    def handle(self, name: str, args: dict) -> str:
        if name == "read_source":
            return self._read(args["path"], int(args["start_line"]), int(args["end_line"]))
        if name == "grep_source":
            return self._grep(args["pattern"], args.get("path_prefix", ""))
        if name == "file_summary":
            return self._summary("file_summaries", args["path"])
        if name == "folder_summary":
            return self._summary("folder_summaries", args["path"] or ".")
        if name == "search_summaries":
            return self._search(args["query"])
        return f"Unknown tool {name}"

    def _read(self, path: str, start: int, end: int) -> str:
        if path not in self.data.blobs:
            return f"{path} is not an indexed file."
        lines = self.data.source_lines(path)
        if not lines:
            return f"No source available for {path}."
        start = max(1, start)
        end = min(len(lines), end, start + MAX_READ_LINES - 1)
        if start > end:
            return f"{path} has {len(lines)} lines."
        self.seen[path].update(range(start, end + 1))
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n({path} has {len(lines)} lines)" if end < len(lines) else ""
        return body + more

    def _grep(self, pattern: str, prefix: str) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"Invalid regular expression: {e}"
        out = []
        for path in sorted(self.data.blobs):
            if prefix and not path.startswith(prefix):
                continue
            for i, line in enumerate(self.data.source_lines(path), 1):
                if rx.search(line):
                    self.seen[path].add(i)
                    out.append(f"{path}:{i}: {line.strip()[:MAX_LINE_CHARS]}")
                    if len(out) >= MAX_MATCHES:
                        return "\n".join(out) + f"\n(stopped at {MAX_MATCHES} matches; narrow the pattern)"
        return "\n".join(out) or "No matches."

    def _summary(self, collection: str, path: str) -> str:
        docs = self.store.query_documents(
            self.repo_id, collection, {"path": path}, {"prose_summary": 1, "lens_prose": 1}, limit=1
        )
        if not docs:
            return f"No {collection[:-1].replace('_', ' ')} for {path}."
        doc = docs[0]
        lens = f"\n\nLENS: {doc['lens_prose']}" if doc.get("lens_prose") else ""
        return (doc.get("prose_summary") or "")[:SUMMARY_CHARS] + lens

    def _search(self, query: str) -> str:
        docs = self.store.text_search_repo(self.repo_id, "file_summaries", query, limit=8)
        return "\n".join(f"{d['path']}: {(d.get('prose_summary') or '')[:300]}" for d in docs) or "No matches."
