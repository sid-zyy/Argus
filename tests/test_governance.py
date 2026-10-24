"""Governance and readiness: the database lock, the hash-chained audit log, policy.yaml, the AI budget, the ROI ledger,
the chatbot comparison and service packs. Offline, with the scripted fake model from test_pipeline."""
import importlib
import sqlite3
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from db import lock as db_lock
from db.store import Store
from orchestrator import policy
from orchestrator.pipeline import run_incident
from orchestrator.trace import Trace
from scenarios import pack
from scenarios.build import build
from tests import test_pipeline as tp
from tools.workspace import Workspace

APPROVE = lambda ctx: {"approved": True, "approver": "t", "role": "developer"}  # noqa: E731


@pytest.fixture(scope="module")
def ws():
    build("incident1_conn_leak")
    return Workspace(tp.SCENARIO)


@pytest.fixture()
def policy_file(tmp_path, monkeypatch):
    def write(text):
        p = tmp_path / "policy.yaml"
        p.write_text(text, encoding="utf-8")
        monkeypatch.setenv("ARGUS_POLICY", str(p))
        return p
    return write


# ---- one server per database -------------------------------------------------------------------------------------
def test_second_server_on_the_same_database_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    holder = subprocess.Popen([sys.executable, "-c", "import sys, time; from db.lock import hold; hold(sys.argv[1]); print('held', flush=True); time.sleep(30)",
                               str(db)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        who = db_lock.holder(db)["pid"]  # not holder.pid: on Windows a venv's python.exe is a launcher with a child
        assert db_lock.is_locked(db)
        with pytest.raises(db_lock.DatabaseInUse, match=f"pid {who}"):
            db_lock.hold(db)
        monkeypatch.setenv("INCIDENT_DB", str(db))
        with pytest.raises(SystemExit, match="Another Argus server"):
            import ui.app as appmod  # raises here when this is the first import, else on the reload
            importlib.reload(appmod)
    finally:
        holder.kill()
        holder.wait()
    assert not db_lock.is_locked(db), "the OS releases the lock when the holder exits"


# ---- tamper-evident audit log --------------------------------------------------------------------------------------
def _store_with_events(path):
    s = Store(path)
    iid = s.create_incident({"title": "t", "service": "checkout-api"}, "incident1_conn_leak", "live")
    for n in range(5):
        s.record_event(iid, {"actor": "orchestrator", "type": "phase", "payload": {"n": n}})
    return s, iid


def test_audit_chain_detects_edits_and_deletions(tmp_path):
    s, iid = _store_with_events(tmp_path / "a.db")
    s.mark_interrupted()  # writes events through its own path; they must be chained too
    v = s.verify_chain(iid)
    assert v["ok"] and v["events"] == 6 and v["incident_events"] == 6
    raw = sqlite3.connect(tmp_path / "a.db")
    raw.execute("UPDATE events SET payload='{\"n\": 99}' WHERE id=3")
    raw.commit()
    bad = s.verify_chain()
    assert not bad["ok"] and bad["first_bad"]["id"] == 3 and "changed" in bad["first_bad"]["problem"]
    raw.execute("UPDATE events SET payload='{\"n\": 2}' WHERE id=3")  # restore, then delete one instead
    raw.execute("DELETE FROM events WHERE id=4")
    raw.commit()
    gone = s.verify_chain()
    assert not gone["ok"] and gone["first_bad"]["id"] == 5 and "removed" in gone["first_bad"]["problem"]


def test_older_databases_are_sealed_on_open(tmp_path):
    _store_with_events(tmp_path / "old.db")
    raw = sqlite3.connect(tmp_path / "old.db")
    raw.execute("UPDATE events SET hash=NULL, prev_hash=NULL")
    raw.commit()
    assert Store(tmp_path / "old.db").verify_chain()["ok"]


# ---- policy.yaml -------------------------------------------------------------------------------------------------
def test_policy_rules_and_fingerprint(policy_file, monkeypatch):
    monkeypatch.delenv("TWO_PERSON_APPROVAL", raising=False)
    policy_file("approvals:\n  sev3: {people: 1}\n  two_person: {enabled: true, severities: [sev1, sev2], lead: true}\n"
                "fixes: {allowed_types: [patch, rollback], protected_paths: ['**/migrations/**'], max_changed_lines: 3}\n")
    r = policy.approval_rule("sev2")
    assert r["count"] == 2 and r["lead"] and r["rule"] == "approvals.two_person" and len(r["policy_sha"]) == 12
    assert policy.approval_rule("sev3")["count"] == 1
    monkeypatch.setenv("TWO_PERSON_APPROVAL", "0")  # the environment overrides the file for solo rehearsals
    assert policy.approval_rule("sev2")["count"] == 1
    diff = "--- a/db/migrations/001.sql\n+++ b/db/migrations/001.sql\n-a\n+b\n+c\n+d\n"
    rules = {v["rule"] for v in policy.check_fix("config_change", diff)}
    assert rules == {"fixes.allowed_types", "fixes.protected_paths", "fixes.max_changed_lines"}
    assert policy.check_fix("patch", "--- a/app/x.py\n+++ b/app/x.py\n-a\n+b\n") == []


def test_fix_outside_the_policy_goes_back_to_the_fixer(ws, policy_file):
    policy_file("fixes: {allowed_types: [rollback]}\n")
    script = tp.trap_script(ws)
    parent = ws.git("rev-parse", "--short", "cb77497~1").strip()
    script.insert(-1, [tp.call("submit_result", {"fix_type": "rollback", "rationale": "undo the culprit", "rollback_to": parent})])
    llm = tp.ScriptedLLM(script)
    trace = Trace()
    state = run_incident(ws, llm, trace, APPROVE)
    blocked = trace.of_type("policy_blocked")
    assert len(blocked) == 1 and blocked[0]["payload"]["rule"] == "fixes.allowed_types"
    assert any("breaks the team's policy" in str(m.get("content")) for m in llm.seen), "the fixer is told which rule it broke"
    assert state["status"] == "applied" and state["fix"].fix_type == "rollback"


def test_ai_budget_stops_the_run(ws, policy_file):
    policy_file("ai: {budget_per_incident: 1, cost_per_1k_tokens: 0.05}\n")

    class Expensive(tp.ScriptedLLM):
        def chat(self, messages, tools=None, provider=None):
            return {**super().chat(messages, tools, provider), "tokens": 100_000}

    trace = Trace()
    state = run_incident(ws, Expensive([]), trace, APPROVE)
    assert state["status"] == "escalated" and "AI budget" in state["escalation"]
    assert trace.of_type("policy_blocked")[0]["payload"]["rule"] == "ai.budget_per_incident"
    assert not trace.of_type("claim"), "no investigation starts once the budget is spent"


# ---- through the API: health, policy on approvals, ROI, chatbot comparison --------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INCIDENT_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("REPLAY_SPEED", "0")
    monkeypatch.setenv("TWO_PERSON_APPROVAL", "0")
    monkeypatch.setenv("SANDBOX_MODE", "subprocess")
    monkeypatch.setenv("SHOP_URL", "http://127.0.0.1:9")  # nothing listens there
    import ui.app as appmod
    appmod = importlib.reload(appmod)
    appmod.runner.llm_factory = lambda: tp.ScriptedLLM(tp.trap_script(Workspace(tp.SCENARIO)))
    return TestClient(appmod.app)


def _resolved_incident(c):
    c.post("/session", json={"user_id": next(u["id"] for u in c.get("/users").json() if u["name"] == "Siddhartha")})
    iid = c.post("/demo/trigger/incident1_conn_leak").json()["id"]
    for _ in range(300):
        if c.get(f"/incidents/{iid}").json()["status"] == "awaiting_approval":
            break
        time.sleep(0.2)
    assert c.post(f"/incidents/{iid}/approve", json={"reason": "minimal diff"}).json()["approved"]
    for _ in range(100):
        if c.get(f"/incidents/{iid}").json()["status"] == "resolved":
            return iid
        time.sleep(0.2)
    raise AssertionError("not resolved")


def test_api_governance_value_and_comparison(client):
    c = client
    iid = _resolved_incident(c)
    inc = c.get(f"/incidents/{iid}").json()
    assert inc["approval"]["rule"] == "approvals.sev2" and inc["approval"]["policy_sha"] == policy.load()["sha"]

    h = c.get("/health").json()
    names = {x["name"]: x for x in h["checks"]}
    assert names["Audit trail"]["status"] == "ok" and names["Policy"]["status"] == "ok"
    assert names["ShopFast"]["status"] == "warn" and all(not n.startswith("Service pack") or names[n]["status"] != "bad" for n in names)
    assert c.get("/audit/verify", params={"incident": iid}).json()["ok"]
    assert any(r["rule"] == "fixes.allowed_types" for r in c.get("/policy").json()["rules"])

    roi = c.get("/roi").json()
    row = next(r for r in roi["rows"] if r["id"] == iid)
    assert row["counted"] and row["impact"]["fixed"] and row["impact"]["engineer_cost_saved"] == round(3 * 80 / 60 * 1070)
    assert roi["totals"]["engineer_cost_saved"] >= row["impact"]["engineer_cost_saved"]

    cmp = c.get(f"/incidents/{iid}/compare").json()
    assert cmp["argus"]["right_category"] and cmp["argus"]["named_culprit"] and cmp["argus"]["wrong_guess_caught"]
    assert cmp["argus"]["fix_passes"] and cmp["argus"]["approved_by_person"]
    assert cmp["chatbot"]["source"] == "recorded" and cmp["trap"].startswith("database overloaded")


def test_asking_a_chatbot_now_is_scored_and_audited(client, monkeypatch):
    import json as _json

    import llm

    class OneShot:  # answers like a chatbot: the trap, a made-up quote, no fix
        def chat(self, messages, tools=None, provider=None):
            return {"content": _json.dumps({"root_cause": "The database is overloaded; scale it up.", "category": "dependency",
                                            "culprit_commit": None, "evidence": [{"source": "db.log", "line": 3, "quote": "cpu_pct=99"}],
                                            "fix": {}}), "provider": "fake", "model": "chatbot"}

    monkeypatch.setattr(llm, "LLM", OneShot)
    c = client
    iid = _resolved_incident(c)
    sign = lambda name: c.post("/session", json={"user_id": next(u["id"] for u in c.get("/users").json() if u["name"] == name)})  # noqa: E731
    sign("Tara")
    assert c.post(f"/incidents/{iid}/compare").status_code == 403  # testers can't spend AI on this
    sign("Siddhartha")
    bot = c.post(f"/incidents/{iid}/compare").json()["chatbot"]
    assert bot["source"] == "run" and bot["by"] == "Siddhartha" and bot["model"] == "fake/chatbot"
    assert bot["right_category"] is False and bot["named_culprit"] is False and bot["evidence_verified"] == 0 and not bot["fix_passes"]
    assert c.get(f"/incidents/{iid}/compare").json()["chatbot"]["source"] == "run"
    assert c.get("/audit/verify").json()["ok"]


# ---- service packs ---------------------------------------------------------------------------------------------
def test_new_service_pack_builds_and_its_bug_is_fixable(tmp_path):
    from orchestrator.state import Edit, FixOutput
    from tools.sandbox import run_sandbox
    d = pack.new("inventory_retries", "inventory-api", root=tmp_path, maps=tmp_path / "maps")
    assert pack.validate("inventory_retries", tmp_path) == [] and (tmp_path / "maps" / "inventory-api.json").is_file()
    info = build("inventory_retries", root=tmp_path)
    assert pack.validate("inventory_retries", tmp_path) == []
    ws = Workspace(d)
    assert ws.alert["service"] == "inventory-api" and ws.alert["value"] > 0.05
    fixed = run_sandbox(ws, FixOutput(fix_type="config_change", rationale="retries back",
                                      edits=[Edit(file="app/config.py", old="MAX_RETRIES = 0", new="MAX_RETRIES = 3")]))
    assert fixed.reproduced and fixed.passed
    assert run_sandbox(ws, FixOutput(fix_type="rollback", rationale="undo", rollback_to=info["shas"][1])).passed
    with pytest.raises(FileExistsError):
        pack.new("inventory_retries", "inventory-api", root=tmp_path)


def test_pack_check_names_what_is_missing(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "history.py").write_text("COMMITS = []\n", encoding="utf-8")
    (tmp_path / "broken" / "business.json").write_text('{"unit": "orders"}', encoding="utf-8")
    problems = pack.validate("broken", tmp_path)
    assert "no generate_logs.py" in problems and "no repro.py" in problems and "history.py: no CULPRIT_INDEX" in problems
    assert any(p.startswith("business.json is missing") for p in problems)
    assert all(pack.validate(n) == [] for n in pack.names()), "the shipped packs are valid"


def test_replayed_report_uses_todays_business_figures(client):
    c = client
    c.post("/session", json={"user_id": next(u["id"] for u in c.get("/users").json() if u["name"] == "Siddhartha")})
    iid = c.post("/demo/trigger/incident1_conn_leak?mode=replay").json()["id"]
    for _ in range(300):
        if c.get(f"/incidents/{iid}").json()["status"] == "awaiting_approval":
            break
        time.sleep(0.2)
    c.post(f"/incidents/{iid}/approve", json={"reason": "ok"})
    for _ in range(150):
        inc = c.get(f"/incidents/{iid}").json()
        if inc["report"]:
            break
        time.sleep(0.2)
    imp = inc["impact"]
    assert imp["sources"] and "Flipkart-size" in imp["assumptions"][0]
    section = inc["report"].split("## Business impact", 1)[1].split("\n## ", 1)[0]
    assert f"about {imp['affected_per_minute']:,.0f} orders per minute" in section and "Sources:" in section
    assert "₹1,450" not in inc["report"], "the recorded run's old assumptions are replaced"
    summary = inc["report"].split("## Business summary", 1)[1].split("\n## ", 1)[0]
    assert "121.6 orders" not in summary and f"{imp['affected_per_minute']:,.0f} orders per minute" in summary
