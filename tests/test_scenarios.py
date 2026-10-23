"""Each scenario must reproduce in the sandbox, be fixed by its intended fix, and refuse a wrong one.

This keeps the planted traps honest: the evidence that refutes the trap is in the data, and only a fix
for the real cause turns the replay healthy.
"""
import json

import pytest

from orchestrator.state import Edit, Evidence, FixOutput
from scenarios.build import ROOT, build
from tools.evidence_check import check_all
from tools.sandbox import run_sandbox
from tools.workspace import Workspace


def ws_for(name):
    build(name)
    return Workspace(ROOT / name)


def line_with(ws, source, needle):
    return next(n for n, l in enumerate(ws.lines(source), 1) if needle in l)


@pytest.fixture(scope="module")
def timeout_ws():
    return ws_for("incident2_timeout")


@pytest.fixture(scope="module")
def oom_ws():
    return ws_for("incident3_cache_oom")


def test_timeout_config_fix_passes(timeout_ws):
    fix = FixOutput(fix_type="config_change", rationale="restore timeout above provider p99",
                    edits=[Edit(file="app/config.py", old="REQUEST_TIMEOUT = 3", new="REQUEST_TIMEOUT = 30")])
    r = run_sandbox(timeout_ws, fix)
    assert r.reproduced and r.passed, r.after.output


def test_timeout_rollback_passes_and_wrong_fix_fails(timeout_ws):
    expected = json.loads((timeout_ws.root / "expected.json").read_text())
    parent = timeout_ws.git("rev-parse", "--short=7", f"{expected['culprit_sha']}^").strip()
    assert run_sandbox(timeout_ws, FixOutput(fix_type="rollback", rationale="x", rollback_to=parent)).passed
    wrong = FixOutput(fix_type="config_change", rationale="bigger pool (wrong cause)",
                      edits=[Edit(file="app/config.py", old="POOL_SIZE = 20", new="POOL_SIZE = 40")])
    assert not run_sandbox(timeout_ws, wrong).passed


def test_timeout_trap_is_refutable_from_evidence(timeout_ws):
    ws = timeout_ws
    evidence = [
        Evidence(source="payments.log", line=1, quote="status=operational"),
        Evidence(source="app.log", line=line_with(ws, "app.log", "REQUEST_TIMEOUT=3.0"), quote="config loaded REQUEST_TIMEOUT=3.0"),
        Evidence(source="app.log", line=line_with(ws, "app.log", "REQUEST_TIMEOUT=30.0"), quote="REQUEST_TIMEOUT=30.0"),
    ]
    assert check_all(ws, evidence) == []
    # slow charges succeeded before the deploy: the provider was this slow all along
    slow_ok = [l for l in ws.lines("app.log") if "charge ok latency_ms=" in l and l < "2026-10-22T13:52"
               and int(l.split("latency_ms=")[1].split()[0]) > 3500]
    assert slow_ok


def test_cache_bounded_fix_passes(oom_ws):
    old = '''# Rendering with reviews is slow; keep rendered pages around.
_PAGE_CACHE = {}
log.info("page cache enabled")


def product_page(product_id, url):
    page = _PAGE_CACHE.get(url)
    if page is None:
        page = render_product(get_product(product_id))
        _PAGE_CACHE[url] = page
    return page


def cache_size():
    return len(_PAGE_CACHE)'''
    new = '''# Rendering with reviews is slow; keep a bounded number of rendered pages, keyed by product.
@functools.lru_cache(maxsize=1024)
def _render(product_id):
    return render_product(get_product(product_id))


def product_page(product_id, url):
    return _render(int(product_id))


def cache_size():
    return _render.cache_info().currsize'''
    fix = FixOutput(fix_type="patch", rationale="bound the cache and key it by product id",
                    edits=[Edit(file="app/catalog.py", old=old, new=new),
                           Edit(file="app/catalog.py", old="import logging\n", new="import functools\nimport logging\n")])
    r = run_sandbox(oom_ws, fix)
    assert r.reproduced and r.passed, (r.error, r.after and r.after.output, r.tests and r.tests.output)


def test_cache_wrong_fix_fails(oom_ws):
    wrong = FixOutput(fix_type="config_change", rationale="more memory (wrong cause)",
                      edits=[Edit(file="deploy.yaml", old="memory: 512Mi", new="memory: 1Gi")])
    assert not run_sandbox(oom_ws, wrong).passed


def test_cache_trap_is_refutable_from_evidence(oom_ws):
    ws = oom_ws
    first_oom = line_with(ws, "events.log", "OOMKilled")
    campaign = line_with(ws, "events.log", "CampaignSent")
    assert first_oom < campaign, "first OOM must happen before the traffic spike"
    assert check_all(ws, [Evidence(source="events.log", line=1, quote="peak_rps=63 max_rss_mb=228 restarts=0"),
                          Evidence(source="events.log", line=first_oom, quote="OOMKilled pod/catalog-api")]) == []
