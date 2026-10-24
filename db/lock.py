"""One Argus server per incident database.

A second server on the same incidents.db would run Store.mark_interrupted() at startup and escalate every incident the
first server is still working on. So the server holds an OS lock on '<db>.lock' for as long as it runs; a second one
is refused with the first one's pid and start time. The OS drops the lock when the process exits, even after a crash.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOCK_OFFSET = 4096  # the locked byte sits past the info text, so others can still read who holds the lock
_held: dict[str, int] = {}  # resolved lock path -> fd, so reloading the app module in one process is fine


class DatabaseInUse(RuntimeError):
    pass


def lock_path(db_path: str | Path) -> Path:
    return Path(f"{Path(db_path).resolve()}.lock")


def _try_lock(fd: int) -> bool:
    os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def holder(db_path: str | Path) -> dict | None:
    """Who last took the lock (pid, start time); None if nobody ever did."""
    try:
        text = lock_path(db_path).read_text(encoding="utf-8").strip()
        return json.loads(text) if text else None
    except (OSError, ValueError):
        return None


def is_locked(db_path: str | Path) -> bool:
    """True when some process (this one included) holds the lock right now."""
    p = lock_path(db_path)
    if str(p) in _held:
        return True
    if not p.exists():
        return False
    fd = os.open(p, os.O_RDWR)
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


def _unlock(fd: int) -> None:
    os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def hold(db_path: str | Path, note: str = "") -> None:
    """Take the lock for the life of this process, or raise DatabaseInUse."""
    p = lock_path(db_path)
    if str(p) in _held:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT)
    if not _try_lock(fd):
        os.close(fd)
        who = holder(db_path) or {}
        raise DatabaseInUse(
            f"Another Argus server (pid {who.get('pid', '?')}, started {who.get('started', '?')}) is already using {Path(db_path).resolve()}. "
            "Starting a second one would escalate its open incidents. Stop it first, or point this one at another database "
            "with INCIDENT_DB=<path>.")
    info = json.dumps({"pid": os.getpid(), "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "note": note})
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, info.encode().ljust(256))  # fixed width: never reaches the locked byte, never leaves an old tail behind
    _held[str(p)] = fd
