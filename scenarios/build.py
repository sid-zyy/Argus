"""Build a scenario's git repo and telemetry from its history.py and generate_logs.py.

Usage: python -m scenarios.build incident1_conn_leak
Commit dates and authors are fixed, so the shas are the same on every machine.
"""
import importlib
import os
import shutil
import stat
import subprocess
import sys
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


def build(name: str) -> dict:
    scenario = ROOT / name
    history = importlib.import_module(f"scenarios.{name}.history")
    gen = importlib.import_module(f"scenarios.{name}.generate_logs")
    repo = scenario / "repo"
    if repo.exists():
        shutil.rmtree(repo, onerror=_force_remove)
    repo.mkdir()
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
