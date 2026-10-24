"""The live shop has no pre-made telemetry: shop/server.py writes the logs while it runs.

This only creates empty log files so the workspace is valid before the shop has started.
"""
from pathlib import Path

LOGS = ("app.log", "metrics.log", "db.log", "deploy.log")


def reset_logs(scenario: Path) -> None:
    logs = scenario / "logs"
    logs.mkdir(exist_ok=True)
    for name in LOGS:
        (logs / name).write_text("", encoding="utf-8")


def generate(scenario: Path, shas: list[str], culprit_index) -> dict:
    reset_logs(scenario)
    return {"live": True}
