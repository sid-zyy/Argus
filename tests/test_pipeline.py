"""Offline tests: a scripted fake LLM drives the real pipeline, tools, evidence checker and sandbox.

The fake model is test-only; it replays the trap scenario (wrong DB hypothesis first) so the
control flow can be checked without API keys.
"""
from pathlib import Path

import pytest

from orchestrator.pipeline import run_incident
from orchestrator.report import build_report
from orchestrator.trace import Trace
from scenarios.build import build
from tools.workspace import Workspace

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "incident1_conn_leak"


@pytest.fixture(scope="module")
def ws():
    build("incident1_conn_leak")
    return Workspace(SCENARIO)


def find(ws, source, needle):
    for n, line in enumerate(ws.lines(source), 1):
        if needle in line:
            return n
    raise AssertionError(f"{needle!r} not in {source}")


def ev(ws, source, needle):
    return {"source": source, "line": find(ws, source, needle), "quote": needle}


def call(name, args, i=[0]):
    i[0] += 1
    return {"id": f"call_{i[0]}", "name": name, "arguments": args}


PLAN = {"steps": [{"agent": "investigator", "goal": "Find when the 5xx errors started"},
                  {"agent": "fact_checker", "goal": "Test each cause against DB health and deploy timing"},
                  {"agent": "change_finder", "goal": "Check the 14:05 deploy for risky changes"},
                  {"agent": "fixer", "goal": "Write the smallest fix for the confirmed cause"},
                  {"agent": "test_lab", "goal": "Replay checkout traffic without and with the fix"}],
        "rationale": "Separate the database theory from the deploy timing first."}


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def chat(self, messages, tools=None, provider=None):
        self.seen.append(messages[-1])
        if messages[0]["content"].startswith("You are the Commander"):  # planner: canned plan, outside the script
            return {"content": "", "tool_calls": [call("submit_result", PLAN)], "provider": "scripted", "model": "test"}
        r = self.replies.pop(0)
        return {"content": "", "tool_calls": r, "provider": "scripted", "model": "test"}


OLD = '''    cart = conn.fetch_cart(order["cart_id"])
    result = payments.charge(order["card_token"], cart.total)
    if not result.ok:
        log.info("payment declined order=%s reason=%s", order["order_id"], result.reason)
        return {"status": 402, "error": result.reason}
    conn.save_order(order["order_id"], cart)
    pool.release(conn)
    return {"status": 200, "order_id": order["order_id"]}'''
NEW = '''    try:
        cart = conn.fetch_cart(order["cart_id"])
        result = payments.charge(order["card_token"], cart.total)
        if not result.ok:
            log.info("payment declined order=%s reason=%s", order["order_id"], result.reason)
            return {"status": 402, "error": result.reason}
        conn.save_order(order["order_id"], cart)
        return {"status": 200, "order_id": order["order_id"]}
    finally:
        pool.release(conn)'''


def trap_script(ws):
    db_claim = {"id": "C1", "category": "dependency", "statement": "The database is overloaded; scale it up.",
                "evidence": [ev(ws, "app.log", "DBConnectionTimeout: pool exhausted")]}
    fake = {"source": "db.log", "line": 30, "quote": "cpu_pct=99.0"}
    leak_claim = {"id": "C1", "category": "code",
                  "statement": "Commit cb77497 returns early on declined payments without releasing the connection.",
                  "evidence": [ev(ws, "git:cb77497", 'return {"status": 402, "error": result.reason}')]}
    return [
        # attempt 1: triage searches, then proposes the trap
        [call("search_logs", {"pattern": "ERROR", "max_results": 3})],
        [call("submit_result", {"claims": [db_claim]})],
        # verifier first cites a fake line (rejected by the checker), then refutes with real evidence
        [call("search_logs", {"pattern": "stats", "source": "db.log", "start": "2026-10-22T14:06:00", "max_results": 4})],
        [call("submit_result", {"verdicts": [{"claim_id": "C1", "verdict": "refuted", "reasoning": "DB CPU is 99%", "evidence": [fake]}]})],
        [call("submit_result", {"verdicts": [{"claim_id": "C1", "verdict": "refuted",
                                               "reasoning": "Postgres is idle with no slow queries; errors start after deploy 1.8.3.",
                                               "evidence": [ev(ws, "db.log", "slow_queries=0 locks_waiting=0"),
                                                            ev(ws, "deploy.log", "version=1.8.3 commit=8efeb50 deployer=arjun status=succeeded")]}]})],
        # code history
        [call("git_log", {"since": "2026-10-21"})],
        [call("submit_result", {"suspects": [{"sha": "cb77497", "summary": "Simplify checkout flow", "reason": "deployed 14:05, removes try/finally",
                                               "evidence": [ev(ws, "git:cb77497", "-    finally:")]}]})],
        # attempt 2
        [call("submit_result", {"claims": [leak_claim]})],
        [call("submit_result", {"verdicts": [{"claim_id": "C2", "verdict": "supported",
                                               "reasoning": "The 402 path returns before pool.release(conn).",
                                               "evidence": [ev(ws, "repo/app/checkout.py", 'return {"status": 402, "error": result.reason}')]}]})],
        # fix
        [call("read_file", {"path": "app/checkout.py"})],
        [call("submit_result", {"fix_type": "patch", "rationale": "release in finally", "edits": [{"file": "app/checkout.py", "old": OLD, "new": NEW}]})],
        # reporter
        [call("submit_result", {"business_summary": "b", "summary": "s", "impact": "i", "root_cause": "r", "follow_ups": ["add a decline-path test"]})],
    ]


def test_trap_is_refuted_then_leak_fixed(ws):
    llm = ScriptedLLM(trap_script(ws))
    trace = Trace()
    decisions = []
    state = run_incident(ws, llm, trace, lambda ctx: decisions.append(ctx) or {"approved": True, "approver": "t", "role": "developer"})

    verdicts = [e["payload"] for e in trace.of_type("verdict")]
    assert [v["verdict"] for v in verdicts] == ["refuted", "supported"]
    assert any(e["actor"] == "verifier" for e in trace.of_type("rejected")), "fake db.log quote must be rejected"
    assert state["attempts"] == 2
    assert state["sandbox"].reproduced and state["sandbox"].passed
    assert state["status"] == "applied" and decisions
    report = build_report(ws.alert, state, trace, llm)
    assert "Ruled out" in report and "Confirmed" in report and "**Pass**" in report and "Decisions made by people" in report


def test_no_supported_claim_escalates(ws):
    claim = {"id": "C1", "category": "resource", "statement": "x", "evidence": [ev(ws, "app.log", "DBConnectionTimeout: pool exhausted")]}
    insufficient = [[call("submit_result", {"verdicts": [{"claim_id": f"C{n}", "verdict": "insufficient", "reasoning": "unclear", "evidence": []}]})]
                    for n in (1, 2, 3)]
    suspects = [call("submit_result", {"suspects": []})]
    script = [[call("submit_result", {"claims": [claim]})], insufficient[0], suspects,
              [call("submit_result", {"claims": [claim]})], insufficient[1],
              [call("submit_result", {"claims": [claim]})], insufficient[2]]
    approved = []
    state = run_incident(ws, ScriptedLLM(script), Trace(), lambda ctx: approved.append(ctx))
    assert state["status"] == "escalated" and state["attempts"] == 3
    assert not approved, "nothing reaches the approval gate without a verified claim"


def test_broken_fix_never_passes(ws):
    from orchestrator.state import FixOutput
    from tools.sandbox import run_sandbox
    bad = FixOutput(fix_type="patch", rationale="x", edits=[{"file": "app/checkout.py", "old": "not in file", "new": "y"}])
    r = run_sandbox(ws, bad)
    assert r.reproduced and not r.passed and r.error


def test_symptom_only_support_is_downgraded(ws):
    from orchestrator.pipeline import _clean_verdicts
    from orchestrator.state import Claim, Evidence, Verdict
    symptom = ev(ws, "app.log", "DBConnectionTimeout: pool exhausted")
    claim = Claim(id="C1", category="resource", statement="pool exhausted because requests held connections", evidence=[Evidence(**symptom)])
    v = Verdict(claim_id="C1", verdict="supported", reasoning="logs show it", evidence=[Evidence(**symptom)])
    assert _clean_verdicts(ws, [claim], [v])[0].verdict == "insufficient"
    cause = ev(ws, "git:cb77497", 'return {"status": 402, "error": result.reason}')
    v2 = Verdict(claim_id="C1", verdict="supported", reasoning="the commit skips release", evidence=[Evidence(**cause)])
    assert _clean_verdicts(ws, [claim], [v2])[0].verdict == "supported"
