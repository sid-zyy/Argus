"""Service packs: one folder per service that Argus can investigate. The spec is docs/SERVICE_PACK.md.

python -m scenarios.pack new inventory_retries --service inventory-api    # a working starter pack to edit
python -m scenarios.pack check inventory_retries                          # what is missing or malformed
python -m scenarios.pack check                                            # every pack

A new pack is ready to run as soon as it is made: it builds, its sandbox check reproduces the planted bug, and the
dashboard lists it. Replace the starter service, bug and telemetry with your own.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAPS = ROOT / "service_maps"
BUSINESS_KEYS = ("service_label", "unit", "units_per_minute", "value_per_unit", "currency", "failure_share",
                 "manual_mttr_minutes", "engineers_paged", "assumptions")
ALERT_KEYS = ("id", "service", "title", "severity", "started_at", "fired_at")
EXPECTED_KEYS = ("category", "culprit_sha", "trap")


def names(root: Path = ROOT) -> list[str]:
    return sorted(p.parent.name for p in root.glob("*/history.py"))


def _defines(path: Path, *wanted: str) -> list[str]:
    """Which of the wanted top-level names (assignments or functions) the file does NOT define."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return [f"syntax error on line {e.lineno}"]
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return [w for w in wanted if w not in found]


def _json_keys(path: Path, keys: tuple[str, ...]) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return [f"{path.name} is not valid JSON ({e})"]
    missing = [k for k in keys if k not in data]
    return [f"{path.name} is missing {', '.join(missing)}"] if missing else []


def validate(name: str, root: Path = ROOT) -> list[str]:
    """Problems that would stop Argus from running this pack. Empty means it's good to go."""
    d = root / name
    live = (d / "live.json").is_file()
    problems = []
    # a pack with an argus.yaml (Argus Connect) names its own sandbox check files, in any language
    connected = (d / "argus.yaml").is_file()
    for f in ("history.py", "generate_logs.py", "business.json") if connected else ("history.py", "generate_logs.py", "repro.py", "business.json"):
        if not (d / f).is_file():
            problems.append(f"no {f}")
    if connected:
        from connect.config import ConfigError, load
        try:
            cfg = load(d)
            problems += [f"argus.yaml: commands.files: no {f}" for f in cfg.commands.files if not cfg.resolve(f).is_file()]
        except ConfigError as e:
            problems.append(f"argus.yaml: {e}")
    if (d / "history.py").is_file():
        problems += [f"history.py: no {w}" if not w.startswith("syntax") else f"history.py: {w}"
                     for w in _defines(d / "history.py", "COMMITS", "CULPRIT_INDEX")]
    if (d / "generate_logs.py").is_file():
        problems += [f"generate_logs.py: no {w}() function" if not w.startswith("syntax") else f"generate_logs.py: {w}"
                     for w in _defines(d / "generate_logs.py", "generate")]
    if (d / "repro.py").is_file() and "sys.argv" not in (d / "repro.py").read_text(encoding="utf-8"):
        problems.append("repro.py must take the repo folder as its argument (sys.argv[1])")
    if (d / "business.json").is_file():
        problems += _json_keys(d / "business.json", BUSINESS_KEYS)
    # alert.json and expected.json are written by generate_logs.py, so they only exist once the pack is built
    if (d / "alert.json").is_file():
        problems += _json_keys(d / "alert.json", ALERT_KEYS)
    if (d / "expected.json").is_file() and not live:
        problems += _json_keys(d / "expected.json", EXPECTED_KEYS)
    return problems


# ---- the starter pack ----------------------------------------------------------------------------------------------
# A small stock-lookup service whose planted bug is a settings change: retries turned off, so brief backend hiccups
# become customer-facing errors. The misleading clue is "the stock database is overloaded".
HISTORY = '''"""Commit history for __SERVICE__. scenarios/build.py turns this into repo/.

Commit 3 is the culprit: it sets MAX_RETRIES to 0 ("fail fast"). The stock backend has always had brief hiccups that a
retry hides; without retries about one lookup in five now fails. The backend itself is no busier than usual.
"""

CONFIG_V1 = """# How many times a stock lookup is retried after a brief backend hiccup.
MAX_RETRIES = 3
BACKEND_URL = "https://stock.internal"
"""

CONFIG_V2 = CONFIG_V1.replace("MAX_RETRIES = 3", "MAX_RETRIES = 0")

STOCK_PY = """\\"\\"\\"Stock lookups with retries on brief backend hiccups.\\"\\"\\"
from app import config


class TransientError(Exception):
    pass


def lookup(sku, backend):
    attempts = config.MAX_RETRIES + 1
    for attempt in range(attempts):
        try:
            return {"status": 200, "sku": sku, "in_stock": backend.get(sku)}
        except TransientError:
            if attempt == attempts - 1:
                return {"status": 503, "error": "stock_unavailable"}
"""

TEST_PY = """from app.stock import lookup


class Backend:
    def get(self, sku):
        return 5


def test_lookup_happy_path():
    assert lookup("sku-1", Backend())["status"] == 200
"""

README_V1 = "# __SERVICE__\\n\\nTells the storefront whether an item is in stock.\\n"
README_V2 = README_V1 + "\\n## On call\\n\\nRunbook: https://wiki.internal/__SERVICE__\\n"

COMMITS = [
    ("2026-10-12T10:00:00+00:00", "Priya Nair <priya@example.com>", "Initial __SERVICE__", {
        "app/__init__.py": "", "app/config.py": CONFIG_V1, "app/stock.py": STOCK_PY, "tests/test_stock.py": TEST_PY,
        "README.md": README_V1,
    }),
    ("2026-10-20T11:00:00+00:00", "Arjun Mehta <arjun@example.com>", "Add on-call runbook link", {"README.md": README_V2}),
    ("2026-10-22T10:05:00+00:00", "Dev Raman <dev@example.com>", "Fail fast: stop retrying stock lookups", {"app/config.py": CONFIG_V2}),
]

CULPRIT_INDEX = 2
'''

GENERATE = '''"""Deterministic synthetic telemetry for __SERVICE__ (retries switched off).

The loudest signal is TransientError from the stock backend, which points at the backend (the trap). The backend's own
error rate is the same all morning; what changed is that a hiccup is no longer retried.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 22, 10, 25, 0, tzinfo=timezone.utc)
DEPLOY_AT = datetime(2026, 10, 22, 10, 10, 0, tzinfo=timezone.utc)
ALERT_AT = datetime(2026, 10, 22, 10, 16, 0, tzinfo=timezone.utc)
RPS, HICCUP = 4, 0.2  # requests per second; share of backend calls that hiccup (unchanged all morning)
SERVICE = "__SERVICE__"


def _ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def generate(out_dir: Path, shas: list[str], culprit_index: int) -> dict:
    rng = random.Random(2026)
    logs = out_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    short = [s[:7] for s in shas]
    app = [f"2026-10-20T11:05:00.000Z INFO  app starting {SERVICE} version=2.1.0 commit={short[1]} MAX_RETRIES=3"]
    retries, deployed, buckets, backend, req = 3, False, {}, {}, 1000
    t = START
    while t < END:
        if not deployed and t >= DEPLOY_AT:
            deployed, retries = True, 0
            app.append(f"{_ts(t)} INFO  app starting {SERVICE} version=2.2.0 commit={short[2]} MAX_RETRIES=0")
        b = t.replace(second=(t.second // 30) * 30, microsecond=0)
        w = buckets.setdefault(b, {"req": 0, "err": 0})
        m = backend.setdefault(t.replace(second=0, microsecond=0), [0, 0])
        for i in range(RPS):
            at = t + timedelta(milliseconds=int(1000 * i / RPS) + rng.randint(0, 120))
            req += 1
            w["req"] += 1
            ok = False
            for attempt in range(retries + 1):
                m[0] += 1
                if rng.random() >= HICCUP:
                    ok = True
                    break
                m[1] += 1
                app.append(f"{_ts(at)} WARN  stock TransientError: backend stock.internal busy attempt={attempt + 1} request_id=r{req:06d}")
            if ok:
                app.append(f"{_ts(at)} INFO  http method=GET path=/stock status=200 request_id=r{req:06d}")
            else:
                w["err"] += 1
                app.append(f"{_ts(at)} ERROR http method=GET path=/stock status=503 error=stock_unavailable request_id=r{req:06d}")
        t += timedelta(seconds=1)
    metrics, series = [], []
    for b, w in sorted(buckets.items()):
        rate = w["err"] / w["req"]
        series.append((b, rate))
        metrics.append(f"{b.strftime('%Y-%m-%dT%H:%M:%SZ')} service={SERVICE} rps={w['req'] / 30:.1f} error_rate_5xx={rate:.2f} restarts=0")
    # the backend's own view: the same share of hiccups before and after the deploy
    metrics += [f"{m.strftime('%Y-%m-%dT%H:%M:%SZ')} service=stock-backend calls={c} transient_errors={e} transient_rate={e / c:.2f} cpu_pct={rng.randint(30, 38)}"
                for m, (c, e) in sorted(backend.items())]
    metrics.sort()
    deploy = [f"2026-10-20T11:04:00Z DEPLOY {SERVICE} version=2.1.0 commit={short[1]} status=succeeded changes={short[1]}",
              f"2026-10-22T10:09:30Z DEPLOY {SERVICE} version=2.2.0 commit={short[2]} status=started changes={short[2]}",
              f"2026-10-22T10:10:00Z DEPLOY {SERVICE} version=2.2.0 commit={short[2]} status=succeeded replicas=1/1"]
    app.sort(key=lambda line: line[:24])
    for name, lines in (("app.log", app), ("metrics.log", metrics), ("deploy.log", deploy)):
        (logs / name).write_text("\\n".join(lines) + "\\n", encoding="utf-8")
    onset = next((b for b, r in series if r > .05), None)
    recent = [r for b, r in series if ALERT_AT - timedelta(minutes=2) <= b < ALERT_AT]
    value = round(sum(recent) / len(recent), 2) if recent else 0.0
    alert = {"id": "ALERT-9001", "service": SERVICE, "title": f"{SERVICE} 5xx rate {value:.0%}", "severity": "sev2",
             "metric": "error_rate_5xx", "value": value, "threshold": 0.05, "window": "2m",
             "started_at": onset.strftime("%Y-%m-%dT%H:%M:%SZ") if onset else None,
             "fired_at": ALERT_AT.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "synthetic-monitoring"}
    (out_dir / "alert.json").write_text(json.dumps(alert, indent=2) + "\\n", encoding="utf-8")
    expected = {"category": "config", "culprit_sha": short[culprit_index], "file": "app/config.py",
                "trap": "the stock database is overloaded", "fix_hint": f"set MAX_RETRIES back to 3, or roll back to {short[1]}"}
    (out_dir / "expected.json").write_text(json.dumps(expected, indent=2) + "\\n", encoding="utf-8")
    return {"alert": alert}
'''

REPRO = '''"""Sandbox check for __SERVICE__: replay stock lookups against a backend with its usual brief hiccups.

Usage: python repro.py <repo_dir>. Exit 0 = healthy, 1 = incident reproduced.
"""
import json
import random
import sys

sys.path.insert(0, sys.argv[1])

from app import config  # noqa: E402
from app.stock import TransientError, lookup  # noqa: E402


class Backend:
    def __init__(self):
        self.rng = random.Random(7)

    def get(self, sku):
        if self.rng.random() < 0.2:
            raise TransientError("backend busy")
        return 5


backend, requests, failed = Backend(), 500, 0
for i in range(requests):
    failed += lookup(f"sku-{i}", backend)["status"] != 200
healthy = failed / requests <= 0.01
print(json.dumps({"requests": requests, "failed": failed, "max_retries": config.MAX_RETRIES, "healthy": healthy}))
sys.exit(0 if healthy else 1)
'''


def _business(service: str) -> dict:
    return {"service_label": "Stock checks", "unit": "orders", "units_per_minute": 15, "value_per_unit": 1200, "currency": "INR",
            "failure_share": "alert", "manual_mttr_minutes": 60, "engineers_paged": 2, "engineer_cost_per_hour": 1500,
            "assumptions": [f"{service} answers every add-to-cart; 15 orders per minute at ₹1,200 each depend on it",
                            "Failed share of orders equals the alert's 5xx rate",
                            "A typical manual fix for a bad settings change takes about 60 minutes with 2 engineers paged",
                            "An engineer's time costs about ₹1,500 an hour, fully loaded"]}


def _service_map(service: str) -> dict:
    return {"about": "How the service fits together. Edges point from a dependency to what depends on it.",
            "team": "Your team",
            "nodes": [
                {"id": "backend", "label": "Stock backend", "kind": "data", "col": 0, "code": [], "signals": ["TransientError", "busy"],
                 "note": "Has brief hiccups that a retry hides"},
                {"id": "config", "label": "Retry settings", "kind": "infra", "col": 1, "code": ["app/config.py"], "signals": ["MAX_RETRIES"],
                 "note": "How often a lookup is retried"},
                {"id": "svc", "label": service, "kind": "service", "col": 2, "code": ["app/stock.py"], "signals": ["5xx", "status=503", "error_rate"],
                 "note": "Answers 'is this in stock?'"},
                {"id": "cart", "label": "Add to cart", "kind": "frontend", "col": 3, "signals": [], "note": "Asks for stock first"},
                {"id": "orders", "label": "Orders", "kind": "business", "col": 4, "signals": [], "metric": "affected", "note": "Orders that can't start"},
                {"id": "revenue", "label": "Revenue", "kind": "business", "col": 5, "signals": [], "metric": "at_risk", "note": "Money from lost orders"},
            ],
            "edges": [["backend", "svc"], ["config", "svc"], ["svc", "cart"], ["cart", "orders"], ["orders", "revenue"]]}


def new(name: str, service: str, root: Path = ROOT, maps: Path | None = None) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("the pack name must be a Python identifier in lower case, e.g. inventory_retries")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", service):
        raise ValueError("the service name must be lower case with dashes, e.g. inventory-api")
    d = root / name
    if d.exists():
        raise FileExistsError(f"{d} already exists")
    d.mkdir(parents=True)
    fill = lambda s: s.replace("__SERVICE__", service)  # noqa: E731
    (d / "__init__.py").write_text("", encoding="utf-8")
    (d / "history.py").write_text(fill(HISTORY), encoding="utf-8")
    (d / "generate_logs.py").write_text(fill(GENERATE), encoding="utf-8")
    (d / "repro.py").write_text(fill(REPRO), encoding="utf-8")
    (d / "business.json").write_text(json.dumps(_business(service), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    maps = maps or root / "service_maps"
    if not (maps / f"{service}.json").exists():
        maps.mkdir(parents=True, exist_ok=True)
        (maps / f"{service}.json").write_text(json.dumps(_service_map(service), indent=2) + "\n", encoding="utf-8")
    return d


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scenarios.pack", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="create a starter pack")
    n.add_argument("name")
    n.add_argument("--service", required=True)
    n.add_argument("--no-build", action="store_true", help="don't build the repo and logs yet")
    c = sub.add_parser("check", help="validate one pack or all of them")
    c.add_argument("name", nargs="?")
    a = ap.parse_args(argv)
    if a.cmd == "new":
        d = new(a.name, a.service)
        print(f"Created {d}")
        if not a.no_build:
            from scenarios.build import build
            info = build(a.name)
            print(f"Built repo ({', '.join(info['shas'])}) and logs; alert: {info['alert']['title']}")
        print(f"Next: edit the files (docs/SERVICE_PACK.md), then try it: python -m orchestrator.main scenarios/{a.name} -v")
        return 0
    bad = 0
    for name in [a.name] if a.name else names():
        problems = validate(name)
        bad += bool(problems)
        print(f"{'FAIL' if problems else ' ok '} {name}" + "".join(f"\n     - {p}" for p in problems))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
