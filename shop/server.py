"""ShopFast: the live store whose checkout-api Argus watches and fixes.

    python -m shop.server            # http://localhost:8100

It runs the code in scenarios/live_shop/repo (a real git repo) with simulated customers plus any real clicks,
and writes real telemetry to scenarios/live_shop/logs: app.log (every request), metrics.log (every 15 s),
db.log (connection pool stats) and deploy.log. Every new commit on the repo is deployed within a couple of
seconds, like a CI/CD pipeline. When the 5xx rate stays above 5% it sends a Prometheus Alertmanager webhook
to Argus (ARGUS_URL, default http://localhost:8000), which opens an incident and investigates this exact repo.
"""
from __future__ import annotations

import collections
import hashlib
import importlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = ROOT / "scenarios" / "live_shop"
REPO, LOGS = SCENARIO / "repo", SCENARIO / "logs"
ARGUS_URL = os.getenv("ARGUS_URL", "http://localhost:8000").rstrip("/")
RPS = float(os.getenv("SHOP_RPS", "4"))      # simulated customers' requests per second
DECLINE_RATE = 0.25                          # share of card payments the bank declines
ALERT_THRESHOLD, ALERT_WINDOW = 0.05, 30     # 5xx rate over the last 30 s
sys.dont_write_bytecode = True               # keep __pycache__ out of the live repo

PRODUCTS = [
    {"id": "p1", "name": "Trail running shoes", "price": 4999, "tint": "#ff8a65"},
    {"id": "p2", "name": "Noise-cancelling headphones", "price": 8999, "tint": "#7e8cff"},
    {"id": "p3", "name": "Insulated water bottle", "price": 899, "tint": "#4dd0a8"},
    {"id": "p4", "name": "Mechanical keyboard", "price": 5499, "tint": "#ffca5f"},
    {"id": "p5", "name": "Cotton hoodie", "price": 1799, "tint": "#c792ea"},
    {"id": "p6", "name": "Desk lamp", "price": 1299, "tint": "#64b5f6"},
]


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime, ms: bool = True) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S") + (f".{t.microsecond // 1000:03d}Z" if ms else "Z")


def git(*args: str, env: dict | None = None) -> str:
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout.strip()


class Logs:
    """Append-only log files. Lines are only ever added, so line numbers Argus cites stay valid."""

    def __init__(self):
        self.lock = threading.Lock()

    def write(self, name: str, line: str) -> None:
        with self.lock, open(LOGS / name, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")


logs = Logs()


class RepoLogHandler(logging.Handler):
    """The repo's own logging (e.g. 'payment declined') lands in app.log like in production."""

    def emit(self, record):
        logs.write("app.log", f"{iso(now())} {record.levelname:<5} {record.name} {record.getMessage()}")


class Release:
    """One deployed version of the repo: its modules and a fresh connection pool."""

    def __init__(self, sha: str):
        self.sha, self.error = sha, None
        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        importlib.invalidate_caches()
        try:
            self.config = importlib.import_module("app.config")
            self.db = importlib.import_module("app.db")
            self.payments = importlib.import_module("app.payments")
            self.checkout = importlib.import_module("app.checkout")
            self.history = importlib.import_module("app.history")
            self.pool = self.db.ConnectionPool(size=self.config.POOL_SIZE, timeout=self.config.POOL_TIMEOUT)
        except Exception as e:  # a commit that doesn't even import: every request fails, like a crash-looping pod
            self.error = f"{type(e).__name__}: {e}"
            self.pool = None


class Bank:
    """Stand-in for the payment provider: a little latency, and some cards are declined."""

    def __init__(self, release: Release, force_decline: bool = False):
        self.result = release.payments.ChargeResult
        self.force_decline = force_decline

    def charge(self, card_token, amount):
        time.sleep(random.uniform(.02, .07))
        if self.force_decline or card_token == "tok_declined" or random.random() < DECLINE_RATE:
            return self.result(ok=False, reason="card_declined")
        return self.result(ok=True)


class Shop:
    def __init__(self):
        self.release: Release | None = None
        self.requests = collections.deque(maxlen=20000)   # (time, path, status, ms)
        self.series = collections.deque(maxlen=120)       # one point per 5 s for the chart
        self.alert = {"firing": False, "since": None, "incident": None, "error": None, "bad_checks": 0, "good_since": None}
        self.orders = 0
        self.seq = 0
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="req")
        self.deployed_at = None
        self.deploys = collections.deque(maxlen=20)  # for the chart markers
        self.resetting = False

    # ---- deploys -------------------------------------------------------------
    def deploy_if_changed(self) -> None:
        sha = git("rev-parse", "HEAD")
        if self.release and sha == self.release.sha:
            return
        old = self.release.sha if self.release else None
        author = git("log", "-1", "--format=%an", sha).split()[0].lower()
        try:
            changes = git("rev-list", "--reverse", f"{old}..{sha}").split() if old else [sha]
        except RuntimeError:  # the old commit is gone (the repo was rebuilt by a reset)
            changes = [sha]
        t = now()
        logs.write("deploy.log", f"{iso(t, ms=False)} DEPLOY checkout-api commit={sha[:7]} deployer={author} status=started "
                                 f"changes={','.join(c[:7] for c in changes)}")
        release = Release(sha)
        if release.error:
            logs.write("app.log", f"{iso(now())} ERROR app failed to start commit={sha[:7]} error=\"{release.error}\"")
        else:
            logs.write("app.log", f"{iso(now())} INFO  app starting checkout-api commit={sha[:7]}")
            logs.write("app.log", f"{iso(now())} INFO  db pool initialised size={release.config.POOL_SIZE} timeout={release.config.POOL_TIMEOUT}s")
        self.release, self.deployed_at = release, now()
        meta = git("log", "-1", "--format=%an%x1f%s", sha).split("\x1f")
        self.deploys.append({"t": iso(self.deployed_at), "sha": sha[:7], "author": meta[0], "message": meta[1], "ok": not release.error})
        logs.write("deploy.log", f"{iso(now(), ms=False)} DEPLOY checkout-api commit={sha[:7]} deployer={author} "
                                 f"status={'failed' if release.error else 'succeeded'} replicas=1/1")

    def watch_deploys(self) -> None:
        while True:
            try:
                if not self.resetting and not (REPO / ".git").exists():  # the repo was lost: rebuild the healthy version
                    print("deploy watcher: the shop's git repo is missing, rebuilding it", file=sys.stderr)
                    self.reset()
                elif not self.resetting:
                    self.deploy_if_changed()
            except Exception as e:
                print("deploy watcher:", e, file=sys.stderr)
            time.sleep(2)

    # ---- requests ------------------------------------------------------------
    def handle(self, path: str, card: str = "tok_visa", product: dict | None = None) -> dict:
        release, t0 = self.release, time.perf_counter()
        with self.lock:
            self.seq += 1
            rid = f"r{self.seq:06d}"
        status, detail, out = 200, "", {}
        try:
            if release.error:
                raise RuntimeError(f"service failed to start: {release.error}")
            if path == "/checkout":
                order = {"order_id": f"o{rid}", "cart_id": f"c{rid}", "card_token": card}
                out = release.checkout.process_checkout(release.pool, Bank(release, card == "tok_declined"), order)
                status, detail = out["status"], out.get("error", "")
            elif path == "/orders/history":
                out = release.history.order_history(release.pool, f"cust{random.randint(1, 400)}")
                status = out["status"]
            else:  # /catalog: no database involved
                time.sleep(random.uniform(.002, .012))
        except Exception as e:
            db = getattr(release, "db", None)
            status, detail = (503 if db and isinstance(e, db.DBConnectionTimeout) else 500), f"{type(e).__name__}: {e}"
        ms = int((time.perf_counter() - t0) * 1000)
        method = "POST" if path == "/checkout" else "GET"
        line = f"{iso(now())} {'ERROR' if status >= 500 else 'INFO ':<5} http method={method} path={path} status={status} duration_ms={ms} request_id={rid}"
        logs.write("app.log", line + (f' error="{detail}"' if status >= 500 else ""))
        with self.lock:
            self.requests.append((time.time(), path, status, ms))
            if path == "/checkout" and status == 200:
                self.orders += 1
        return {"status": status, "detail": detail, "request_id": rid, "ms": ms}

    def customers(self) -> None:
        while True:
            r = random.random()
            path = "/checkout" if r < .6 else "/catalog" if r < .9 else "/orders/history"
            self.pool.submit(self.handle, path)
            time.sleep(random.expovariate(RPS))

    # ---- telemetry -----------------------------------------------------------
    def window(self, seconds: float) -> list[tuple]:
        cutoff = time.time() - seconds
        with self.lock:
            return [r for r in self.requests if r[0] >= cutoff]

    @staticmethod
    def summarise(rows: list[tuple], seconds: float) -> dict:
        n = len(rows)
        errors = sum(1 for r in rows if r[2] >= 500)
        ms = sorted(r[3] for r in rows)
        return {"rps": n / seconds, "error_rate": errors / n if n else 0.0, "p95_ms": ms[int(.95 * (n - 1))] if n else 0,
                "orders": sum(1 for r in rows if r[1] == "/checkout" and r[2] == 200)}

    def pool_stats(self) -> tuple[int, int]:
        rel = self.release
        return (rel.pool.in_use, rel.pool.size) if rel and rel.pool else (0, 0)

    def telemetry(self) -> None:
        tick = 0
        while True:
            time.sleep(5)
            tick += 1
            s = self.summarise(self.window(5), 5)
            self.series.append({"t": iso(now()), "rps": round(s["rps"], 2), "error_rate": round(s["error_rate"], 3)})
            self.check_alert()
            if tick % 3:
                continue
            s = self.summarise(self.window(15), 15)
            in_use, size = self.pool_stats()
            t = now()
            logs.write("metrics.log", f"{iso(t, ms=False)} service=checkout-api rps={s['rps']:.1f} error_rate_5xx={s['error_rate']:.2f} "
                                      f"p95_ms={s['p95_ms']} pool_in_use={in_use} pool_size={size} "
                                      f"commit={self.release.sha[:7] if self.release else '-'} restarts=0")
            db_rows = [r for r in self.window(15) if r[1] != "/catalog" and r[2] < 500]
            logs.write("db.log", f"{t:%Y-%m-%d %H:%M:%S}.000 UTC [postgres] LOG  stats connections_total={size + 4} "
                                 f"active={min(in_use, 2)} idle_in_transaction={max(0, in_use - 2)} qps={len(db_rows) / 15:.0f} "
                                 f"p95_latency_ms={random.uniform(3, 6):.1f} slow_queries=0 locks_waiting=0")

    # ---- alerting ------------------------------------------------------------
    def check_alert(self) -> None:
        if self.resetting:
            return
        rows = self.window(ALERT_WINDOW)
        s = self.summarise(rows, ALERT_WINDOW)
        a = self.alert
        bad = len(rows) >= 10 and s["error_rate"] > ALERT_THRESHOLD
        if bad:
            a["bad_checks"] += 1
            a["good_since"] = None
            a["since"] = a["since"] or iso(now(), ms=False)
        else:
            a["bad_checks"] = 0
            a["good_since"] = a["good_since"] or time.time()
            if not a["firing"]:
                a["since"] = None
        if bad and a["bad_checks"] >= 2 and (not a["firing"] or (a["error"] and a["bad_checks"] % 3 == 0)):
            self.fire(s["error_rate"])
        if a["firing"] and not bad and time.time() - a["good_since"] >= 30:
            logs.write("app.log", f"{iso(now())} INFO  monitor alert resolved CheckoutErrorRateHigh")
            a.update(firing=False, since=None, error=None)

    def fire(self, rate: float) -> None:
        a = self.alert
        a["firing"] = True
        pct = round(rate * 100)
        body = {"receiver": "argus", "status": "firing", "alerts": [{
            "status": "firing",
            "labels": {"alertname": "CheckoutErrorRateHigh", "service": "checkout-api", "severity": "high", "scenario": "live_shop"},
            "annotations": {"summary": f"ShopFast checkout-api 5xx rate {pct}%", "metric": "error_rate_5xx",
                            "value": round(rate, 3), "threshold": ALERT_THRESHOLD, "window": f"{ALERT_WINDOW}s"},
            "startsAt": a["since"],
            "fingerprint": hashlib.sha1(f"checkout-{a['since']}".encode()).hexdigest()[:12],
        }]}
        logs.write("app.log", f"{iso(now())} WARN  monitor alert firing CheckoutErrorRateHigh error_rate_5xx={rate:.2f}")
        try:
            req = urllib.request.Request(f"{ARGUS_URL}/alerts", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                reply = json.loads(r.read())
            if reply.get("paused"):  # held, not investigated; the alert is re-sent while it keeps firing
                a["incident"], a["error"] = None, "Argus is paused, so no incident was opened"
            else:
                a["incident"], a["error"] = reply, None
        except Exception as e:
            a["error"] = f"it could not reach Argus at {ARGUS_URL} ({e})"

    # ---- demo controls -------------------------------------------------------
    def commits(self, n: int = 6) -> list[dict]:
        out = git("log", f"-{n}", "--format=%h%x1f%an%x1f%aI%x1f%s")
        return [dict(zip(("sha", "author", "date", "message"), line.split("\x1f"))) for line in out.splitlines()]

    def status(self) -> dict:
        s = self.summarise(self.window(30), 30)
        in_use, size = self.pool_stats()
        rel = self.release
        return {"online": True, "healthy": s["error_rate"] <= ALERT_THRESHOLD and not (rel and rel.error),
                "error_rate": round(s["error_rate"], 3), "rps": round(s["rps"], 2), "p95_ms": s["p95_ms"],
                "orders_per_min": s["orders"] * 2, "orders_total": self.orders, "pool_in_use": in_use, "pool_size": size,
                "start_error": rel.error if rel else None, "deployed_at": iso(self.deployed_at) if self.deployed_at else None,
                "commits": self.commits(), "series": list(self.series), "deploys": list(self.deploys),
                "alert": {k: self.alert[k] for k in ("firing", "since", "incident", "error")},
                "broken_change_live": self.bad_change_live(), "argus_url": ARGUS_URL, "argus_agents": self.argus_agents()}

    def argus_agents(self) -> dict | None:
        """What will answer the next alert: Argus's live agents or a replay of a recorded run. Cached for a few seconds,
        since the page polls; None while Argus can't be reached."""
        cached = getattr(self, "_agents", None)
        if cached and time.time() - cached[0] < 5:
            return cached[1]
        try:
            with urllib.request.urlopen(f"{ARGUS_URL}/demo/agents", timeout=2) as r:
                value = json.loads(r.read())
        except Exception:  # noqa: BLE001 - Argus is down or older; the page just doesn't show the mode
            value = None
        self._agents = (time.time(), value)
        return value

    def bad_change_live(self) -> bool:
        from scenarios.live_shop.history import BAD_CHANGE
        try:
            return all((REPO / p).read_text(encoding="utf-8") == c for p, c in BAD_CHANGE["files"].items())
        except OSError:  # mid-reset
            return False

    def push_bad_change(self) -> dict:
        from scenarios.live_shop.history import BAD_CHANGE
        if self.bad_change_live():
            raise HTTPException(409, "the bad change is already live")
        for path, content in BAD_CHANGE["files"].items():
            (REPO / path).write_bytes(content.encode("utf-8"))
        who, email = BAD_CHANGE["author"][:-1].split(" <")
        env = {**os.environ, "GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": email}
        git("add", "-A", env=env)
        git("commit", "-q", "-m", BAD_CHANGE["message"], env=env)
        return self.commit(git("rev-parse", "HEAD"))

    def commit(self, ref: str) -> dict:
        """One commit with its diff, for the 'what was pushed' view."""
        sha, author, date, message = git("log", "-1", "--format=%h%x1f%an%x1f%aI%x1f%s", ref).split("\x1f")
        return {"sha": sha, "author": author, "date": date, "message": message,
                "diff": git("show", "--format=", "--no-color", "--unified=4", ref)}

    def reset(self) -> dict:
        from scenarios.build import build
        # the running release keeps serving while the repo is rebuilt, so a reset never causes errors itself
        self.resetting = True
        try:
            build("live_shop")  # fresh repo with only the good history, and empty logs
            self.deploys.clear()
            self.deploy_if_changed()
            with self.lock:
                self.requests.clear()
                self.series.clear()
            self.alert.update(firing=False, since=None, incident=None, error=None, bad_checks=0, good_since=None)
        finally:
            self.resetting = False
        return {"sha": self.release.sha[:7]}


shop = Shop()
app = FastAPI(title="ShopFast")
logging.getLogger("checkout").addHandler(RepoLogHandler())
logging.getLogger("checkout").setLevel(logging.INFO)


@app.on_event("startup")
def start() -> None:
    if not (REPO / ".git").exists():
        from scenarios.build import build
        build("live_shop")
    LOGS.mkdir(exist_ok=True)
    shop.deploy_if_changed()
    for target in (shop.watch_deploys, shop.customers, shop.telemetry):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()


@app.get("/", include_in_schema=False)
def store_page():
    return HTMLResponse((Path(__file__).parent / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/products")
def products():
    return PRODUCTS


@app.post("/api/checkout")
def buy(body: dict):
    product = next((p for p in PRODUCTS if p["id"] == body.get("product_id")), None)
    if not product:
        raise HTTPException(404, "no such product")
    return {**shop.handle("/checkout", "tok_declined" if body.get("decline") else "tok_visa", product), "product": product["name"]}


@app.get("/api/status")
def status():
    return shop.status()


@app.get("/api/commits/{sha}")
def commit(sha: str):
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        raise HTTPException(422, "not a commit id")
    try:
        return shop.commit(sha)
    except RuntimeError:
        raise HTTPException(404, "no such commit")


@app.post("/admin/break")
def push_bad_change():
    return shop.push_bad_change()


@app.post("/admin/reset")
def reset():
    return shop.reset()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("SHOP_PORT", "8100")), log_level="warning")
