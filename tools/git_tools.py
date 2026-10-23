"""Read-only git and source tools over the scenario repo."""
from __future__ import annotations

from tools.workspace import Tool, ToolSet, Workspace, fmt, params

MAX_FILE_LINES = 120


def git_tools(ws: Workspace) -> ToolSet:
    def git_log(since: str | None = None, until: str | None = None, path: str | None = None) -> str:
        args = ["log", "--no-color", "--date=iso-strict", "--format=%h %ad %an | %s", "-n", "30"]
        if since:
            args.append(f"--since={since}")
        if until:
            args.append(f"--until={until}")
        if path:
            args += ["--", path]
        return ws.git(*args).strip() or "no commits in that range"

    def git_show(sha: str) -> str:
        if sha.startswith("-"):
            return "ERROR: invalid sha"
        source = f"git:{sha}"
        lines = ws.lines(source)
        return "\n".join(fmt(source, n, l) for n, l in enumerate(lines[:200], 1))

    return ToolSet([
        Tool("git_log", "Commit list (short sha, ISO date, author | subject). Dates like '2026-10-22T13:00:00Z'.",
             params({"since": {"type": "string"}, "until": {"type": "string"},
                     "path": {"type": "string", "description": "Limit to one file, e.g. 'app/checkout.py'"}}),
             git_log),
        Tool("git_show", "Show one commit's message and diff with line numbers. Cite it as source 'git:<sha>'.",
             params({"sha": {"type": "string"}}, ["sha"]), git_show),
    ])


def file_tools(ws: Workspace) -> ToolSet:
    def list_files() -> str:
        return ws.git("ls-files").strip()

    def read_file(path: str, start_line: int = 1, end_line: int = MAX_FILE_LINES) -> str:
        source = f"repo/{path.removeprefix('repo/')}"
        lines = ws.lines(source)
        a = max(1, int(start_line))
        b = min(len(lines), int(end_line), a + MAX_FILE_LINES - 1)
        return "\n".join(fmt(source, n, lines[n - 1]) for n in range(a, b + 1)) or "(empty file)"

    return ToolSet([
        Tool("list_files", "List files tracked in the service repo.", params({}), list_files),
        Tool("read_file", "Read a repo file at the deployed version with line numbers. Cite it as source 'repo/<path>'.",
             params({"path": {"type": "string", "description": "e.g. 'app/checkout.py'"},
                     "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
             read_file),
    ])
