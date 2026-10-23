"""Deterministic synthetic telemetry for incident 2 (payment timeouts after a config change).

The loudest signal is PaymentTimeout from the upstream provider, which points at the provider (the trap).
The provider's own latency is unchanged all morning; what changed is our timeout, from 30s to 3s.
"""
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 10, 22, 13, 40, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 22, 14, 5, 0, tzinfo=timezone.utc)
DEPLOY_AT = datetime(2026, 10, 22, 13, 52, 10, tzinfo=timezone.utc)
ALERT_AT = datetime(2026, 10, 22, 13, 58, 0, tzinfo=timezone.utc)
RPS = 4
MEDIAN_S, SIGMA = 1.9, 0.42  # payment provider latency, lognormal; p95 ~3.8s, p99 ~5s
ENDPOINTS = [("POST", "/checkout", .70), ("GET", "/cart", .15), ("GET", "/health", .15)]


def _ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def provider_latency(rng):
    return rng.lognormvariate(math.log(MEDIAN_S), SIGMA)


def _pick(rng):
    r, acc = rng.random(), 0.0
    for ep in ENDPOINTS:
        acc += ep[2]
        if r < acc:
            return ep
    return ENDPOINTS[-1]


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0


def generate(out_dir: Path, shas: list[str], culprit_index: int) -> dict:
    rng = random.Random(2026)
    logs = out_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    short = [s[:7] for s in shas]
    app = [
        "2026-10-20T11:02:41.000Z INFO  app starting checkout-api version=1.8.9 commit=" + short[1],
        "2026-10-20T11:02:41.120Z INFO  app config loaded REQUEST_TIMEOUT=30.0 POOL_SIZE=20",
    ]
    timeout = 30.0
    deployed = False
    buckets, minutes = {}, {}
    req = 5000
    t = START
    while t < END:
        if not deployed and t >= DEPLOY_AT:
            deployed, timeout = True, 3.0
            app.append(f"{_ts(t)} INFO  app received SIGTERM, draining (deploy)")
            app.append(f"{_ts(t + timedelta(milliseconds=800))} INFO  app starting checkout-api version=1.9.0 commit={short[3]}")
            app.append(f"{_ts(t + timedelta(milliseconds=900))} INFO  app config loaded REQUEST_TIMEOUT=3.0 POOL_SIZE=20")
        b = t.replace(second=(t.second // 30) * 30, microsecond=0)
        m = t.replace(second=0, microsecond=0)
        w = buckets.setdefault(b, {"req": 0, "err": 0, "dur": [], "lat": [], "timeouts": 0})
        lat_min = minutes.setdefault(m, [])
        for i in range(RPS):
            at = t + timedelta(milliseconds=int(1000 * i / RPS) + rng.randint(0, 120))
            req += 1
            rid = f"r{req:06d}"
            method, path, _ = _pick(rng)
            w["req"] += 1
            if path != "/checkout":
                dur = rng.randint(3, 15)
                w["dur"].append(dur)
                app.append(f"{_ts(at)} INFO  http method={method} path={path} status=200 duration_ms={dur} request_id={rid}")
                continue
            lat = provider_latency(rng)
            w["lat"].append(lat * 1000)
            lat_min.append(lat * 1000)
            if lat > timeout:
                w["err"] += 1
                w["timeouts"] += 1
                w["dur"].append(int(timeout * 1000) + 2)
                app.append(f"{_ts(at)} ERROR payments PaymentTimeout: upstream payments.internal did not respond within {timeout:.1f}s request_id={rid}")
                app.append(f"{_ts(at)} INFO  http method=POST path=/checkout status=504 duration_ms={int(timeout * 1000) + 2} request_id={rid}")
            else:
                dur = int(lat * 1000) + rng.randint(20, 60)
                w["dur"].append(dur)
                app.append(f"{_ts(at)} INFO  payments charge ok latency_ms={int(lat * 1000)} request_id={rid}")
                app.append(f"{_ts(at)} INFO  http method=POST path=/checkout status=200 duration_ms={dur} request_id={rid}")
        t += timedelta(seconds=1)

    # the provider's own status probe: steady all morning
    payments = [f"{m.strftime('%Y-%m-%dT%H:%M:%SZ')} provider=payments.internal status=operational p50_ms={_pct(l, .5):.0f} "
                f"p95_ms={_pct(l, .95):.0f} p99_ms={_pct(l, .99):.0f} error_rate=0.0{rng.randint(5, 9)}% region=eu-west"
                for m, l in sorted(minutes.items())]
    metrics, series = [], []
    for b, w in sorted(buckets.items()):
        rate = w["err"] / w["req"]
        series.append((b, rate))
        metrics.append(f"{b.strftime('%Y-%m-%dT%H:%M:%SZ')} service=checkout-api rps={w['req'] / 30:.1f} error_rate_5xx={rate:.2f} "
                       f"p95_ms={_pct(w['dur'], .95)} payment_p95_ms={_pct(w['lat'], .95):.0f} payment_timeouts={w['timeouts']} "
                       f"mem_mb={rng.randint(290, 310)} restarts=0")
    deploy = [
        f"2026-10-20T11:01:30Z DEPLOY checkout-api version=1.8.9 commit={short[1]} deployer=arjun status=started changes={short[1]}",
        f"2026-10-20T11:02:41Z DEPLOY checkout-api version=1.8.9 commit={short[1]} deployer=arjun status=succeeded replicas=1/1",
        f"2026-10-22T13:51:02Z DEPLOY checkout-api version=1.9.0 commit={short[3]} deployer=dev status=started changes={short[2]},{short[3]}",
        f"2026-10-22T13:52:10Z DEPLOY checkout-api version=1.9.0 commit={short[3]} deployer=dev status=succeeded replicas=1/1",
    ]
    app.sort(key=lambda l: l[:24])
    for name, lines in (("app.log", app), ("payments.log", payments), ("metrics.log", metrics), ("deploy.log", deploy)):
        (logs / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    onset = next((b for b, r in series if r > .05), None)
    recent = [r for b, r in series if ALERT_AT - timedelta(minutes=2) <= b < ALERT_AT]
    value = round(sum(recent) / len(recent), 2) if recent else 0.0
    alert = {"id": "ALERT-7802", "service": "checkout-api", "title": f"checkout-api 5xx rate {value:.0%}", "severity": "sev2",
             "metric": "error_rate_5xx", "value": value, "threshold": 0.05, "window": "2m",
             "started_at": onset.strftime("%Y-%m-%dT%H:%M:%SZ") if onset else None,
             "fired_at": ALERT_AT.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "synthetic-monitoring"}
    (out_dir / "alert.json").write_text(json.dumps(alert, indent=2) + "\n", encoding="utf-8")
    expected = {"category": "config", "culprit_sha": short[culprit_index], "file": "app/config.py",
                "trap": "the upstream payment provider is slow or degraded",
                "fix_hint": f"set REQUEST_TIMEOUT back above the provider's p99 (30s), or roll back to {short[1]}"}
    (out_dir / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return {"alert": alert}
