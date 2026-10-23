"""Read-only view of one scenario (logs + git repo) and the tool registry agents call."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAX_LINE = 300


class Workspace:
    def __init__(self, scenario_dir: str | Path):
        self.root = Path(scenario_dir).resolve()
        self.logs_dir = self.root / "logs"
        self.repo_dir = self.root / "repo"
        self.alert = json.loads((self.root / "alert.json").read_text(encoding="utf-8"))
        self._cache: dict[str, list[str]] = {}
        self.extra_sources: dict[str, list[str]] = {}  # e.g. 'report' for tester bug reports

    def log_sources(self) -> list[str]:
        return sorted(p.name for p in self.logs_dir.glob("*.log"))

    def git(self, *args: str) -> str:
        r = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=self.repo_dir,
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise ValueError(r.stderr.strip() or f"git {' '.join(args)} failed")
        return r.stdout

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
        else:
            path = self._safe(self.logs_dir, source)
            out = path.read_text(encoding="utf-8").splitlines()
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
