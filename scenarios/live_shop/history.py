"""Commit history for the live ShopFast checkout-api. scenarios/build.py turns this into repo/.

Unlike the recorded incidents, nothing here is broken: the shop server (python -m shop.server) runs this
repo's code with real traffic, and a person breaks it live by committing a change (by hand, or with the
demo's "Push a bad change" button, which commits BAD_CHANGE below).
"""
from scenarios.incident1_conn_leak import history as base

# the same service as incident 1, up to the commit before the culprit
COMMITS = [
    *base.COMMITS[:base.CULPRIT_INDEX],
    ("2026-10-23T09:20:00+00:00", "Priya Nair <priya@example.com>", "Add runbook link to README", {
        "README.md": base.README_V2,
    }),
]
CULPRIT_INDEX = None

# what the demo button commits: the incident 1 refactor that leaks a connection on every declined card
BAD_CHANGE = {
    "author": "Arjun Mehta <arjun@example.com>",
    "message": "Simplify checkout flow; return early on declined payments",
    "files": {"app/checkout.py": base.CHECKOUT_BAD},
}
