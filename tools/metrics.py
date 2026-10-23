"""Metrics tool over logs/metrics.log (one line per 30s bucket, key=value fields)."""
from __future__ import annotations

from tools.logs import _in_window
from tools.workspace import Tool, ToolSet, Workspace, fmt, params


def metric_tools(ws: Workspace) -> ToolSet:
    def get_metrics(start: str | None = None, end: str | None = None) -> str:
        if "metrics.log" not in ws.log_sources():
            return "ERROR: no metrics.log in this scenario"
        rows = [(n, l) for n, l in enumerate(ws.lines("metrics.log"), 1) if _in_window(l, start, end)]
        if not rows:
            return "no metric rows in that window"
        return "\n".join(fmt("metrics.log", n, l) for n, l in rows[:60])

    return ToolSet([
        Tool("get_metrics",
             "Service metrics per 30s bucket (rps, error_rate_5xx, p95_ms, pool_in_use, mem_mb, restarts). "
             "Database health is in db.log; use search_logs for it.",
             params({"start": {"type": "string"}, "end": {"type": "string"}}),
             get_metrics),
    ])
