"""Event log for one incident run. Every agent step and human action goes here; listeners render it live."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

Listener = Callable[[dict], None]


class Trace:
    def __init__(self, listeners: list[Listener] | None = None):
        self.events: list[dict] = []
        self.listeners = listeners or []

    def emit(self, actor: str, type_: str, payload: dict) -> dict:
        event = {"seq": len(self.events) + 1, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                 "actor": actor, "type": type_, "payload": payload}
        self.events.append(event)
        for listener in self.listeners:
            listener(event)
        return event

    def of_type(self, type_: str) -> list[dict]:
        return [e for e in self.events if e["type"] == type_]

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.events, indent=2, default=str), encoding="utf-8")
