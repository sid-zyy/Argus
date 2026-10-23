"""Deterministic synthetic telemetry for incident 3 (OOM restarts from an unbounded page cache).

The misleading clue is a marketing email that raises traffic 30% at 09:45 (the trap: "traffic spike").
Memory starts climbing at the 09:31 deploy while traffic is flat, the first OOM kill happens before the
email goes out, and yesterday's higher peak caused no restarts.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 10, 22, 9, 25, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 22, 10, 6, 0, tzinfo=timezone.utc)
DEPLOY_AT = datetime(2026, 10, 22, 9, 31, 5, tzinfo=timezone.utc)
CAMPAIGN = (datetime(2026, 10, 22, 9, 45, 0, tzinfo=timezone.utc), datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc))
ALERT_AT = datetime(2026, 10, 22, 10, 5, 0, tzinfo=timezone.utc)
BASE_RSS, LIMIT, PAGE_KB, UNIQUE = 210.0, 512.0, 12.0, 0.9
SAMPLE = 10  # access log keeps 1 in 10 requests


def _ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _z(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(out_dir: Path, shas: list[str], culprit_index: int) -> dict:
    rng = random.Random(33)
    logs = out_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    short = [s[:7] for s in shas]
    app = ["2026-10-20T15:00:12.000Z INFO  app starting catalog-api version=3.3.2 commit=" + short[1],
           "2026-10-20T15:00:12.050Z INFO  app access log sampling 1/10"]
    events = ["2026-10-21T18:30:00Z Normal TrafficPeak service=catalog-api peak_rps=63 max_rss_mb=228 restarts=0 (daily summary)"]
    metrics = []
    entries, deployed, down_until, restarts, req = 0.0, False, None, 0, 70000
    ooms, bucket = [], None
    acc = {"req": 0, "err": 0, "rss": BASE_RSS}

    def flush(b):
        if b is None:
            return
        rps = acc["req"] / 30
        metrics.append(f"{_z(b)} service=catalog-api rps={rps:.1f} error_rate_5xx={acc['err'] / max(1, acc['req']):.2f} "
                       f"rss_mb={acc['rss']:.0f} limit_mb={LIMIT:.0f} page_cache_entries={int(entries)} restarts={restarts}")

    t = START
    while t < END:
        b = t.replace(second=(t.second // 30) * 30, microsecond=0)
        if b != bucket:
            flush(bucket)
            bucket, acc = b, {"req": 0, "err": 0, "rss": acc["rss"]}
        if not deployed and t >= DEPLOY_AT:
            deployed = True
            app += [f"{_ts(t)} INFO  app received SIGTERM, draining (deploy)",
                    f"{_ts(t + timedelta(milliseconds=700))} INFO  app starting catalog-api version=3.4.0 commit={short[3]}",
                    f"{_ts(t + timedelta(milliseconds=760))} INFO  catalog page cache enabled"]
        if t == CAMPAIGN[0]:
            events.append(f"{_z(t)} Normal CampaignSent campaign=october-picks recipients=120000 expected_traffic=+30% links=/products/*?utm_source=email")
        rps = 40 + (12 if CAMPAIGN[0] <= t < CAMPAIGN[1] else 0) + rng.randint(-2, 2)
        if down_until and t < down_until:
            acc["req"] += rps
            acc["err"] += rps
            if t.second % 2 == 0:
                app.append(f"{_ts(t)} WARN  lb upstream catalog-api unavailable (restarting) status=503")
            t += timedelta(seconds=1)
            continue
        if down_until and t >= down_until:
            down_until = None
            app += [f"{_ts(t)} INFO  app starting catalog-api version=3.4.0 commit={short[3]}", f"{_ts(t)} INFO  catalog page cache enabled"]
            events.append(f"{_z(t)} Normal Started pod/catalog-api-7d9f container=catalog-api restart_count={restarts}")
        acc["req"] += rps
        if deployed:
            entries += rps * UNIQUE
        rss = BASE_RSS + entries * PAGE_KB / 1024 + rng.uniform(-3, 3)
        acc["rss"] = rss
        for i in range(rps):
            req += 1
            if req % SAMPLE:
                continue
            at = t + timedelta(milliseconds=rng.randint(0, 999))
            pid = rng.randint(1, 400)
            query = f"?utm_source=email&utm_campaign=october-picks&sid={rng.getrandbits(32):08x}" if rng.random() < .6 else f"?sid={rng.getrandbits(32):08x}"
            hit = not deployed or rng.random() < .08
            dur = rng.randint(4, 9) if deployed and hit else rng.randint(38, 70)
            app.append(f"{_ts(at)} INFO  http method=GET path=/products/{pid}{query} status=200 duration_ms={dur} request_id=r{req:07d}")
        if rss > LIMIT * .85 and t.second % 15 == 0:
            app.append(f"{_ts(t)} WARN  runtime memory high rss_mb={rss:.0f} limit_mb={LIMIT:.0f}")
        if rss >= LIMIT:
            restarts += 1
            ooms.append(t)
            events.append(f"{_z(t)} Warning OOMKilled pod/catalog-api-7d9f container=catalog-api memory limit 512Mi exceeded "
                          f"(rss {rss:.0f}Mi); restarting restart_count={restarts}")
            app.append(f"{_ts(t)} ERROR runtime killed: out of memory (rss_mb={rss:.0f})")
            entries, down_until, acc["rss"] = 0.0, t + timedelta(seconds=8), BASE_RSS
        t += timedelta(seconds=1)
    flush(bucket)

    deploy = [f"2026-10-20T14:58:40Z DEPLOY catalog-api version=3.3.2 commit={short[1]} deployer=sam status=started changes={short[1]}",
              f"2026-10-20T15:00:12Z DEPLOY catalog-api version=3.3.2 commit={short[1]} deployer=sam status=succeeded replicas=1/1",
              f"2026-10-22T09:29:50Z DEPLOY catalog-api version=3.4.0 commit={short[3]} deployer=lena status=started changes={short[2]},{short[3]}",
              f"2026-10-22T09:31:05Z DEPLOY catalog-api version=3.4.0 commit={short[3]} deployer=lena status=succeeded replicas=1/1"]
    app.sort(key=lambda l: l[:24])
    events.sort()
    for name, lines in (("app.log", app), ("events.log", events), ("metrics.log", metrics), ("deploy.log", deploy)):
        (logs / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    window = [o for o in ooms if o <= ALERT_AT]
    alert = {"id": "ALERT-7815", "service": "catalog-api", "title": f"catalog-api restarting: {len(window)} OOMKilled restarts in 35m",
             "severity": "sev2", "metric": "container_restarts", "value": len(window), "threshold": 1, "window": "35m",
             "started_at": _z(window[0]) if window else None, "fired_at": _z(ALERT_AT), "source": "synthetic-monitoring"}
    (out_dir / "alert.json").write_text(json.dumps(alert, indent=2) + "\n", encoding="utf-8")
    expected = {"category": "resource", "culprit_sha": short[culprit_index], "file": "app/catalog.py",
                "trap": "the marketing traffic spike exhausted memory; add memory or replicas",
                "fix_hint": f"bound the page cache (LRU with a max size) or key it by product id, or roll back to {short[1]}"}
    (out_dir / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return {"alert": alert, "ooms": [_z(o) for o in ooms]}
