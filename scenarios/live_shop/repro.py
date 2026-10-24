"""Sandbox check for the live shop: replay checkout traffic (with declined cards) against the repo's code.

Usage: python repro.py <repo_dir>. Exit 0 = healthy, 1 = broken.
Any change that makes checkout fail, leak connections or crash counts as broken, not just incident 1's bug.
"""
import json
import sys
import traceback

sys.path.insert(0, sys.argv[1])

try:
    from app.checkout import process_checkout
    from app.db import ConnectionPool, DBConnectionTimeout
    from app.payments import ChargeResult
except Exception:  # the change broke the import itself
    print(json.dumps({"healthy": False, "error": traceback.format_exc(limit=1).strip().splitlines()[-1]}))
    sys.exit(1)


class FlakyCards:
    """Declines every 4th charge, like real card traffic with a high decline rate."""

    def __init__(self):
        self.n = 0

    def charge(self, card_token, amount):
        self.n += 1
        return ChargeResult(ok=False, reason="card_declined") if self.n % 4 == 0 else ChargeResult(ok=True)


pool = ConnectionPool(size=5, timeout=0.05)
payments = FlakyCards()
requests, failures, statuses, crash = 80, 0, {}, None
for i in range(requests):
    order = {"order_id": f"o{i}", "cart_id": f"c{i}", "card_token": "tok_test"}
    try:
        status = process_checkout(pool, payments, order)["status"]
    except DBConnectionTimeout:
        status = 503
    except Exception as e:
        status, crash = 500, crash or f"{type(e).__name__}: {e}"
    if status >= 500:
        failures += 1
    statuses[status] = statuses.get(status, 0) + 1

declined_ok = statuses.get(402, 0) > 0  # declines must still be reported to the customer
error_rate = failures / requests
healthy = error_rate <= 0.05 and pool.in_use == 0 and declined_ok
print(json.dumps({
    "requests": requests,
    "status_counts": statuses,
    "error_rate_5xx": round(error_rate, 3),
    "pool_in_use_after": pool.in_use,
    "first_crash": crash,
    "healthy": healthy,
}))
sys.exit(0 if healthy else 1)
