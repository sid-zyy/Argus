"""Argus Connect: the standard incident schema, the adapters, per-service config and its checks, and the same pipeline
running unchanged on a second application (QuickBite, a Node.js service with Grafana alerts and JSON logs).

Offline: the pipeline runs with the scripted fake model from test_pipeline. QuickBite tests need Node.js.
"""
import json
import shutil
from pathlib import Path

import pytest

from connect.adapters import alerts
from connect.adapters.logs import logs_for
from connect.config import ConfigError, load, parse, service_folders
from connect.intake import alert_for
from connect.policy import required_approvals
from connect.validate import check, check_folder
from orchestrator.pipeline import _clean_verdicts, run_incident
from orchestrator.state import Claim, Edit, Evidence, FixOutput, Verdict
from orchestrator.trace import Trace
from scenarios.build import ROOT as SCENARIOS, build
from tests.test_pipeline import ScriptedLLM, call
from tools.sandbox import run_sandbox
from tools.workspace import Workspace

ROOT = SCENARIOS.parent
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="QuickBite is a Node.js app")


@pytest.fixture(scope="module")
def qb():
    build("quickbite_orders")
    return Workspace(SCENARIOS / "quickbite_orders")


def find(ws, source, needle):
    return next(n for n, l in enumerate(ws.lines(source), 1) if needle in l)


def ev(ws, source, needle):
    return {"source": source, "line": find(ws, source, needle), "quote": needle}


# ---- config and checks ---------------------------------------------------------------
def test_every_service_folder_has_a_passing_config():
    keys = [d.name for d in service_folders()]
    assert {"live_shop", "quickbite_orders", "incident1_conn_leak"} <= set(keys)
    for d in service_folders():
        if not (load(d).repo_path() / ".git").exists():
            build(d.name)
        r = check_folder(d)
        assert r.ok, (d.name, [c.message for c in r.errors])


def test_broken_config_is_reported_not_run(tmp_path):
    folder = tmp_path / "broken"
    folder.mkdir()
    cfg = parse({
        "service": {"name": "x"}, "repo": {"path": "nowhere"},
        "logs": [{"name": "app.log", "path": "logs/app.log"}, {"name": "report", "path": "logs/r.log"}],
        "metrics": {"source": "missing.log"},
        "dependencies": [{"name": "db", "logs": ["db.log"]}],
        "commands": {"repro": "no-such-tool check {repo}", "files": ["checks/repro.py"]},
        "severity": {"approvals": {"urgent": {"count": 2}}},
    }, folder)
    errors = " | ".join(c.message for c in check(cfg).errors)
    for problem in ("no git repo", "no such file: app.log", "'report' is reserved", "missing.log", "'db.log' is not one of",
                    "'no-such-tool' is not installed", "checks/repro.py does not exist", "'urgent'"):
        assert problem in errors, problem
    with pytest.raises(ConfigError, match="unknown_field"):
        parse({"service": {"name": "x"}, "repo": {}, "commands": {"repro": "x"}, "unknown_field": 1}, folder)


def test_legacy_folder_without_config_gets_the_built_in_layout(tmp_path):
    src = SCENARIOS / "incident2_timeout"
    folder = tmp_path / "old_style"
    shutil.copytree(src, folder, ignore=shutil.ignore_patterns("argus.yaml", "repo", "__pycache__"))
    cfg = load(folder)
    assert cfg.file is None and cfg.service.name == "checkout-api"
    assert cfg.sources("deploy") == ["deploy.log"] and cfg.symptom_sources == ("app.log", "metrics.log")
    assert cfg.commands.repro == "python repro.py {repo}"


# ---- adapters --------------------------------------------------------------------------
def test_jsonl_and_csv_logs_render_as_timestamped_lines(qb):
    app = qb.lines("app.log")
    assert all(l[:4] == "2026" and l[10] == "T" for l in app), "pino epoch-ms times become ISO"
    failed = app[find(qb, "app.log", "order failed") - 1]
    assert " ERROR order failed orderId=" in failed and "pid=" not in failed and "hostname=" not in failed
    assert 'err="GatewayError: charge rejected (400): invalid_request"' in failed
    metrics = qb.lines("metrics.log")
    assert metrics[0].startswith("2026-10-23T18:30:00.000Z rps=") and "order_failure_rate=" in metrics[0]
    assert "deploy_finished" in qb.lines("deploy.log")[-1]


def test_bad_json_lines_are_kept_and_counted(tmp_path):
    (tmp_path / "a.jsonl").write_text('{"time": "2026-01-01T00:00:00Z", "msg": "ok"}\nnot json\n', encoding="utf-8")
    cfg = parse({"service": {"name": "x"}, "repo": {"path": "."}, "commands": {"repro": "x"},
                 "logs": [{"name": "a.log", "adapter": "jsonl", "path": "a.jsonl"}]}, tmp_path)
    src = logs_for(cfg)["a.log"]
    assert src.lines() == ["2026-01-01T00:00:00.000Z ok", "not json"] and src.bad_lines == 1


def test_alertmanager_grafana_and_standard_payloads_normalise_and_route():
    services = [load(d) for d in service_folders()]
    am = json.loads((ROOT / "examples" / "alertmanager_webhook.json").read_text(encoding="utf-8"))
    cfg, inc, fmt = alerts.route(am, services)
    assert (fmt, cfg.key, inc.severity, inc.source) == ("alertmanager", "incident1_conn_leak", "sev2", "Prometheus Alertmanager")
    assert inc.id == "AM-CheckoutHighErrorRate-a1b2c3"

    gf = json.loads((SCENARIOS / "quickbite_orders" / "alerts" / "grafana_alert.json").read_text(encoding="utf-8"))
    cfg, inc, fmt = alerts.route(gf, services)
    assert (fmt, cfg.key, inc.service, inc.metric) == ("grafana", "quickbite_orders", "orders-service", "order_failure_rate")
    assert inc.value == pytest.approx(gf["alerts"][0]["values"]["A"]) and inc.threshold == 0.1
    assert inc.severity == "sev3" and alert_for(cfg, inc)["severity"] == "sev1", "the service's rule escalates a 43% failure rate"

    cfg, inc, fmt = alerts.route({"service": "catalog-api", "title": "catalog OOM", "severity": "critical"}, services)
    assert (fmt, cfg.key) == ("standard", "incident3_cache_oom") and inc.severity == "sev1"

    with pytest.raises(alerts.RouteError, match="no connected service"):
        alerts.route({"service": "billing", "title": "x"}, services)
    with pytest.raises(alerts.RouteError, match="unrecognised"):
        alerts.parse({"hello": "world"})


def test_approval_policy_comes_from_the_service(monkeypatch):
    monkeypatch.setenv("TWO_PERSON_APPROVAL", "0")
    qb_cfg = load(SCENARIOS / "quickbite_orders")
    rule = required_approvals("sev1", qb_cfg)
    assert (rule["count"], rule["lead"], rule["label"], rule["policy_file"]) == (1, True, "A team lead", "quickbite_orders/argus.yaml")
    assert required_approvals("sev3", qb_cfg)["lead"] is False
    shop = load(SCENARIOS / "live_shop")
    assert required_approvals("sev1", shop)["count"] == 1  # no rule of its own: policy.yaml decides
    monkeypatch.setenv("TWO_PERSON_APPROVAL", "1")
    assert required_approvals("sev1", shop)["count"] == 2


# ---- the second application, through the unchanged pipeline --------------------------------
PRICING_OLD = "Math.round(subtotal * promo.percent)"
PRICING_NEW = "Math.round((subtotal * promo.percent) / 100)"


@needs_node
def test_quickbite_sandbox_accepts_only_a_real_fix(qb):
    culprit = json.loads((qb.root / "expected.json").read_text(encoding="utf-8"))["culprit_sha"]
    good = run_sandbox(qb, FixOutput(fix_type="patch", rationale="whole-number percentages",
                                     edits=[Edit(file="src/pricing.js", old=PRICING_OLD, new=PRICING_NEW)]))
    assert good.reproduced and good.passed and good.tests and good.tests.exit_code == 0
    hide = run_sandbox(qb, FixOutput(fix_type="patch", rationale="never charge less than 1 paisa", edits=[Edit(
        file="src/pricing.js", old="total: subtotal - discount + DELIVERY_FEE }",
        new="total: Math.max(1, subtotal - discount + DELIVERY_FEE) }")]))
    assert not hide.passed and '"wrong_amount_charged":20' in hide.after.output
    parent = qb.git("rev-parse", "--short=7", f"{culprit}^").strip()
    assert run_sandbox(qb, FixOutput(fix_type="rollback", rationale="x", rollback_to=parent)).passed


def test_quickbite_evidence_roles_come_from_its_config(qb):
    gateway_down = Claim(id="C1", category="dependency", statement="PayRail is failing charges", evidence=[
        Evidence(**ev(qb, "app.log", "GatewayError: charge rejected (400)"))])
    v = Verdict(claim_id="C1", verdict="supported", reasoning="errors", evidence=gateway_down.evidence)
    assert _clean_verdicts(qb, [gateway_down], [v])[0].verdict == "insufficient", "app.log is this service's own log"
    v2 = Verdict(claim_id="C1", verdict="supported", reasoning="gateway says so",
                 evidence=[Evidence(**ev(qb, "payrail.log", "event=charge_rejected"))])
    assert _clean_verdicts(qb, [gateway_down], [v2])[0].verdict == "supported", "payrail.log is the dependency's own log"


def quickbite_script(ws):
    culprit = json.loads((ws.root / "expected.json").read_text(encoding="utf-8"))["culprit_sha"]
    trap = {"id": "C1", "category": "dependency", "statement": "PayRail is rejecting charges; fail over to the backup gateway.",
            "evidence": [ev(ws, "app.log", "GatewayError: charge rejected (400): invalid_request")]}
    real = {"id": "C1", "category": "code", "statement": f"Commit {culprit} multiplies by promo.percent (20) instead of 20%, "
                                                         "so WEEKEND20 totals go negative and PayRail rejects them.",
            "evidence": [ev(ws, f"git:{culprit}", "+  if (promo.type === \"percent\") return Math.round(subtotal * promo.percent);")]}
    return [
        [call("search_logs", {"pattern": "ERROR", "source": "app.log", "max_results": 3})],
        [call("submit_result", {"claims": [trap]})],
        [call("search_logs", {"pattern": "health", "source": "payrail.log", "start": "2026-10-23T19:03:00", "max_results": 3})],
        [call("submit_result", {"verdicts": [{"claim_id": "C1", "verdict": "refuted",
                                               "reasoning": "PayRail reports itself operational and only rejects negative amounts.",
                                               "evidence": [ev(ws, "payrail.log", "status=operational"),
                                                            ev(ws, "payrail.log", 'reason="amount must be a positive integer"')]}]})],
        [call("git_log", {"since": "2026-10-22"})],
        [call("submit_result", {"suspects": [{"sha": culprit, "summary": "Move promo codes to promos.json",
                                               "reason": "deployed 19:03, right before the failures",
                                               "evidence": [ev(ws, "deploy.log", f"version=2.4.0 commit={culprit}")]}]})],
        [call("submit_result", {"claims": [real]})],
        [call("submit_result", {"verdicts": [{"claim_id": "C2", "verdict": "supported",
                                               "reasoning": "promos.json stores 20, pricing multiplies by it.",
                                               "evidence": [ev(ws, "repo/src/pricing.js", "subtotal * promo.percent")]}]})],
        [call("read_file", {"path": "src/pricing.js"})],
        [call("submit_result", {"fix_type": "patch", "rationale": "Divide whole-number percentages by 100",
                                "edits": [{"file": "src/pricing.js", "old": PRICING_OLD, "new": PRICING_NEW}]})],
    ]


@needs_node
def test_same_pipeline_runs_on_quickbite(qb):
    qb.alert = alert_for(qb.config)
    assert qb.alert["severity"] == "sev1"
    trace = Trace()
    state = run_incident(qb, ScriptedLLM(quickbite_script(qb)), trace,
                         lambda ctx: {"approved": True, "approver": "Yuvika", "role": "lead"})
    assert [e["payload"]["verdict"] for e in trace.of_type("verdict")] == ["refuted", "supported"]
    assert state["status"] == "applied" and state["attempts"] == 2
    assert state["sandbox"].reproduced and state["sandbox"].passed and "promo.percent) / 100" in state["sandbox"].diff
    tools = {e["payload"]["tool"] for e in trace.of_type("tool_call")}
    assert {"search_logs", "git_log", "read_file"} <= tools


# ---- the web intake ---------------------------------------------------------------------
@needs_node
def test_grafana_webhook_opens_a_quickbite_incident(tmp_path, monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    from tests.test_api import sign_in, wait_for
    monkeypatch.setenv("INCIDENT_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("TWO_PERSON_APPROVAL", "0")
    import ui.app as appmod
    appmod = importlib.reload(appmod)
    build("quickbite_orders")
    ws = Workspace(SCENARIOS / "quickbite_orders")
    appmod.runner.llm_factory = lambda: ScriptedLLM(quickbite_script(ws) + [
        [call("submit_result", {"business_summary": "b", "summary": "s", "impact": "i", "root_cause": "r", "follow_ups": []})]])
    c = TestClient(appmod.app)

    gf = json.loads((SCENARIOS / "quickbite_orders" / "alerts" / "grafana_alert.json").read_text(encoding="utf-8"))
    r = c.post("/alerts", json=gf)
    assert r.status_code == 201 and r.json()["received_from"] == "grafana" and r.json()["service_folder"] == "quickbite_orders"
    iid = r.json()["id"]
    inc = c.get(f"/incidents/{iid}").json()
    assert inc["severity"] == "sev1" and inc["assignee"] == "Meera" and inc["alert"]["source"] == "Grafana Alerting"
    assert inc["required_approvals"]["label"] == "A team lead"
    wait_for(c, iid, "awaiting_approval")
    sign_in(c, "Meera")
    assert c.post(f"/incidents/{iid}/approve", json={}).json() == {"complete": False, "still_needed": "a team lead"}
    sign_in(c, "Yuvika")
    assert c.post(f"/incidents/{iid}/approve", json={}).json()["complete"] is True

    services = {s["key"]: s for s in c.get("/connect/services").json()}
    assert services["quickbite_orders"]["check"]["ok"] and services["quickbite_orders"]["alerts"] == "grafana"
    sign_in(c, "Tara")
    assert c.post("/connect/services/quickbite_orders/check").status_code == 403  # running checks needs a developer or lead
    sign_in(c, "Siddhartha")
    assert c.post("/connect/services/quickbite_orders/check?full=true").json()["ok"]
    t = c.post("/connect/services/incident1_conn_leak/test-alert")  # a recorded incident, never the live shop's repo
    assert t.status_code == 201 and t.json()["service_folder"] == "incident1_conn_leak" and t.json()["received_from"] == "alertmanager"
