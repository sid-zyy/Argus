"""Pre-demo self-check: everything Argus needs, each as ok / warn / bad with what to do about it.

python -m ui.health          # from a terminal, with or without the server running
GET /health                  # the same checks from the running server (the System check page shows them)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from db import lock as db_lock
from llm import PROVIDERS
from orchestrator import policy
from scenarios import pack
from tools.sandbox import docker_available

ROOT = Path(__file__).resolve().parent.parent


def _check(name: str, status: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def database(db_path: Path, store=None, in_server: bool = False) -> list[dict]:
    out = []
    if in_server:
        out.append(_check("Database", "ok", f"{db_path.name}, locked to this server so a second one can't escalate its incidents"))
    elif db_lock.is_locked(db_path):
        who = db_lock.holder(db_path) or {}
        out.append(_check("Database", "ok", f"In use by a running Argus server (pid {who.get('pid', '?')}). A second server would be refused."))
    else:
        out.append(_check("Database", "ok", f"{db_path.name} is free; the next server will take it"))
    if store is not None:
        v = store.verify_chain()
        out.append(_check("Audit trail", "ok" if v["ok"] else "bad",
                          f"{v['events']} events, hash chain intact (head {v['head']})" if v["ok"]
                          else f"Event {v['first_bad']['id']} (INC-{v['first_bad']['incident_id']}): {v['first_bad']['problem']}",
                          "" if v["ok"] else "Someone edited the database directly. Keep a copy for review before changing anything."))
    return out


def ai() -> list[dict]:
    order = [os.getenv("LLM_PROVIDER", "groq")] + [p.strip() for p in os.getenv("LLM_FALLBACKS", "gemini,openrouter").split(",") if p.strip()]
    keyed = [p for p in dict.fromkeys(order) if p in PROVIDERS and (PROVIDERS[p]["key_env"] is None or os.getenv(PROVIDERS[p]["key_env"]))]
    keyed = [p for p in keyed if p != "ollama"]  # ollama needs no key, so it says nothing about readiness
    if not keyed:
        return [_check("AI providers", "bad", "No API key for any provider in LLM_PROVIDER / LLM_FALLBACKS",
                       "Add GROQ_API_KEY or GEMINI_API_KEY to .env. Replays still work without one.")]
    status = "ok" if len(keyed) > 1 else "warn"
    return [_check("AI providers", status, f"Keys for {', '.join(keyed)} (tried in that order)",
                   "" if status == "ok" else "Add a second provider's key so a free-tier rate limit can fail over.")]


_docker = {"at": 0.0, "up": False}


def _docker_up() -> bool:
    """`docker info` can take seconds, so the answer is kept for a minute."""
    if time.time() - _docker["at"] > 60:
        _docker.update(at=time.time(), up=docker_available())
    return _docker["up"]


def sandbox() -> list[dict]:
    mode = os.getenv("SANDBOX_MODE", "auto")
    if mode == "docker" or (mode == "auto" and _docker_up()):
        return [_check("Sandbox", "ok", "Docker: no network, CPU and memory limits")]
    return [_check("Sandbox", "warn", "Subprocess in a temp copy (Docker isn't running): scrubbed environment and a timeout, but no network isolation",
                   "Start Docker Desktop and run: docker build -t incident-commander-sandbox sandbox/")]


def scenarios() -> list[dict]:
    out = []
    for name in pack.names():
        problems = pack.validate(name)
        built = (ROOT / "scenarios" / name / "repo" / ".git").exists()
        live = (ROOT / "scenarios" / name / "live.json").is_file()
        replay = (ROOT / "replay" / f"{name}.json").is_file()
        if problems:
            out.append(_check(f"Service pack {name}", "bad", "; ".join(problems), f"See docs/SERVICE_PACK.md, then: python -m scenarios.pack check {name}"))
        elif not live and not replay:
            out.append(_check(f"Service pack {name}", "warn", "Valid, but no recorded replay, so it needs a live AI run",
                              f"python -m orchestrator.main scenarios/{name} --save-replay"))
        else:
            notes = ["valid", "repo built" if built else "repo builds on first use"] + (["recorded replay"] if replay else []) + (["live app"] if live else [])
            out.append(_check(f"Service pack {name}", "ok", ", ".join(notes)))
    return out


def shop(url: str) -> list[dict]:
    try:
        u = urllib.parse.urlsplit(url)
        # a quick port probe first: on Windows a refused connection otherwise takes about 2 s per address
        socket.create_connection((u.hostname, u.port or 80), timeout=0.4).close()
        with urllib.request.urlopen(f"{url}/api/status", timeout=2) as r:
            s = json.loads(r.read())
        return [_check("ShopFast", "ok" if s.get("healthy") else "warn",
                       f"Running at {url}, {'healthy' if s.get('healthy') else 'checkout failing'}, live commit {s['commits'][0]['sha']}",
                       "" if s.get("healthy") else "Expected during the demo. Otherwise click Reset on the dashboard.")]
    except Exception:  # noqa: BLE001 - any failure means it can't be reached
        return [_check("ShopFast", "warn", f"Not reachable at {url}", "Start it: python -m shop.server (or python run_demo.py)")]


def governance() -> list[dict]:
    try:
        s = policy.summary()
    except Exception as e:  # noqa: BLE001 - a broken policy file must show up here, not crash the page
        return [_check("Policy", "bad", f"policy.yaml could not be read: {e}", "Fix the YAML; until then the built-in defaults apply.")]
    two = "two people for serious incidents" if s["two_person"] else "one approver per fix"
    return [_check("Policy", "ok", f"{s['file']} (fingerprint {s['sha']}): {two}, {len(s['rules'])} rules")]


def run(db_path: Path, store=None, in_server: bool = False, shop_url: str | None = None) -> dict:
    checks = (database(db_path, store, in_server) + governance() + ai() + sandbox() + scenarios()
              + shop(shop_url or os.getenv("SHOP_URL", "http://localhost:8100").rstrip("/")))
    worst = "bad" if any(c["status"] == "bad" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"status": worst, "checks": checks}


def main() -> int:
    db = Path(os.getenv("INCIDENT_DB", ROOT / "incidents.db"))
    url = os.getenv("ARGUS_URL", "http://localhost:8000").rstrip("/")
    try:  # a running server can also check the audit trail, which needs the database
        with urllib.request.urlopen(f"{url}/health", timeout=20) as r:
            report = json.loads(r.read())
        print(f"Checked by the Argus server at {url}\n")
    except Exception:  # noqa: BLE001 - no server: check what can be checked from here
        report = run(db)
        print(f"No Argus server at {url}; checking from this terminal (the audit trail is checked by the server)\n")
    mark = {"ok": "[ ok ]", "warn": "[warn]", "bad": "[FAIL]"}
    for c in report["checks"]:
        print(f"{mark[c['status']]} {c['name']}: {c['detail']}")
        if c["fix"]:
            print(f"       -> {c['fix']}")
    print({"ok": "\nReady for the demo.", "warn": "\nReady, with warnings.", "bad": "\nNot ready: fix the FAIL lines first."}[report["status"]])
    return 1 if report["status"] == "bad" else 0


if __name__ == "__main__":
    sys.exit(main())
