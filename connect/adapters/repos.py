"""Repository adapter: where a service's code and history come from.

Only git for now: a local checkout (`repo.path`) or a remote (`repo.url`) cloned once into runs/repos/<service>.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from connect.config import ServiceConfig


class GitRepo:
    def __init__(self, cfg: ServiceConfig):
        self.cfg = cfg
        self.path: Path = cfg.repo_path()

    def git(self, *args: str) -> str:
        r = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=self.path,
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise ValueError(r.stderr.strip() or f"git {' '.join(args)} failed")
        return r.stdout

    def exists(self) -> bool:
        return (self.path / ".git").exists()

    def ensure(self) -> None:
        """Clone the remote on first use. A local checkout is used as it is."""
        if self.exists() or not self.cfg.repo.url:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        args = ["git", "clone", "-q"] + (["-b", self.cfg.repo.branch] if self.cfg.repo.branch else []) + [self.cfg.repo.url, str(self.path)]
        r = subprocess.run(args, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise ValueError(f"could not clone {self.cfg.repo.url}: {r.stderr.strip()}")


ADAPTERS = {"git": GitRepo}


def repo_for(cfg: ServiceConfig) -> GitRepo:
    return ADAPTERS[cfg.repo.adapter](cfg)
