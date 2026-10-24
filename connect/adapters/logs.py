"""Log adapters: turn a service's logs, whatever their format, into numbered text lines that start with an ISO time.

The agents search, read and quote these lines, and the evidence checker compares quotes against the same rendering,
so a JSON log or a CSV of metrics is investigated exactly like a plain text log.

- text:    plain lines that already start with a timestamp
- jsonl:   one JSON object per line (pino, bunyan, structlog, CI records ...), rendered as `time LEVEL message k=v ...`
- csv:     a header row then one row per sample (metrics exports), rendered as `time k=v ...`
- command: the output of a command (e.g. `docker logs`, `kubectl logs`), read once per incident
"""
from __future__ import annotations

import csv
import io
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from connect.config import LogConfig, ServiceConfig

TIME_KEYS = ("time", "timestamp", "ts", "@timestamp", "date")
LEVEL_KEYS = ("level", "severity", "lvl")
MESSAGE_KEYS = ("msg", "message", "event")


def _iso(value, fmt: str) -> str | None:
    """Any supported timestamp as '2026-10-23T19:03:41.220Z'."""
    if value in (None, ""):
        return None
    try:
        if fmt == "epoch_ms" or (fmt == "iso" and isinstance(value, (int, float)) and value > 1e11):
            t = datetime.fromtimestamp(float(value) / 1000, timezone.utc)
        elif fmt == "epoch_s" or isinstance(value, (int, float)):
            t = datetime.fromtimestamp(float(value), timezone.utc)
        else:
            t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
            t = t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _value(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    s = str(v)
    return json.dumps(s) if (not s or any(c in s for c in ' ="')) else s


class LogSource:
    def __init__(self, cfg: LogConfig, service: ServiceConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.role = cfg.role
        self.path: Path | None = service.resolve(cfg.path) if cfg.path else None
        self.bad_lines = 0  # lines that could not be parsed; kept as raw text and reported by the checks

    def exists(self) -> bool:
        return bool(self.path and self.path.is_file())

    def _text(self) -> str:
        if not self.exists():
            raise ValueError(f"no such file: {self.name}" + (f" ({self.path})" if self.path else ""))
        return self.path.read_text(encoding="utf-8", errors="replace")

    def lines(self) -> list[str]:
        return self._text().splitlines()


class TextLog(LogSource):
    pass


class JsonLinesLog(LogSource):
    def _key(self, field: str, defaults: tuple[str, ...], obj: dict) -> str | None:
        key = self.cfg.fields.get(field)
        return key if key else next((k for k in defaults if k in obj), None)

    def render(self, obj: dict) -> str:
        tk, lk, mk = self._key("time", TIME_KEYS, obj), self._key("level", LEVEL_KEYS, obj), self._key("message", MESSAGE_KEYS, obj)
        ts = _iso(obj.get(tk), self.cfg.time_format) if tk else None
        level = obj.get(lk) if lk else None
        if level is not None:
            level = self.cfg.level_names.get(str(level), str(level)).upper()
        skip = {tk, lk, mk, *self.cfg.drop}
        rest = " ".join(f"{k}={_value(v)}" for k, v in obj.items() if k not in skip)
        parts = [ts or "-", f"{level:<5}" if level else None, str(obj.get(mk, "")) if mk else None, rest]
        return " ".join(p for p in parts if p)

    def lines(self) -> list[str]:
        out = []
        for raw in self._text().splitlines():
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
                out.append(self.render(obj) if isinstance(obj, dict) else raw)
            except json.JSONDecodeError:
                self.bad_lines += 1
                out.append(raw)
        return out


class CsvLog(LogSource):
    def lines(self) -> list[str]:
        rows = list(csv.DictReader(io.StringIO(self._text())))
        if not rows:
            return []
        tk = self.cfg.fields.get("time") or next((k for k in rows[0] if k.lower() in TIME_KEYS), next(iter(rows[0])))
        out = []
        for row in rows:
            ts = _iso(row.get(tk), self.cfg.time_format)
            if ts is None:
                self.bad_lines += 1
            rest = " ".join(f"{k}={v}" for k, v in row.items() if k != tk and k not in self.cfg.drop and k is not None)
            out.append(f"{ts or row.get(tk, '-')} {rest}")
        return out


class CommandLog(LogSource):
    def exists(self) -> bool:
        return bool(self.cfg.run)

    def _text(self) -> str:
        if not self.cfg.run:
            raise ValueError(f"{self.name}: the command adapter needs 'run'")
        try:
            r = subprocess.run(shlex.split(self.cfg.run), capture_output=True, text=True, timeout=self.cfg.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValueError(f"{self.name}: could not run {self.cfg.run!r}: {e}") from None
        if r.returncode != 0:
            raise ValueError(f"{self.name}: {self.cfg.run!r} exited {r.returncode}: {r.stderr.strip()[:200]}")
        return r.stdout


ADAPTERS = {"text": TextLog, "jsonl": JsonLinesLog, "csv": CsvLog, "command": CommandLog}


def logs_for(cfg: ServiceConfig) -> dict[str, LogSource]:
    return {l.name: ADAPTERS[l.adapter](l, cfg) for l in cfg.logs}
