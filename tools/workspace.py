"""Read-only view of one service (logs + git repo) and the tool registry agents call.

What the view contains comes from the service's argus.yaml through Argus Connect's adapters (connect/), so the same
agents and tools work on any connected service; a folder without argus.yaml uses the built-in scenario layout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from connect.adapters.logs import logs_for
from connect.adapters.repos import repo_for
from connect.config import ServiceConfig, load
from connect.schema import IncidentInput

MAX_LINE = 300


class Workspace:
    def __init__(self, scenario_dir: str | Path, config: ServiceConfig | None = None):
        self.root = Path(scenario_dir).resolve()
        self.config = config or load(self.root)
        self.repo = repo_for(self.config)
        self.repo_dir = self.repo.path
        self.logs = logs_for(self.config)
        base = self.config.base_alert()
        self.alert = IncidentInput.model_validate({"service": self.config.service.name, **base}).as_alert() if base else {
            "id": "MANUAL", "service": self.config.service.name, "title": f"{self.config.service.name} incident"}
        self._cache: dict[str, list[str]] = {}
        self.extra_sources: dict[str, list[str]] = {}  # e.g. 'report' for tester bug reports

    # what the evidence rules and the metrics tool need to know about this service's sources
    @property
    def symptom_sources(self) -> tuple[str, ...]:
        return self.config.symptom_sources

    @property
    def change_sources(self) -> tuple[str, ...]:
        return self.config.change_sources

    def log_sources(self) -> list[str]:
        return sorted(name for name, src in self.logs.items() if src.exists())

    def git(self, *args: str) -> str:
        return self.repo.git(*args)

    def lines(self, source: str) -> list[str]:
        """Lines of an evidence source: 'app.log', 'repo/app/x.py', 'git:<sha>' or 'report'."""
        if source in self._cache:
            return self._cache[source]
        if source in self.extra_sources:
            out = self.extra_sources[source]
        elif source.startswith("git:"):
            out = self.git("show", "--no-color", "--format=medium", source[4:]).splitlines()
        elif source.startswith("repo/"):
            path = self._safe(self.repo_dir, source[5:])
            out = path.read_text(encoding="utf-8").splitlines()
        elif source in self.logs:
            out = self.logs[source].lines()
        else:
            raise ValueError(f"no such file: {source}")
        self._cache[source] = out
        return out

    @staticmethod
    def _safe(base: Path, rel: str) -> Path:
        p = (base / rel).resolve()
        if base.resolve() not in p.parents and p != base.resolve():
            raise ValueError(f"path escapes workspace: {rel}")
        if not p.is_file():
            raise ValueError(f"no such file: {rel}")
        return p


def fmt(source: str, n: int, text: str) -> str:
    if len(text) > MAX_LINE:
        text = text[:MAX_LINE] + " …"
    return f"{source}:{n}: {text}"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]

    def spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class ToolSet:
    tools: list[Tool] = field(default_factory=list)

    def __add__(self, other: "ToolSet") -> "ToolSet":
        return ToolSet(self.tools + other.tools)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self.tools]

    def run(self, name: str, args: dict) -> str:
        tool = next((t for t in self.tools if t.name == name), None)
        if tool is None:
            return f"ERROR: unknown tool {name!r}. Available: {', '.join(t.name for t in self.tools)}"
        if "__invalid_json__" in args:
            return "ERROR: tool arguments were not valid JSON"
        try:
            return tool.fn(**args)
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except (ValueError, OSError) as e:
            return f"ERROR: {e}"


def params(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}
