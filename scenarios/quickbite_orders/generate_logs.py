"""Deterministic synthetic telemetry for QuickBite's orders-service, in the formats a Node.js shop would really have:

- logs/app.jsonl      pino JSON lines (numeric levels, epoch-millisecond times) from the service
- logs/payrail.log    the payment gateway's merchant log export (plain text)
- logs/metrics.csv    a Grafana/Prometheus export, one row per 30 s
- logs/deploys.jsonl  CI deploy records

Argus Connect's jsonl, text and csv adapters turn all of them into the same numbered lines the agents read.

The planted trap: every failure is logged as "GatewayError: charge rejected (400)", the gateway client changed the day
before, and a WEEKEND20 marketing push starts at 19:00. The evidence that refutes it: PayRail reports itself operational
and rejects only negative amounts, WEEKEND20 orders were fine between 19:00 and the 19:03 deploy, and only WEEKEND20
orders fail after it.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 10, 23, 18, 30, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 23, 19, 12, 0, tzinfo=timezone.utc)
CAMPAIGN_AT = datetime(2026, 10, 23, 19, 0, 0, tzinfo=timezone.utc)
DEPLOY_AT = datetime(2026, 10, 23, 19, 3, 5, tzinfo=timezone.utc)
ALERT_AT = datetime(2026, 10, 23, 19, 6, 30, tzinfo=timezone.utc)
FEE = 3000
MENU = {"paneer-tikka": 24000, "butter-chicken": 32000, "veg-biryani": 26000, "dal-makhani": 22000,
        "garlic-naan": 6000, "mango-lassi": 9000}
PID, HOST = 4182, "orders-7f9c4d"


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _z(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(t):
    return int(t.timestamp() * 1000)


def generate(out_dir: Path, shas: list[str], culprit_index: int) -> dict:
    rng = random.Random(4242)
    logs = out_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    short = [s[:7] for s in shas]
    app, payrail, metrics = [], [], []

    def log(t, level, msg, **fields):
        app.append((t, json.dumps({"level": level, "time": _ms(t), "pid": PID, "hostname": HOST, "msg": msg, **fields})))

    def gw(t, text):
        payrail.append((t, f"{_iso(t)} gateway=payrail merchant=quickbite {text}"))

    log(START - timedelta(hours=31, minutes=15), 30, "server started", version="2.3.1", commit=short[3], port=8200)
    gw(START - timedelta(minutes=1), "event=health status=operational success_rate=99.8% p99_ms=230 open_incidents=0")

    order_no, bucket, deployed, buckets = 1000, None, False, {}
    t = START
    while t < END:
        b = t.replace(second=(t.second // 30) * 30, microsecond=0)
        acc = buckets.setdefault(b, {"orders": 0, "failed": 0, "declined": 0, "gw4xx": 0, "lat": []})
        if t.second == 0 and t.microsecond == 0:
            gw(t, f"event=health status=operational success_rate={rng.uniform(99.6, 99.9):.1f}% p99_ms={rng.randint(210, 260)} open_incidents=0")
        if not deployed and t >= DEPLOY_AT:
            deployed = True
            log(t - timedelta(seconds=2), 30, "received SIGTERM, draining connections")
            log(t, 30, "server started", version="2.4.0", commit=short[culprit_index], port=8200)
            log(t + timedelta(milliseconds=40), 30, "promo codes loaded", count=2, source="src/promos.json")
        if t == CAMPAIGN_AT:
            log(t, 30, "campaign traffic tag seen", campaign="weekend20-push", utm_source="push")

        per_min = 5 if t < CAMPAIGN_AT else 11
        if rng.random() < per_min / 60:
            order_no += 1
            oid = f"o-{order_no}"
            items = [{"sku": s, "qty": rng.randint(1, 3)} for s in rng.sample(sorted(MENU), rng.randint(1, 3))]
            subtotal = sum(MENU[i["sku"]] * i["qty"] for i in items)
            r = rng.random()
            weekend_share = 0.06 if t < CAMPAIGN_AT else 0.36
            promo = "WEEKEND20" if r < weekend_share else "FLAT100" if r < weekend_share + 0.08 else None
            when = t + timedelta(milliseconds=rng.randint(0, 900))
            log(when, 30, "order received", orderId=oid, items=len(items), promo=promo)
            if promo == "WEEKEND20":
                discount = subtotal * 20 if deployed else round(subtotal * 0.2)
            elif promo == "FLAT100":
                discount = min(10000, subtotal)
            else:
                discount = 0
            amount = subtotal - discount + FEE
            done = when + timedelta(milliseconds=rng.randint(140, 320))
            acc["orders"] += 1
            if amount <= 0:
                acc["failed"] += 1
                acc["gw4xx"] += 1
                gw(done, f"event=charge_rejected reference={oid} amount={amount} currency=INR http=400 "
                         f'error=invalid_request reason="amount must be a positive integer"')
                log(done, 30, "gateway charge", orderId=oid, status=400, latency_ms=rng.randint(40, 90))
                log(done + timedelta(milliseconds=2), 50, "order failed", orderId=oid, err="GatewayError: charge rejected (400): invalid_request")
            elif rng.random() < 0.03:
                acc["declined"] += 1
                gw(done, f"event=charge_declined reference={oid} amount={amount} currency=INR http=402 decline_code=card_declined")
                log(done, 30, "gateway charge", orderId=oid, status=402, latency_ms=rng.randint(150, 300))
                log(done + timedelta(milliseconds=2), 30, "payment declined", orderId=oid, reason="card_declined")
            else:
                latency = rng.randint(150, 320)
                acc["lat"].append(latency)
                gw(done, f"event=charge_succeeded reference={oid} amount={amount} currency=INR http=201")
                log(done, 30, "gateway charge", orderId=oid, status=201, latency_ms=latency)
                log(done + timedelta(milliseconds=3), 30, "order placed", orderId=oid, total=amount)
        t += timedelta(seconds=1)

    for b, acc in sorted(buckets.items()):
        n = acc["orders"]
        lat = sorted(acc["lat"]) or [0]
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        rps = round(n / 30 * 6 + rng.uniform(1.5, 2.5), 2)  # orders plus menu and tracking requests
        metrics.append([_z(b), f"{rps:.2f}", str(n * 2), f"{acc['failed'] / n if n else 0:.3f}", str(p95 or rng.randint(180, 240)),
                        str(rng.randint(14, 22) + (6 if b >= CAMPAIGN_AT else 0)), f"{rng.uniform(1.1, 2.4):.1f}",
                        str(acc["gw4xx"]), "0", str(rng.randint(61, 68))])

    deploys = []
    for when, version, sha, actor, changes in (
            (datetime(2026, 10, 16, 13, 2, tzinfo=timezone.utc), "2.2.0", short[1], "ananya", [short[1]]),
            (datetime(2026, 10, 20, 15, 40, tzinfo=timezone.utc), "2.3.0", short[2], "ananya", [short[2]]),
            (datetime(2026, 10, 22, 11, 15, tzinfo=timezone.utc), "2.3.1", short[3], "rohan", [short[3]]),
            (DEPLOY_AT - timedelta(minutes=1, seconds=40), "2.4.0", short[culprit_index], "kabir", [short[culprit_index]])):
        base = {"service": "orders-service", "version": version, "commit": sha, "actor": actor, "pipeline": "github-actions"}
        deploys.append(json.dumps({"ts": _z(when), "event": "deploy_started", **base, "changes": changes}))
        deploys.append(json.dumps({"ts": _z(when + timedelta(seconds=100)), "event": "deploy_finished", **base, "status": "succeeded",
                                   "replicas": "2/2"}))

    app.sort(key=lambda x: x[0])
    payrail.sort(key=lambda x: x[0])
    (logs / "app.jsonl").write_text("\n".join(l for _, l in app) + "\n", encoding="utf-8")
    (logs / "payrail.log").write_text("\n".join(l for _, l in payrail) + "\n", encoding="utf-8")
    header = "timestamp,rps,orders_per_min,order_failure_rate,p95_ms,cpu_pct,event_loop_lag_ms,gateway_4xx,gateway_5xx,heap_mb"
    (logs / "metrics.csv").write_text(header + "\n" + "\n".join(",".join(r) for r in metrics) + "\n", encoding="utf-8")
    (logs / "deploys.jsonl").write_text("\n".join(deploys) + "\n", encoding="utf-8")

    # the alert: failure rate over the two minutes before it fired
    window = [a for b, a in buckets.items() if ALERT_AT - timedelta(minutes=2) <= b < ALERT_AT]
    orders, failed = sum(a["orders"] for a in window), sum(a["failed"] for a in window)
    rate = round(failed / orders, 3) if orders else 0
    started = next(b for b, a in sorted(buckets.items()) if b >= DEPLOY_AT - timedelta(seconds=30) and a["failed"])
    alert = {"id": "GF-OrderFailureRateHigh", "service": "orders-service", "title": f"orders-service order failure rate {rate:.0%}",
             "severity": "sev3", "metric": "order_failure_rate", "value": rate, "threshold": 0.1, "window": "2m",
             "started_at": _z(started), "fired_at": _z(ALERT_AT), "source": "Grafana Alerting"}
    (out_dir / "alert.json").write_text(json.dumps(alert, indent=2) + "\n", encoding="utf-8")
    grafana = {
        "receiver": "argus", "status": "firing", "orgId": 1, "state": "alerting",
        "title": "[FIRING:1] OrderFailureRateHigh QuickBite",
        "groupLabels": {"alertname": "OrderFailureRateHigh"},
        "commonLabels": {"alertname": "OrderFailureRateHigh", "service": "orders-service", "team": "ordering", "severity": "warning"},
        "externalURL": "https://grafana.quickbite.example/", "version": "1", "truncatedAlerts": 0,
        "alerts": [{
            "status": "firing",
            "labels": {"alertname": "OrderFailureRateHigh", "service": "orders-service", "team": "ordering", "severity": "warning",
                       "grafana_folder": "QuickBite"},
            "annotations": {"summary": f"orders-service order failure rate {rate:.0%}",
                            "description": "Share of POST /orders ending in a 5xx, over 2 minutes, is above 10%",
                            "metric": "order_failure_rate", "threshold": "0.1", "window": "2m",
                            "fired_at": _z(ALERT_AT)},  # recorded payload: keep its own fire time, so detection time is real
            "startsAt": _z(started), "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "https://grafana.quickbite.example/alerting/grafana/order-failure-rate/view",
            "fingerprint": "9c1e4b7f02aa61d3",
            "dashboardURL": "https://grafana.quickbite.example/d/orders/orders-service",
            "values": {"A": rate, "C": 1}, "valueString": f"[ var='A' labels={{service=orders-service}} value={rate} ]",
        }],
    }
    (out_dir / "alerts").mkdir(exist_ok=True)
    (out_dir / "alerts" / "grafana_alert.json").write_text(json.dumps(grafana, indent=2) + "\n", encoding="utf-8")
    expected = {"category": "code", "culprit_sha": short[culprit_index], "file": "src/pricing.js",
                "trap": "PayRail (the payment gateway) is failing, or the marketing push overloaded the service",
                "fix_hint": f"divide promo.percent by 100 in src/pricing.js, or roll back to {short[3]}"}
    (out_dir / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return {"alert": alert, "orders": order_no - 1000}
