"""Commit history for checkout-api (incident 2). scenarios/build.py turns this into repo/.

Commit 4 is the culprit: it lowers REQUEST_TIMEOUT from 30s to 3s. The payment provider's normal
p95 latency is about 3.8s, so roughly one checkout in seven now times out. The provider itself is fine.
"""

DB_PY = '''"""Tiny in-memory stand-in for the Postgres connection pool."""
import queue


class DBConnectionTimeout(Exception):
    pass


class Connection:
    def __init__(self, store):
        self._store = store

    def cart_total(self, cart_id):
        return self._store.get(cart_id, 25.0)

    def save_order(self, order_id, total):
        self._store[order_id] = total


class ConnectionPool:
    def __init__(self, size, timeout):
        self.size = size
        self.timeout = timeout
        self._store = {}
        self._free = queue.Queue()
        for _ in range(size):
            self._free.put(Connection(self._store))

    def acquire(self):
        try:
            return self._free.get(timeout=self.timeout)
        except queue.Empty:
            raise DBConnectionTimeout(f"pool exhausted (size={self.size})") from None

    def release(self, conn):
        self._free.put(conn)
'''

PAYMENTS_V1 = '''"""Client for the upstream payment provider."""
from dataclasses import dataclass


class PaymentTimeout(Exception):
    pass


@dataclass
class ChargeResult:
    ok: bool
    reason: str = ""


class PaymentClient:
    def __init__(self, base_url, timeout, transport):
        self.base_url = base_url
        self.timeout = timeout
        self.transport = transport

    def charge(self, card_token, amount):
        resp = self.transport.post(f"{self.base_url}/v1/charges", {"card": card_token, "amount": amount}, timeout=self.timeout)
        return ChargeResult(ok=resp["status"] == "succeeded", reason=resp.get("decline_code", ""))
'''

PAYMENTS_V2 = PAYMENTS_V1.replace('"""Client for the upstream payment provider."""\n', '"""Client for the upstream payment provider."""\nimport logging\nimport time\n') \
    .replace("class PaymentTimeout(Exception):", 'log = logging.getLogger("payments")\n\n\nclass PaymentTimeout(Exception):') \
    .replace('''        resp = self.transport.post(f"{self.base_url}/v1/charges", {"card": card_token, "amount": amount}, timeout=self.timeout)
''', '''        start = time.monotonic()
        resp = self.transport.post(f"{self.base_url}/v1/charges", {"card": card_token, "amount": amount}, timeout=self.timeout)
        log.info("payments charge ok latency_ms=%d", (time.monotonic() - start) * 1000)
''')

CONFIG_V1 = '''POOL_SIZE = 20
POOL_TIMEOUT = 5.0
# Seconds to wait for the payment provider. Its p99 latency is about 5s.
REQUEST_TIMEOUT = 30
PAYMENTS_URL = "https://payments.internal"
'''

CONFIG_V2 = CONFIG_V1.replace("REQUEST_TIMEOUT = 30", "REQUEST_TIMEOUT = 3")

CHECKOUT_PY = '''"""POST /checkout business logic."""
from app.payments import PaymentTimeout


def process_checkout(pool, payments, order):
    conn = pool.acquire()
    try:
        total = conn.cart_total(order["cart_id"])
        try:
            result = payments.charge(order["card_token"], total)
        except PaymentTimeout:
            return {"status": 504, "error": "payment_timeout"}
        if not result.ok:
            return {"status": 402, "error": result.reason}
        conn.save_order(order["order_id"], total)
        return {"status": 200, "order_id": order["order_id"]}
    finally:
        pool.release(conn)
'''

MAIN_PY = '''"""FastAPI wiring for checkout-api."""
import logging

from fastapi import FastAPI, HTTPException

from app import config
from app.checkout import process_checkout
from app.db import ConnectionPool
from app.http import HttpTransport
from app.payments import PaymentClient

log = logging.getLogger("app")
app = FastAPI(title="checkout-api")
pool = ConnectionPool(size=config.POOL_SIZE, timeout=config.POOL_TIMEOUT)
payments = PaymentClient(config.PAYMENTS_URL, timeout=config.REQUEST_TIMEOUT, transport=HttpTransport())
log.info("config loaded REQUEST_TIMEOUT=%.1f POOL_SIZE=%d", config.REQUEST_TIMEOUT, config.POOL_SIZE)


@app.post("/checkout")
def checkout(order: dict):
    result = process_checkout(pool, payments, order)
    if result["status"] != 200:
        raise HTTPException(status_code=result["status"], detail=result["error"])
    return result
'''

HTTP_PY = '''"""Minimal HTTP transport; raises PaymentTimeout when the provider is slower than the timeout."""
import httpx

from app.payments import PaymentTimeout


class HttpTransport:
    def post(self, url, body, timeout):
        try:
            return httpx.post(url, json=body, timeout=timeout).json()
        except httpx.TimeoutException as e:
            raise PaymentTimeout(f"upstream did not respond within {timeout}s") from e
'''

TEST_PY = '''from app.checkout import process_checkout
from app.db import ConnectionPool
from app.payments import ChargeResult, PaymentClient


class FastProvider:
    def post(self, url, body, timeout):
        return {"status": "succeeded"}


def test_checkout_happy_path():
    pool = ConnectionPool(size=2, timeout=0.1)
    payments = PaymentClient("https://payments.test", timeout=30, transport=FastProvider())
    order = {"order_id": "o1", "cart_id": "c1", "card_token": "tok"}
    assert process_checkout(pool, payments, order)["status"] == 200
'''

README_V1 = "# checkout-api\n\nHandles cart checkout for the storefront.\n"
README_V2 = README_V1 + "\n## Payment provider\n\nSLO: p99 latency under 6s. Status page: https://status.payments.internal\n"

COMMITS = [
    ("2026-10-12T10:00:00+00:00", "Priya Nair <priya@example.com>", "Initial checkout-api service", {
        "app/__init__.py": "", "app/db.py": DB_PY, "app/payments.py": PAYMENTS_V1, "app/config.py": CONFIG_V1,
        "app/checkout.py": CHECKOUT_PY, "app/main.py": MAIN_PY, "app/http.py": HTTP_PY,
        "tests/test_checkout.py": TEST_PY, "README.md": README_V1,
    }),
    ("2026-10-19T16:20:00+00:00", "Arjun Mehta <arjun@example.com>", "Log payment provider latency per charge", {
        "app/payments.py": PAYMENTS_V2,
    }),
    ("2026-10-22T13:40:00+00:00", "Dev Raman <dev@example.com>", "Lower REQUEST_TIMEOUT to fail fast under load", {
        "app/config.py": CONFIG_V2,
    }),
    ("2026-10-22T13:45:00+00:00", "Priya Nair <priya@example.com>", "Document payment provider SLO", {
        "README.md": README_V2,
    }),
]

CULPRIT_INDEX = 2
