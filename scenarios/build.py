"""Build a scenario's git repo and telemetry from its history.py and generate_logs.py.

Usage: python -m scenarios.build incident1_conn_leak
Commit dates and authors are fixed, so the shas are the same on every machine.
"""
import importlib
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent


def _git(repo: Path, *args, env=None):
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args],
        cwd=repo, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _empty(repo: Path) -> None:
    """Delete what is inside the repo folder, keeping the folder itself. On Windows the folder can't be removed while
    another process (e.g. ShopFast's deploy watcher running git) has it as its working directory, so emptying it is
    more reliable; a file that is briefly locked is retried."""
    repo.mkdir(exist_ok=True)
    for attempt in range(10):
        try:
            for child in repo.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, onerror=_force_remove)
                else:
                    os.chmod(child, stat.S_IWRITE)
                    child.unlink()
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.3)


def _module(scenario: Path, part: str):
    if scenario.parent == ROOT:
        return importlib.import_module(f"scenarios.{scenario.name}.{part}")
    spec = importlib.util.spec_from_file_location(f"pack_{scenario.name}_{part}", scenario / f"{part}.py")  # a pack elsewhere
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build(name: str, root: Path = ROOT) -> dict:
    scenario = root / name
    history = _module(scenario, "history")
    gen = _module(scenario, "generate_logs")
    repo = scenario / "repo"
    _empty(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    shas = []
    for date, author, message, files in history.COMMITS:
        for path, content in files.items():
            p = repo / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content.encode("utf-8"))
        _git(repo, "add", "-A")
        who, email = author[:-1].split(" <")
        env = {**os.environ, "GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
               "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date}
        _git(repo, "commit", "-q", "-m", message, env=env)
        shas.append(_git(repo, "rev-parse", "HEAD"))
    info = gen.generate(scenario, shas, history.CULPRIT_INDEX)
    info["shas"] = [s[:7] for s in shas]
    return info


if __name__ == "__main__":
    import json
    print(json.dumps(build(sys.argv[1] if len(sys.argv) > 1 else "incident1_conn_leak"), indent=2))
