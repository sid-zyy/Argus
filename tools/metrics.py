"""Metrics tool over the service's metrics source (one line per time bucket, key=value fields).

Which source holds the metrics and what the fields mean come from the service's config.
"""
from __future__ import annotations

from connect.config import DEFAULT_METRICS_DESCRIPTION
from tools.logs import _in_window
from tools.workspace import Tool, ToolSet, Workspace, fmt, params


def metric_tools(ws: Workspace) -> ToolSet:
    source = ws.config.metrics_source

    def get_metrics(start: str | None = None, end: str | None = None) -> str:
        if not source or source not in ws.log_sources():
            return "ERROR: this service has no metrics source; use search_logs instead"
        rows = [(n, l) for n, l in enumerate(ws.lines(source), 1) if _in_window(l, start, end)]
        if not rows:
            return "no metric rows in that window"
        return "\n".join(fmt(source, n, l) for n, l in rows[:60])

    description = ws.config.metrics.description or DEFAULT_METRICS_DESCRIPTION
    if source and source != "metrics.log":
        description += f" Cite rows as source '{source}'."
    return ToolSet([
        Tool("get_metrics", description, params({"start": {"type": "string"}, "end": {"type": "string"}}), get_metrics),
    ])
