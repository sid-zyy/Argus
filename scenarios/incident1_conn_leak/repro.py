"""Sandbox check for incident 1: replay checkout traffic with declined payments.

Usage: python repro.py <repo_dir>. Exit 0 = healthy, 1 = incident reproduced.
"""
import json
import sys

sys.path.insert(0, sys.argv[1])

from app.checkout import process_checkout  # noqa: E402
from app.db import ConnectionPool, DBConnectionTimeout  # noqa: E402
from app.payments import ChargeResult  # noqa: E402


class FlakyCards:
    """Declines every 4th charge, like real card traffic with a high decline rate."""

    def __init__(self):
        self.n = 0

    def charge(self, card_token, amount):
        self.n += 1
        return ChargeResult(ok=False, reason="card_declined") if self.n % 4 == 0 else ChargeResult(ok=True)


pool = ConnectionPool(size=5, timeout=0.05)
payments = FlakyCards()
requests, timeouts, statuses = 80, 0, {}
for i in range(requests):
    order = {"order_id": f"o{i}", "cart_id": f"c{i}", "card_token": "tok_test"}
    try:
        status = process_checkout(pool, payments, order)["status"]
    except DBConnectionTimeout:
        timeouts += 1
        status = 503
    statuses[status] = statuses.get(status, 0) + 1

error_rate = timeouts / requests
healthy = error_rate <= 0.05 and pool.in_use == 0
print(json.dumps({
    "requests": requests,
    "status_counts": statuses,
    "error_rate_5xx": round(error_rate, 3),
    "pool_in_use_after": pool.in_use,
    "healthy": healthy,
}))
sys.exit(0 if healthy else 1)
