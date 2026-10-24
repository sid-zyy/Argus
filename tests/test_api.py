"""API tests for the user system: sign-in, role checks, two-person approval for sev 1/2, bug reports and QA.

Live runs use the scripted fake model from test_pipeline, so no API keys are needed.
"""
import importlib
import time

import pytest
from fastapi.testclient import TestClient

from tests import test_pipeline as tp
from tools.workspace import Workspace


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INCIDENT_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("REPLAY_SPEED", "0")
    monkeypatch.setenv("TWO_PERSON_APPROVAL", "1")  # these tests cover the stricter rule
    import ui.app as appmod
    appmod = importlib.reload(appmod)
    ws = Workspace(tp.SCENARIO)
    appmod.runner.llm_factory = lambda: tp.ScriptedLLM(tp.trap_script(ws))
    return TestClient(appmod.app)


def users(c):
    return {u["name"]: u["id"] for u in c.get("/users").json()}


def sign_in(c, name):
    assert c.post("/session", json={"user_id": users(c)[name]}).status_code == 200


def wait_for(c, iid, status, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/incidents/{iid}").json()["status"]
        if s == status:
            return
        time.sleep(0.2)
    raise AssertionError(f"INC-{iid} stuck in {s}, wanted {status}")


def test_sev2_needs_developer_and_lead(client):
    c = client
    assert c.post("/demo/trigger/incident1_conn_leak").status_code == 401
    sign_in(c, "Siddhartha")
    iid = c.post("/demo/trigger/incident1_conn_leak").json()["id"]
    wait_for(c, iid, "awaiting_approval")
    inc = c.get(f"/incidents/{iid}").json()
    assert inc["severity"] == "sev2" and inc["required_approvals"]["count"] == 2

    sign_in(c, "Tara")
    assert c.post(f"/incidents/{iid}/approve", json={}).status_code == 403  # tester cannot approve
    sign_in(c, "Siddhartha")
    r = c.post(f"/incidents/{iid}/approve", json={"reason": "diff is minimal"}).json()
    assert r == {"complete": False, "still_needed": "a team lead"}
    assert c.post(f"/incidents/{iid}/approve", json={}).status_code == 409  # no double vote
    sign_in(c, "Meera")
    assert c.post(f"/incidents/{iid}/approve", json={}).json()["still_needed"] == "a team lead"  # two devs are not enough
    sign_in(c, "Yuvika")
    assert c.post(f"/incidents/{iid}/approve", json={}).json() == {"complete": True, "approved": True}
    wait_for(c, iid, "resolved")
    for _ in range(50):  # the report is written just after the status flips to resolved
        inc = c.get(f"/incidents/{iid}").json()
        if inc["report"]:
            break
        time.sleep(0.1)
    assert "Yuvika" in inc["approval"]["approver"] and inc["report"]
    m = c.get("/metrics").json()
    assert m["claims_refuted"] == 1 and m["fabrications_blocked"] == 1 and m["sandbox_passed"] == 1


def test_bug_report_flow_with_qa_and_separation_of_duties(client):
    c = client
    sign_in(c, "Siddhartha")
    bug = {"scenario": "incident1_conn_leak", "title": "Checkout fails with 503 after a declined card", "severity": "sev2",
           "repro_steps": "1. add item\n2. pay with a declined card\n3. pay again with a good card",
           "expected": "second payment succeeds", "actual": "503 Service Unavailable"}
    assert c.post("/bugs", json=bug).status_code == 403  # only testers file bug reports
    sign_in(c, "Tara")
    iid = c.post("/bugs", json=bug).json()["id"]
    wait_for(c, iid, "awaiting_approval")
    inc = c.get(f"/incidents/{iid}").json()
    assert inc["source"] == "tester_report" and inc["alert"]["id"] == f"BUG-{iid}"
    sign_in(c, "Yuvika")
    c.post(f"/incidents/{iid}/approve", json={})
    sign_in(c, "Meera")
    c.post(f"/incidents/{iid}/approve", json={})
    wait_for(c, iid, "awaiting_qa")
    assert c.post(f"/incidents/{iid}/qa", json={"verified": True}).status_code == 403  # developer cannot do QA
    sign_in(c, "Tara")
    assert c.post(f"/incidents/{iid}/qa", json={"verified": True, "note": "works now"}).status_code == 200
    assert c.get(f"/incidents/{iid}").json()["status"] == "closed"


def test_reporter_cannot_approve_own_sev2(client):
    import ui.app as appmod
    c = client
    tara = users(c)["Tara"]
    appmod.store._write(lambda db: db.execute("UPDATE users SET role='lead' WHERE id=?", (tara,)))  # a lead who also files reports
    sign_in(c, "Siddhartha")
    iid = c.post("/demo/trigger/incident1_conn_leak").json()["id"]
    appmod.store._write(lambda db: db.execute("UPDATE incidents SET reported_by=? WHERE id=?", (tara, iid)))
    wait_for(c, iid, "awaiting_approval")
    sign_in(c, "Tara")
    r = c.post(f"/incidents/{iid}/approve", json={})
    assert r.status_code == 403 and "separation of duties" in r.json()["detail"]
    sign_in(c, "Siddhartha")
    assert c.post(f"/incidents/{iid}/reject", json={}).status_code == 422  # rejection needs a reason
    assert c.post(f"/incidents/{iid}/reject", json={"reason": "want a test first"}).json()["approved"] is False
    wait_for(c, iid, "rejected")


def test_alertmanager_webhook_opens_an_incident(client):
    import json
    from pathlib import Path
    payload = json.loads((Path(__file__).resolve().parent.parent / "examples" / "alertmanager_webhook.json").read_text())
    r = client.post("/alerts", json=payload)
    assert r.status_code == 201 and r.json()["received_from"] == "alertmanager"
    inc = client.get(f"/incidents/{r.json()['id']}").json()
    assert inc["severity"] == "sev2" and inc["alert"]["source"] == "Prometheus Alertmanager"
    assert inc["assignee"] == "Siddhartha"  # auto-assigned to checkout-api's on-call developer
    assert inc["impact"]["at_risk_per_minute"] > 0


def test_pause_ignores_alerts_and_notifications_are_sent(client):
    c = client
    alert = {"alerts": [{"status": "firing", "labels": {"alertname": "X", "service": "checkout-api", "severity": "high"},
                         "annotations": {"summary": "Checkout errors"}}]}
    sign_in(c, "Tara")
    assert c.post("/argus/pause").status_code == 403  # only developers and leads
    sign_in(c, "Siddhartha")
    assert c.post("/argus/pause").json()["paused"] is True
    r = c.post("/alerts", json=alert)
    assert r.status_code == 202 and r.json()["paused"] is True
    state = c.get("/argus/state").json()
    assert state["held"] == 1 and state["last_held"]["title"] == "Checkout errors"
    assert c.get("/incidents").json() == []
    assert c.post("/argus/resume").json()["paused"] is False
    iid = c.post("/alerts", json=alert).json()["id"]
    wait_for(c, iid, "awaiting_approval")
    kinds = [n["payload"]["kind"] for n in c.get("/notifications").json()]
    assert "opened" in kinds and "cause" in kinds and "approval" in kinds


def test_impact_graph_maps_the_fault_and_prices_the_options(client):
    c = client
    sign_in(c, "Siddhartha")
    iid = c.post("/demo/trigger/incident1_conn_leak").json()["id"]
    wait_for(c, iid, "awaiting_approval")
    g = c.get(f"/incidents/{iid}/impact-graph").json()
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes["checkout"]["origin"] and nodes["checkout"]["status"] == "observed"  # the fix goes where the bug is
    assert nodes["revenue"]["status"] == "inferred" and nodes["postgres"]["status"] == "healthy"
    assert nodes["support"]["fails_at_min"] > 0  # spreads later if nothing is done
    opts = {o["key"]: o for o in g["options"]}
    assert set(opts) == {"fix", "rollback", "wait"} and opts["fix"].get("recommended")
    assert opts["fix"]["blast"]["score"] < opts["rollback"]["blast"]["score"]  # the tested, smaller change is safer
    assert opts["wait"]["loss"] > opts["fix"]["loss"]
