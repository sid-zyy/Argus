"""Deterministic synthetic telemetry for incident 1 (connection leak after deploy 1.8.3).

The loudest signal is DBConnectionTimeout, which points at the database (the trap).
The database itself stays healthy; the pool fills up only after the deploy, one
connection per declined payment.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 10, 22, 14, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 22, 14, 12, 0, tzinfo=timezone.utc)
DEPLOY_AT = datetime(2026, 10, 22, 14, 5, 12, tzinfo=timezone.utc)
ALERT_AT = datetime(2026, 10, 22, 14, 9, 0, tzinfo=timezone.utc)
POOL_SIZE = 20
RPS = 5
DECLINE_RATE = 0.12
# (method, path, uses_db, share)
ENDPOINTS = [
    ("POST", "/checkout", True, 0.30),
    ("GET", "/orders/history", True, 0.08),
    ("GET", "/catalog", False, 0.50),
    ("GET", "/health", False, 0.12),
]


def _ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _pick(rng):
    r = rng.random()
    acc = 0.0
    for ep in ENDPOINTS:
        acc += ep[3]
        if r < acc:
            return ep
    return ENDPOINTS[-1]


def generate(out_dir: Path, shas: list[str], culprit_index: int) -> dict:
    rng = random.Random(42)
    logs = out_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    short = [s[:7] for s in shas]
    app_lines, db_lines, metric_lines = [], [], []
    windows = {}  # 30s bucket -> counters
    leaked = 0
    deployed = False
    first_exhausted = None
    req = 1000

    app_lines.append(f"{_ts(START - timedelta(minutes=50))} INFO  app starting checkout-api version=1.8.2 commit={short[2]}")
    app_lines.append(f"{_ts(START - timedelta(minutes=50))} INFO  db pool initialised size={POOL_SIZE} timeout=5.0s")

    t = START
    while t < END:
        if not deployed and t >= DEPLOY_AT:
            deployed = True
            leaked = 0
            app_lines.append(f"{_ts(t)} INFO  app received SIGTERM, draining connections (deploy)")
            app_lines.append(f"{_ts(t + timedelta(milliseconds=900))} INFO  app starting checkout-api version=1.8.3 commit={short[4]}")
            app_lines.append(f"{_ts(t + timedelta(milliseconds=950))} INFO  db pool initialised size={POOL_SIZE} timeout=5.0s")
        bucket = t.replace(second=(t.second // 30) * 30, microsecond=0)
        w = windows.setdefault(bucket, {"req": 0, "err": 0, "dur": [], "db_q": 0})
        for i in range(RPS):
            at = t + timedelta(milliseconds=int(1000 * i / RPS) + rng.randint(0, 150))
            req += 1
            rid = f"r{req:06d}"
            method, path, uses_db, _ = _pick(rng)
            w["req"] += 1
            if uses_db and leaked >= POOL_SIZE:
                first_exhausted = first_exhausted or at
                w["err"] += 1
                w["dur"].append(5003)
                app_lines.append(f"{_ts(at)} ERROR db request_id={rid} path={path} DBConnectionTimeout: pool exhausted (size={POOL_SIZE}, waited 5.0s)")
                app_lines.append(f"{_ts(at)} INFO  http method={method} path={path} status=503 duration_ms=5003 request_id={rid}")
                continue
            dur = rng.randint(30, 70) if uses_db else rng.randint(3, 12)
            status = 200
            if uses_db:
                w["db_q"] += 3
            if path == "/checkout" and rng.random() < DECLINE_RATE:
                status = 402
                reason = rng.choice(["card_declined", "insufficient_funds", "expired_card"])
                if deployed:
                    leaked += 1
                    app_lines.append(f"{_ts(at)} INFO  checkout payment declined order=o{req} reason={reason}")
                    if leaked >= 15:
                        app_lines.append(f"{_ts(at)} WARN  db pool usage high in_use={min(leaked + 1, POOL_SIZE)}/{POOL_SIZE}")
            w["dur"].append(dur)
            app_lines.append(f"{_ts(at)} INFO  http method={method} path={path} status={status} duration_ms={dur} request_id={rid}")
        t += timedelta(seconds=1)

    # Postgres-side view: healthy throughout; idle connections pile up once the pool leaks.
    db_lines.append("2026-10-22 13:10:00.000 UTC [postgres] LOG  database system is ready to accept connections max_connections=200")
    for k, (bucket, w) in enumerate(sorted(windows.items())):
        for half in (0, 15):
            at = bucket + timedelta(seconds=half)
            exhausted = first_exhausted is not None and at >= first_exhausted
            qps = (w["db_q"] / 30) + rng.uniform(-0.4, 0.4) + 4
            cpu = (2.5 if exhausted else 18) + rng.uniform(-2, 3)
            active = rng.randint(0, 1) if exhausted else rng.randint(2, 5)
            total = POOL_SIZE + 4
            db_lines.append(
                f"{at.strftime('%Y-%m-%d %H:%M:%S')}.000 UTC [postgres] LOG  stats cpu_pct={cpu:.1f} connections_total={total} "
                f"active={active} idle={total - active} qps={qps:.0f} p95_latency_ms={rng.uniform(3.6, 4.8):.1f} "
                f"slow_queries=0 locks_waiting=0"
            )

    in_use_trace = []
    for bucket, w in sorted(windows.items()):
        durs = sorted(w["dur"])
        p95 = durs[int(len(durs) * 0.95) - 1] if durs else 0
        rate = w["err"] / w["req"] if w["req"] else 0
        in_use_trace.append((bucket, rate))
        # in_use at the end of the bucket is reconstructed from the app log below
        metric_lines.append({"bucket": bucket, "rps": w["req"] / 30, "rate": rate, "p95": p95})

    # pool_in_use per bucket from leaked declines in app log
    leaked_at = [l for l in app_lines if "payment declined" in l]
    for m in metric_lines:
        end = m["bucket"] + timedelta(seconds=30)
        n = sum(1 for l in leaked_at if _ts(DEPLOY_AT) <= l[:24] < _ts(end)) if end > DEPLOY_AT else 0
        m["in_use"] = min(POOL_SIZE, n + rng.randint(2, 4)) if n < POOL_SIZE else POOL_SIZE
    metrics_out = [
        f"{m['bucket'].strftime('%Y-%m-%dT%H:%M:%SZ')} service=checkout-api rps={m['rps']:.1f} error_rate_5xx={m['rate']:.2f} "
        f"p95_ms={m['p95']} pool_in_use={m['in_use']} pool_size={POOL_SIZE} mem_mb={rng.randint(305, 330)} restarts=0"
        for m in metric_lines
    ]

    deploy_lines = [
        f"2026-10-20T11:30:02Z DEPLOY checkout-api version=1.8.2 commit={short[2]} deployer=priya status=started changes={short[1]},{short[2]}",
        f"2026-10-20T11:31:10Z DEPLOY checkout-api version=1.8.2 commit={short[2]} deployer=priya status=succeeded replicas=1/1",
        f"2026-10-22T14:04:30Z DEPLOY checkout-api version=1.8.3 commit={short[4]} deployer=arjun status=started changes={short[3]},{short[4]}",
        f"2026-10-22T14:05:12Z DEPLOY checkout-api version=1.8.3 commit={short[4]} deployer=arjun status=succeeded replicas=1/1",
    ]

    app_lines.sort(key=lambda l: l[:24])
    (logs / "app.log").write_text("\n".join(app_lines) + "\n", encoding="utf-8")
    (logs / "db.log").write_text("\n".join(db_lines) + "\n", encoding="utf-8")
    (logs / "metrics.log").write_text("\n".join(metrics_out) + "\n", encoding="utf-8")
    (logs / "deploy.log").write_text("\n".join(deploy_lines) + "\n", encoding="utf-8")

    onset = next((b for b, r in in_use_trace if r > 0.05), None)
    recent = [r for b, r in in_use_trace if ALERT_AT - timedelta(minutes=2) <= b < ALERT_AT]
    value = round(sum(recent) / len(recent), 2) if recent else 0.0
    alert = {
        "id": "ALERT-7781",
        "service": "checkout-api",
        "title": f"checkout-api 5xx rate {value:.0%}",
        "severity": "sev2",
        "metric": "error_rate_5xx",
        "value": value,
        "threshold": 0.05,
        "window": "2m",
        "started_at": onset.strftime("%Y-%m-%dT%H:%M:%SZ") if onset else None,
        "fired_at": ALERT_AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "synthetic-monitoring",
    }
    (out_dir / "alert.json").write_text(json.dumps(alert, indent=2) + "\n", encoding="utf-8")
    expected = {
        "category": "code",
        "culprit_sha": short[culprit_index],
        "file": "app/checkout.py",
        "trap": "database overloaded; scale up the database",
        "fix_hint": "release the connection in a finally block (or roll back to " + short[2] + ")",
    }
    (out_dir / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return {"alert": alert, "first_exhausted": _ts(first_exhausted) if first_exhausted else None}
