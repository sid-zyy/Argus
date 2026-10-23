"""Log tools: list sources, regex search with a time window, read a line range."""
from __future__ import annotations

import re

from tools.workspace import Tool, ToolSet, Workspace, fmt, params

MAX_RESULTS = 15
MAX_READ = 40


def _ts(line: str) -> str:
    # Lines start with '2026-10-22T14:06:43.030Z' or '2026-10-22 14:06:45.000 UTC'; compare as 'YYYY-MM-DDTHH:MM:SS'
    return line[:19].replace(" ", "T")


def _in_window(line: str, start: str | None, end: str | None) -> bool:
    t = _ts(line)
    if start and t < start.replace(" ", "T")[:19]:
        return False
    if end and t > end.replace(" ", "T")[:19]:
        return False
    return True


def log_tools(ws: Workspace) -> ToolSet:
    def list_sources() -> str:
        out = []
        for s in ws.log_sources() + sorted(ws.extra_sources):
            lines = ws.lines(s)
            first = _ts(lines[0]) if lines else "-"
            last = _ts(lines[-1]) if lines else "-"
            out.append(f"{s}: {len(lines)} lines, {first} .. {last}")
        return "\n".join(out)

    def search_logs(pattern: str, source: str | None = None, start: str | None = None,
                    end: str | None = None, max_results: int = MAX_RESULTS) -> str:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"ERROR: bad regex: {e}"
        sources = [source] if source else ws.log_sources()
        hits = []
        for s in sources:
            for n, line in enumerate(ws.lines(s), 1):
                if rx.search(line) and _in_window(line, start, end):
                    hits.append((s, n, line))
        if not hits:
            return "0 matches"
        limit = max(1, min(int(max_results), MAX_RESULTS))
        head = f"{len(hits)} matches; first {hits[0][0]}:{hits[0][1]} at {_ts(hits[0][2])}, last {hits[-1][0]}:{hits[-1][1]} at {_ts(hits[-1][2])}"
        shown = hits[:limit]
        body = "\n".join(fmt(s, n, l) for s, n, l in shown)
        more = f"\n... {len(hits) - limit} more (narrow with start/end or a stricter pattern)" if len(hits) > limit else ""
        return f"{head}\n{body}{more}"

    def read_lines(source: str, start_line: int, end_line: int) -> str:
        lines = ws.lines(source)
        a = max(1, int(start_line))
        b = min(len(lines), int(end_line), a + MAX_READ - 1)
        if a > len(lines):
            return f"ERROR: {source} has only {len(lines)} lines"
        return "\n".join(fmt(source, n, lines[n - 1]) for n in range(a, b + 1))

    return ToolSet([
        Tool("list_sources", "List log sources with line counts and time ranges.", params({}), list_sources),
        Tool("search_logs",
             "Case-insensitive regex search over log files. Returns a match count, first/last match and up to 15 numbered lines. "
             "Times are ISO like '2026-10-22T14:05:00'.",
             params({
                 "pattern": {"type": "string", "description": "Regex, e.g. 'ERROR|503'"},
                 "source": {"type": "string", "description": "One log file, e.g. 'app.log'. Omit to search all."},
                 "start": {"type": "string", "description": "Only lines at or after this time"},
                 "end": {"type": "string", "description": "Only lines at or before this time"},
                 "max_results": {"type": "integer"},
             }, ["pattern"]),
             search_logs),
        Tool("read_lines", "Read numbered lines from a log source (max 40 lines).",
             params({"source": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
                    ["source", "start_line", "end_line"]),
             read_lines),
    ])
