"""Sandbox check for incident 2: replay checkout traffic against the provider's real latency profile.

Usage: python repro.py <repo_dir>. Exit 0 = healthy, 1 = incident reproduced. Nothing sleeps: the fake
provider compares each request's latency with the client's timeout, like a real HTTP client would.
"""
import json
import math
import random
import sys

sys.path.insert(0, sys.argv[1])

from app import config  # noqa: E402
from app.checkout import process_checkout  # noqa: E402
from app.db import ConnectionPool  # noqa: E402
from app.payments import PaymentClient, PaymentTimeout  # noqa: E402


class ProviderReplay:
    """Same latency distribution as payments.log: median 1.9s, p95 about 3.8s, p99 about 5s."""

    def __init__(self):
        self.rng = random.Random(7)

    def post(self, url, body, timeout):
        latency = self.rng.lognormvariate(math.log(1.9), 0.42)
        if latency > timeout:
            raise PaymentTimeout(f"upstream did not respond within {timeout}s")
        return {"status": "succeeded"}


pool = ConnectionPool(size=5, timeout=0.05)
payments = PaymentClient(config.PAYMENTS_URL, timeout=config.REQUEST_TIMEOUT, transport=ProviderReplay())
requests, statuses = 300, {}
for i in range(requests):
    status = process_checkout(pool, payments, {"order_id": f"o{i}", "cart_id": f"c{i}", "card_token": "tok_test"})["status"]
    statuses[status] = statuses.get(status, 0) + 1

timeouts = statuses.get(504, 0)
healthy = timeouts / requests <= 0.01
print(json.dumps({"requests": requests, "status_counts": statuses, "timeout_rate": round(timeouts / requests, 3),
                  "request_timeout_s": config.REQUEST_TIMEOUT, "healthy": healthy}))
sys.exit(0 if healthy else 1)
