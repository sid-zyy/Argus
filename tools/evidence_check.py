"""Deterministic evidence check: every cited source:line must exist and contain the quote.

This is what makes 'no claim without proof' real: an agent cannot cite a log line it made up.
"""
from __future__ import annotations

import re

from orchestrator.state import Evidence
from tools.workspace import Workspace

MIN_QUOTE = 6


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def check_one(ws: Workspace, ev: Evidence) -> str | None:
    """Return an error message, or None if the evidence is real."""
    try:
        lines = ws.lines(ev.source)
    except (ValueError, OSError) as e:
        return f"{ev.source}:{ev.line}: unknown source ({e})"
    if not 1 <= ev.line <= len(lines):
        return f"{ev.source}:{ev.line}: line out of range (source has {len(lines)} lines)"
    quote = _norm(ev.quote)
    prefix = f"{ev.source}:{ev.line}:"
    if quote.startswith(prefix):
        quote = quote[len(prefix):].strip()
    quote = quote.strip("\"'` ").removesuffix("…").removesuffix("...").strip()
    if len(quote) < MIN_QUOTE:
        return f"{ev.source}:{ev.line}: quote too short; copy at least {MIN_QUOTE} characters from the line"
    if quote not in _norm(lines[ev.line - 1]):
        return f"{ev.source}:{ev.line}: quote not found on that line. The line reads: {lines[ev.line - 1][:200]!r}"
    return None


def check_all(ws: Workspace, evidence: list[Evidence]) -> list[str]:
    return [err for ev in evidence if (err := check_one(ws, ev))]


SYMPTOM_SOURCES = ("app.log", "metrics.log")


def cause_evidence_missing(category: str, evidence: list[Evidence]) -> str | None:
    """A supported root cause must cite the cause itself, not only logs that show the symptom.

    code/config/resource causes need a code line ('repo/...'), a commit ('git:...') or a deploy record;
    dependency causes need the dependency's own signal (any log other than the service's app log and metrics).
    """
    sources = [e.source for e in evidence]
    if category == "dependency":
        if not any(s not in SYMPTOM_SOURCES for s in sources):
            return "a dependency cause needs evidence from the dependency's own logs, not just this service's errors"
        return None
    if not any(s.startswith(("repo/", "git:")) or s == "deploy.log" for s in sources):
        return (f"a {category} cause needs evidence of the cause itself (a code line 'repo/...', a commit 'git:...' "
                "or a deploy record), not only logs showing the symptom; otherwise use 'insufficient'")
    return None
