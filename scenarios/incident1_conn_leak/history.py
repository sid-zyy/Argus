"""Commit history for the toy checkout-api. scenarios/build.py turns this into repo/.

Commit 4 is the culprit: it drops the try/finally and returns early on a declined
payment, so the pooled connection is never released.
"""

DB_PY = '''"""Tiny in-memory stand-in for the Postgres connection pool."""
import queue
import threading


class DBConnectionTimeout(Exception):
    pass


class Cart:
    def __init__(self, cart_id, total):
        self.cart_id = cart_id
        self.total = total


class Connection:
    def __init__(self, conn_id, store):
        self.conn_id = conn_id
        self._store = store

    def fetch_cart(self, cart_id):
        return Cart(cart_id, self._store.get(cart_id, 25.0))

    def save_order(self, order_id, cart):
        self._store[order_id] = cart.total

    def order_history(self, customer_id):
        return [k for k in self._store if str(k).startswith(str(customer_id))]


class ConnectionPool:
    def __init__(self, size, timeout):
        self.size = size
        self.timeout = timeout
        self._store = {}
        self._free = queue.Queue()
        self._lock = threading.Lock()
        self._in_use = 0
        for i in range(size):
            self._free.put(Connection(i, self._store))

    def acquire(self):
        try:
            conn = self._free.get(timeout=self.timeout)
        except queue.Empty:
            raise DBConnectionTimeout(
                f"pool exhausted (size={self.size}, waited {self.timeout}s)"
            ) from None
        with self._lock:
            self._in_use += 1
        return conn

    def release(self, conn):
        with self._lock:
            self._in_use -= 1
        self._free.put(conn)

    @property
    def in_use(self):
        return self._in_use
'''

PAYMENTS_PY = '''"""Client for the upstream payment provider."""
from dataclasses import dataclass


@dataclass
class ChargeResult:
    ok: bool
    reason: str = ""


class PaymentClient:
    def __init__(self, base_url, timeout):
        self.base_url = base_url
        self.timeout = timeout

    def charge(self, card_token, amount):
        raise NotImplementedError("network call to the payment provider")
'''

CONFIG_V1 = '''POOL_SIZE = 10
POOL_TIMEOUT = 5.0
REQUEST_TIMEOUT = 30
PAYMENTS_URL = "https://payments.internal"
'''

CONFIG_V2 = CONFIG_V1.replace("POOL_SIZE = 10", "POOL_SIZE = 20")

CHECKOUT_GOOD = '''"""POST /checkout business logic."""
import logging

log = logging.getLogger("checkout")


def process_checkout(pool, payments, order):
    conn = pool.acquire()
    try:
        cart = conn.fetch_cart(order["cart_id"])
        result = payments.charge(order["card_token"], cart.total)
        if result.ok:
            conn.save_order(order["order_id"], cart)
            response = {"status": 200, "order_id": order["order_id"]}
        else:
            response = {"status": 402, "error": result.reason}
    finally:
        pool.release(conn)
    return response
'''

CHECKOUT_BAD = '''"""POST /checkout business logic."""
import logging

log = logging.getLogger("checkout")


def process_checkout(pool, payments, order):
    conn = pool.acquire()
    cart = conn.fetch_cart(order["cart_id"])
    result = payments.charge(order["card_token"], cart.total)
    if not result.ok:
        log.info("payment declined order=%s reason=%s", order["order_id"], result.reason)
        return {"status": 402, "error": result.reason}
    conn.save_order(order["order_id"], cart)
    pool.release(conn)
    return {"status": 200, "order_id": order["order_id"]}
'''

HISTORY_PY = '''"""GET /orders/history business logic."""


def order_history(pool, customer_id):
    conn = pool.acquire()
    try:
        return {"status": 200, "orders": conn.order_history(customer_id)}
    finally:
        pool.release(conn)
'''

MAIN_V1 = '''"""FastAPI wiring for checkout-api."""
from fastapi import FastAPI, HTTPException

from app import config
from app.checkout import process_checkout
from app.db import ConnectionPool, DBConnectionTimeout
from app.payments import PaymentClient

app = FastAPI(title="checkout-api")
pool = ConnectionPool(size=config.POOL_SIZE, timeout=config.POOL_TIMEOUT)
payments = PaymentClient(config.PAYMENTS_URL, timeout=config.REQUEST_TIMEOUT)


@app.post("/checkout")
def checkout(order: dict):
    try:
        result = process_checkout(pool, payments, order)
    except DBConnectionTimeout as e:
        raise HTTPException(status_code=503, detail=str(e))
    if result["status"] != 200:
        raise HTTPException(status_code=result["status"], detail=result["error"])
    return result


@app.get("/health")
def health():
    return {"ok": True}
'''

MAIN_V2 = MAIN_V1.replace(
    "from app.db import ConnectionPool, DBConnectionTimeout\n",
    "from app.db import ConnectionPool, DBConnectionTimeout\nfrom app.history import order_history\n",
) + '''

@app.get("/orders/history")
def history(customer_id: str):
    try:
        return order_history(pool, customer_id)
    except DBConnectionTimeout as e:
        raise HTTPException(status_code=503, detail=str(e))
'''

TEST_PY = '''from app.checkout import process_checkout
from app.db import ConnectionPool
from app.payments import ChargeResult


class ApprovingPayments:
    def charge(self, card_token, amount):
        return ChargeResult(ok=True)


def test_checkout_happy_path():
    pool = ConnectionPool(size=2, timeout=0.1)
    order = {"order_id": "o1", "cart_id": "c1", "card_token": "tok"}
    assert process_checkout(pool, ApprovingPayments(), order)["status"] == 200
    assert pool.in_use == 0
'''

README_V1 = """# checkout-api

Handles cart checkout and order history for the storefront.
"""

README_V2 = README_V1 + """
## Runbook

On-call runbook: https://wiki.internal/runbooks/checkout-api
"""

# (iso date, author, message, {path: content})
COMMITS = [
    ("2026-10-15T10:12:00+00:00", "Priya Nair <priya@example.com>", "Initial checkout-api service", {
        "app/__init__.py": "",
        "app/db.py": DB_PY,
        "app/payments.py": PAYMENTS_PY,
        "app/config.py": CONFIG_V1,
        "app/checkout.py": CHECKOUT_GOOD,
        "app/main.py": MAIN_V1,
        "tests/test_checkout.py": TEST_PY,
        "README.md": README_V1,
    }),
    ("2026-10-18T15:40:00+00:00", "Arjun Mehta <arjun@example.com>", "Add order history endpoint", {
        "app/history.py": HISTORY_PY,
        "app/main.py": MAIN_V2,
    }),
    ("2026-10-20T11:05:00+00:00", "Priya Nair <priya@example.com>", "Raise DB pool size from 10 to 20", {
        "app/config.py": CONFIG_V2,
    }),
    ("2026-10-22T13:58:00+00:00", "Arjun Mehta <arjun@example.com>", "Simplify checkout flow; return early on declined payments", {
        "app/checkout.py": CHECKOUT_BAD,
    }),
    ("2026-10-22T14:01:00+00:00", "Priya Nair <priya@example.com>", "Add runbook link to README", {
        "README.md": README_V2,
    }),
]

CULPRIT_INDEX = 3
