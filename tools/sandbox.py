"""Test a proposed fix on a throwaway clone of the repo: repro before, repro after, then the repo's tests.

Mode SANDBOX_MODE=docker runs each step in a container with no network and CPU/memory limits.
Mode 'subprocess' (fallback when Docker is missing) runs in a temp directory with a scrubbed
environment and a timeout; it does NOT isolate the network.
The scenario's real repo is never modified.
"""
from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from orchestrator.state import FixOutput, SandboxResult, SandboxRun
from tools.workspace import Workspace

TIMEOUT = 60
DOCKER_IMAGE = os.getenv("SANDBOX_IMAGE", "incident-commander-sandbox")
MAX_OUTPUT = 2000


class FixError(ValueError):
    pass


def preview_fix(ws: Workspace, fix: FixOutput) -> tuple[str, list[str]]:
    """Return (unified diff, errors) without touching any file."""
    if fix.fix_type == "rollback":
        if not fix.rollback_to:
            return "", ["rollback needs rollback_to (a commit sha)"]
        try:
            ws.git("rev-parse", "--verify", f"{fix.rollback_to}^{{commit}}")
            return ws.git("diff", "--no-color", "HEAD", fix.rollback_to), []
        except ValueError as e:
            return "", [f"rollback_to {fix.rollback_to!r} is not a commit: {e}"]
    if not fix.edits:
        return "", [f"{fix.fix_type} needs at least one edit"]
    errors, diffs, files = [], [], {}
    for i, ed in enumerate(fix.edits):
        path = ed.file.removeprefix("repo/")
        if path not in files:
            try:
                files[path] = (ws.repo_dir / path).read_text(encoding="utf-8") if (ws.repo_dir / path).is_file() else None
            except OSError as e:
                errors.append(f"edit {i}: cannot read {path}: {e}")
                continue
        text = files[path]
        if text is None:
            errors.append(f"edit {i}: {path} does not exist")
            continue
        count = text.count(ed.old)
        if count != 1:
            errors.append(f"edit {i}: 'old' text occurs {count} times in {path}; it must match exactly once (copy it from read_file without line prefixes)")
            continue
        files[path] = text.replace(ed.old, ed.new)
    if errors:
        return "", errors
    for path, new in files.items():
        old = (ws.repo_dir / path).read_text(encoding="utf-8")
        diffs.append("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}")))
    return "".join(diffs), []


def _apply(repo: Path, ws: Workspace, fix: FixOutput) -> None:
    if fix.fix_type == "rollback":
        _git(repo, "checkout", "-q", fix.rollback_to)
        return
    for ed in fix.edits:
        p = repo / ed.file.removeprefix("repo/")
        p.write_text(p.read_text(encoding="utf-8").replace(ed.old, ed.new, 1), encoding="utf-8")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True, capture_output=True)


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _mode() -> str:
    mode = os.getenv("SANDBOX_MODE", "auto")
    if mode == "auto":
        return "docker" if docker_available() else "subprocess"
    return mode


def _run(mode: str, work: Path, argv: list[str]) -> SandboxRun:
    t0 = time.time()
    if mode == "docker":
        cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "256m", "--cpus", "1", "--pids-limit", "128",
               "-e", "PYTHONPATH=/work/repo", "-v", f"{work}:/work", "-w", "/work", DOCKER_IMAGE, "python", *argv]
        env = None
    else:
        cmd = [sys.executable, *argv]
        keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(work / "repo"))
    try:
        r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=TIMEOUT)
        out = (r.stdout + r.stderr).strip()
        code = r.returncode
    except subprocess.TimeoutExpired:
        out, code = f"timed out after {TIMEOUT}s", 124
    return SandboxRun(exit_code=code, output=out[-MAX_OUTPUT:], seconds=round(time.time() - t0, 2))


def run_sandbox(ws: Workspace, fix: FixOutput) -> SandboxResult:
    mode = _mode()
    diff, errors = preview_fix(ws, fix)
    work = Path(tempfile.mkdtemp(prefix="ic-sandbox-"))
    try:
        _git(work, "clone", "-q", "--no-hardlinks", str(ws.repo_dir), "repo")
        shutil.copy(ws.root / "repro.py", work / "repro.py")
        before = _run(mode, work, ["repro.py", "repo"])
        reproduced = before.exit_code != 0
        if errors:
            return SandboxResult(mode=mode, before=before, reproduced=reproduced, passed=False, error="; ".join(errors))
        _apply(work / "repo", ws, fix)
        after = _run(mode, work, ["repro.py", "repo"])
        tests = None
        if (work / "repo" / "tests").is_dir():
            tests = _run(mode, work, ["-m", "pytest", "-q", "-p", "no:cacheprovider", "repo/tests"])
        passed = after.exit_code == 0 and (tests is None or tests.exit_code == 0)
        return SandboxResult(mode=mode, before=before, after=after, tests=tests, reproduced=reproduced, passed=passed, diff=diff)
    finally:
        shutil.rmtree(work, ignore_errors=True)
