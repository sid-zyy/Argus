"""Incident tracker API + static UI.

uvicorn ui.app:app --port 8000 --reload      then open http://localhost:8000

Sign-in is a demo user picker (no passwords); the chosen user is kept in a signed cookie and every
write checks that user's role on the server.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from connect.adapters import alerts as alert_adapters
from connect.config import ConfigError
from connect.schema import IncidentInput
from connect.validate import check as check_config
from db.store import Store
from tools.workspace import Workspace
from ui.runner import ROOT, Denied, IncidentRunner

STATIC = Path(__file__).parent / "static"
SSE_POLL = 0.4  # seconds between store polls while a stream has nothing new
COOKIE = "argus_user"
SECRET = os.getenv("ARGUS_SECRET", "argus-demo-secret").encode()
SEVERITIES = ("sev1", "sev2", "sev3", "sev4")

store = Store(os.getenv("INCIDENT_DB", ROOT / "incidents.db"))
store.mark_interrupted()
runner = IncidentRunner(store, replay_speed=float(os.getenv("REPLAY_SPEED", "2")))

app = FastAPI(title="Argus incident tracker")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    """Make browsers revalidate pages and assets, so UI changes show up on a normal reload."""
    resp = await call_next(request)
    if not request.url.path.endswith("/events"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---- session -------------------------------------------------------------------
def _sign(uid: int) -> str:
    return f"{uid}.{hmac.new(SECRET, str(uid).encode(), hashlib.sha256).hexdigest()[:32]}"


def current_user(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE, "")
    uid, _, _ = raw.partition(".")
    if uid.isdigit() and hmac.compare_digest(raw, _sign(int(uid))):
        return store.get_user(int(uid))
    return None


def require(*roles: str):
    def dep(request: Request) -> dict:
        user = current_user(request)
        if not user:
            raise HTTPException(401, "sign in first (pick a user in the top-right menu)")
        if roles and user["role"] not in roles:
            raise HTTPException(403, f"this needs the {' or '.join(roles)} role; you are signed in as a {user['role']}")
        return user
    return dep


signed_in = require()


class SessionIn(BaseModel):
    user_id: int


@app.get("/users")
def users():
    return store.users()


@app.get("/session/me")
def me(request: Request):
    return current_user(request)


@app.post("/session")
def sign_in(body: SessionIn, response: Response):
    user = store.get_user(body.user_id)
    if not user:
        raise HTTPException(404, "no such user")
    response.set_cookie(COOKIE, _sign(user["id"]), httponly=True, samesite="lax", max_age=7 * 86400)
    return user


@app.delete("/session")
def sign_out(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---- models --------------------------------------------------------------------
class AlertIn(BaseModel):
    scenario: str = Field(description="Which synthetic environment the alert belongs to")
    alert: dict = Field(default_factory=dict, description="Optional overrides for the scenario's alert fields")
    mode: str = "live"


class BugIn(BaseModel):
    scenario: str = Field(description="The service/environment the bug was seen in")
    title: str = Field(min_length=4, max_length=200)
    severity: str = "sev3"
    repro_steps: str = Field(min_length=1, max_length=4000)
    expected: str = Field(min_length=1, max_length=2000)
    actual: str = Field(min_length=1, max_length=2000)
    environment: str = ""
    log_snippet: str = Field(default="", max_length=8000)


class Decision(BaseModel):
    reason: str = ""


class QaIn(BaseModel):
    verified: bool
    note: str = ""


class RerunIn(BaseModel):
    note: str = ""


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class IncidentPatch(BaseModel):
    assignee_id: int | None = None
    severity: str | None = None


def _incident_or_404(iid: int) -> dict:
    inc = store.get_incident(iid)
    if not inc:
        raise HTTPException(404, f"INC-{iid} not found")
    return inc


def _denied(e: Denied):
    raise HTTPException(e.status, str(e))


# ---- pages ---------------------------------------------------------------------
def _page(name: str) -> HTMLResponse:
    """Serve a page with its /static assets stamped by modification time, so an edited file always gets a new URL."""
    html = (STATIC / name).read_text(encoding="utf-8")
    stamp = lambda m: f'{m.group(1)}?v={int((STATIC / m.group(2)).stat().st_mtime)}"'  # noqa: E731
    return HTMLResponse(re.sub(r'(/static/([\w.-]+\.(?:js|css)))"', stamp, html))


@app.get("/", include_in_schema=False)
def index():
    return _page("index.html")


@app.get("/results", include_in_schema=False)
def results_page():
    return _page("results.html")


@app.get("/results/data")
def results_data():
    """Argus (scored from the recorded runs) next to a single chatbot prompt (eval/baseline.json)."""
    from eval.compare import BASELINE, argus_from_replay
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.is_file() else {"rows": []}
    by_name = {r["scenario"]: r for r in baseline["rows"]}
    rows = []
    # only incidents the chatbot was also run on, so the two columns stay a like-for-like comparison
    for s in (s for s in runner.scenarios() if not s["live_app"] and s["name"] in by_name):
        rows.append({"scenario": s["name"], "service": s["service"], "title": s["title"],
                     "argus": argus_from_replay(s["name"]), "chatbot": by_name.get(s["name"])})
    return {"rows": rows, "baseline_recorded_at": baseline.get("recorded_at")}


@app.get("/incidents/{iid}/view", include_in_schema=False)
def ticket_page(iid: int):
    _incident_or_404(iid)
    return _page("ticket.html")


# ---- intake --------------------------------------------------------------------
@app.post("/alerts", status_code=201)
def post_alert(body: dict):
    """Webhook intake from monitoring. Argus Connect's alert adapters accept Prometheus Alertmanager, Grafana Alerting and
    the standard incident format, and route the alert to the connected service whose argus.yaml matches its labels; the
    older {"scenario": ..., "alert": {...}} shape still works. Unauthenticated, like a real alert webhook on an internal
    network. While Argus is paused, alerts are counted and ignored."""
    state = pause_state()
    if state["paused"]:
        first = next(iter(body.get("alerts") or []), {})
        title = (first.get("annotations") or {}).get("summary") or (body.get("alert") or {}).get("title") or body.get("title")             or body.get("scenario") or "Alert"
        store.set_setting("pause", {**state, "held": state["held"] + 1,
                                    "last_held": {"title": title, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}})
        return JSONResponse({"paused": True, "detail": "Argus is paused, so no incident was opened"}, status_code=202)
    if alert_adapters.detect(body):
        return _route(body)
    try:
        parsed = AlertIn.model_validate(body)
    except ValueError as e:
        raise HTTPException(422, f"not an Alertmanager, Grafana or standard incident payload, or {{scenario, alert}}: {e}")
    return _start(parsed.scenario, parsed.mode, parsed.alert)


def _route(payload: dict) -> dict:
    """Normalise a monitoring payload with its adapter and open an incident on the service it belongs to."""
    services = []
    for s in runner.scenarios():
        try:
            services.append(runner.config(s["name"]))
        except ConfigError:
            pass
    try:
        cfg, inc, fmt = alert_adapters.route(payload, services)
    except alert_adapters.RouteError as e:
        raise HTTPException(e.status, str(e))
    return {**_start(cfg.key, "live", inc.as_alert()), "received_from": fmt, "service_folder": cfg.key}


@app.post("/demo/trigger/{scenario}", status_code=201)
def demo_trigger(scenario: str, mode: str = Query("live", pattern="^(live|replay)$"), user: dict = Depends(signed_in)):
    return _start(scenario, mode, None)


@app.post("/bugs", status_code=201)
def file_bug(body: BugIn, user: dict = Depends(require("tester"))):
    if body.severity not in SEVERITIES:
        raise HTTPException(422, "severity must be sev1..sev4")
    return _start(body.scenario, "live", None, reporter=user, report=body.model_dump())


def _start(scenario: str, mode: str, override: dict | None, reporter: dict | None = None, report: dict | None = None) -> dict:
    if scenario not in {s["name"] for s in runner.scenarios()}:
        raise HTTPException(404, f"unknown scenario {scenario!r}")
    if mode not in ("live", "replay"):
        raise HTTPException(422, "mode must be live or replay")
    try:
        iid = runner.start(scenario, mode, override, reporter=reporter, report=report)
    except FileNotFoundError as e:
        raise HTTPException(409, str(e))
    except Denied as e:  # e.g. the service's argus.yaml fails its checks
        _denied(e)
    return {"id": iid, "key": f"INC-{iid}", "url": f"/incidents/{iid}/view"}


# ---- pause switch ----------------------------------------------------------------
# For rehearsing: while paused, monitoring alerts are ignored (counted, not investigated). Demo incidents started by
# hand still run, and incidents already running carry on.
def pause_state() -> dict:
    return {"paused": False, "by": None, "at": None, "held": 0, "last_held": None, **(store.get_setting("pause") or {})}


@app.get("/argus/state")
def argus_state():
    return pause_state()


@app.post("/argus/pause")
def pause(user: dict = Depends(require("developer", "lead"))):
    state = {"paused": True, "by": user["name"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "held": 0, "last_held": None}
    store.set_setting("pause", state)
    return state


@app.post("/argus/resume")
def resume(user: dict = Depends(require("developer", "lead"))):
    store.set_setting("pause", {**pause_state(), "paused": False, "by": user["name"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return pause_state()


# ---- live demo app (ShopFast) ---------------------------------------------------
SHOP_URL = os.getenv("SHOP_URL", "http://localhost:8100").rstrip("/")


def _shop(path: str, method: str = "GET") -> dict:
    req = urllib.request.Request(f"{SHOP_URL}{path}", method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            detail = json.loads(body).get("detail", str(e))
        except ValueError:  # e.g. a plain-text "Internal Server Error"
            detail = f"ShopFast had an error ({e.code}). Check its terminal, or click Reset."
        raise HTTPException(e.code, detail)
    except OSError:
        raise HTTPException(503, f"ShopFast is not running. Start it with: python -m shop.server")


@app.get("/live/status")
def live_status():
    try:
        return {**_shop("/api/status"), "url": SHOP_URL}
    except HTTPException as e:
        return {"online": False, "url": SHOP_URL, "error": e.detail if e.status_code != 503 else None}


@app.post("/live/break")
def live_break(user: dict = Depends(require("developer", "lead"))):
    return _shop("/admin/break", "POST")


@app.get("/live/commits/{sha}")
def live_commit(sha: str):
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        raise HTTPException(422, "bad sha")
    return _shop(f"/api/commits/{sha}")


@app.post("/live/reset")
def live_reset(user: dict = Depends(require("developer", "lead"))):
    return _shop("/admin/reset", "POST")


@app.get("/scenarios")
def scenarios():
    return runner.scenarios()


# ---- Argus Connect: connected services, their config checks, test alerts ------------------
@app.get("/connect", include_in_schema=False)
def connect_page():
    return _page("connect.html")


def _config_or_404(key: str):
    if key not in {s["name"] for s in runner.scenarios()}:
        raise HTTPException(404, f"no service folder {key!r}")
    try:
        return runner.config(key)
    except ConfigError as e:
        raise HTTPException(422, str(e))


@app.get("/connect/services")
def connect_services():
    """Every service with its adapters and the result of the quick checks that gate each incident."""
    users = [u["name"] for u in store.users()]
    out = []
    for s in runner.scenarios():
        if s.get("config_error"):
            out.append({"key": s["name"], "service": s["name"], "check": {"ok": False, "errors": 1, "warnings": 0,
                        "checks": [{"status": "error", "area": "config", "message": s["config_error"]}]}})
            continue
        cfg = runner.config(s["name"])
        sample = None
        if cfg.alerts.sample:
            try:
                sample = cfg.sample_payload()
            except (OSError, ValueError):
                pass
        out.append({**cfg.summary(), "title": s["title"], "replay_available": s["replay_available"], "live_app": s["live_app"],
                    "commands": cfg.commands.model_dump(include={"repro", "test", "image"}),
                    "severity": cfg.severity.model_dump(), "metrics_source": cfg.metrics_source,
                    "logs": [l.model_dump(include={"name", "adapter", "role", "path", "run", "dependency"}) for l in cfg.logs],
                    "dependencies": [d.model_dump() for d in cfg.dependencies],
                    "sample_alert": {"adapter": alert_adapters.detect(sample), "payload": sample} if sample else None,
                    "check": check_config(cfg, users=users).as_dict()})
    return out


@app.post("/connect/services/{key}/check")
def connect_check(key: str, full: bool = False, user: dict = Depends(require("developer", "lead"))):
    """Run the setup wizard's checks. full=true also runs the repro and tests once in the sandbox."""
    cfg = _config_or_404(key)
    return check_config(cfg, full=full, users=[u["name"] for u in store.users()]).as_dict()


@app.post("/connect/services/{key}/test-alert", status_code=201)
def connect_test_alert(key: str, user: dict = Depends(require("developer", "lead"))):
    """Send the service's sample payload through the same adapter and routing a real webhook uses, and open the incident.
    Started by hand, so it runs even while monitoring alerts are paused."""
    cfg = _config_or_404(key)
    if not cfg.alerts.sample:
        raise HTTPException(409, f"{key} has no alerts.sample payload in its argus.yaml")
    payload = cfg.sample_payload()
    # several folders can watch the same service (e.g. ShopFast's recorded incidents), so name this one explicitly
    if isinstance(payload.get("alerts"), list):
        payload = {**payload, "alerts": [{**a, "labels": {**a.get("labels", {}), "scenario": key}} for a in payload["alerts"]]}
    else:
        payload = {**payload, "labels": {**payload.get("labels", {}), "scenario": key}}
    return _route(payload)


@app.get("/connect/schema")
def connect_schema():
    """The standard incident input every alert adapter produces, as JSON Schema."""
    return IncidentInput.model_json_schema()


# ---- dashboard -----------------------------------------------------------------
@app.get("/metrics")
def metrics():
    return store.metrics()


@app.get("/notifications")
def notifications(after: int = 0, limit: int = Query(20, le=50), user: dict = Depends(signed_in)):
    """What Argus told people: new incidents, causes found, fixes waiting, deploys. Shown as in-app and desktop notifications."""
    return store.notifications(after, limit)


@app.get("/events/recent")
def recent_events(limit: int = Query(40, le=200)):
    return store.recent_events(limit)


# ---- incidents -----------------------------------------------------------------
@app.get("/incidents")
def list_incidents():
    return store.list_incidents()


@app.get("/incidents/{iid}")
def get_incident(iid: int):
    return {**_incident_or_404(iid), "approval_sla_minutes": runner.sla_minutes}


@app.patch("/incidents/{iid}")
def patch_incident(iid: int, body: IncidentPatch, user: dict = Depends(require("developer", "lead"))):
    _incident_or_404(iid)
    changes = {}
    if body.severity is not None:
        if body.severity not in SEVERITIES:
            raise HTTPException(422, "severity must be sev1..sev4")
        changes["severity"] = body.severity
    if body.assignee_id is not None:
        assignee = store.get_user(body.assignee_id) if body.assignee_id else None
        if body.assignee_id and (not assignee or assignee["role"] == "tester"):
            raise HTTPException(422, "assign to a developer or lead")
        changes["assignee_id"] = assignee["id"] if assignee else None
        changes["assignee"] = assignee["name"] if assignee else None
    store.update_incident(iid, **changes)
    if changes:
        store.record_event(iid, {"actor": "human", "type": "updated",
                                 "payload": {"by": user["name"], **{k: v for k, v in changes.items() if k != "assignee_id"}}}, user)
    return _incident_or_404(iid)


class ArchiveIn(BaseModel):
    statuses: list[str] = Field(default_factory=lambda: ["escalated", "rejected"])


@app.post("/incidents/archive")
def archive_incidents(body: ArchiveIn, user: dict = Depends(require("developer", "lead"))):
    """Clear a board column (by default 'Needs a person'). Archived incidents keep their full history."""
    allowed = {"escalated", "rejected", "resolved", "closed"}
    if not body.statuses or not set(body.statuses) <= allowed:
        raise HTTPException(422, f"can only clear finished incidents: {sorted(allowed)}")
    ids = store.archive(tuple(body.statuses), user)
    return {"archived": len(ids), "ids": ids}


@app.post("/incidents/{iid}/approve")
def approve(iid: int, body: Decision, user: dict = Depends(signed_in)):
    try:
        return runner.decide(iid, user, True, body.reason)
    except Denied as e:
        _denied(e)


@app.post("/incidents/{iid}/reject")
def reject(iid: int, body: Decision, user: dict = Depends(signed_in)):
    try:
        return runner.decide(iid, user, False, body.reason)
    except Denied as e:
        _denied(e)


@app.post("/incidents/{iid}/qa")
def qa(iid: int, body: QaIn, user: dict = Depends(signed_in)):
    try:
        runner.qa(iid, user, body.verified, body.note)
    except Denied as e:
        _denied(e)
    return {"ok": True}


@app.post("/incidents/{iid}/rerun")
def rerun(iid: int, body: RerunIn, user: dict = Depends(require("developer", "lead"))):
    inc = _incident_or_404(iid)
    if inc["status"] not in ("escalated", "rejected"):
        raise HTTPException(409, "only escalated or rejected incidents can be re-run")
    try:
        runner.rerun(iid, user, body.note)
    except Denied as e:
        _denied(e)
    return {"ok": True}


@app.post("/incidents/{iid}/comment", status_code=201)
def comment(iid: int, body: CommentIn, user: dict = Depends(signed_in)):
    _incident_or_404(iid)
    c = store.add_comment(iid, user, body.text)
    store.record_event(iid, {"actor": "human", "type": "comment", "payload": {"author": user["name"], "text": body.text}}, user)
    return c


@app.get("/incidents/{iid}/impact-graph")
def impact_graph(iid: int):
    """What else the incident touches (observed vs inferred), how it would spread, and the cost of each way out."""
    from orchestrator.impact_graph import analyse
    inc = _incident_or_404(iid)
    graph = analyse(inc, Workspace(ROOT / "scenarios" / inc["scenario"]))
    if not graph:
        raise HTTPException(404, f"no service map for {inc['service']}")
    return graph


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)


@app.post("/incidents/{iid}/ask")
def ask(iid: int, body: AskIn, user: dict = Depends(signed_in)):
    """Ask Argus about this incident. The answer comes only from the incident's record, with sources, and is kept in the audit log."""
    from llm import LLMError
    from orchestrator.ask import answer
    inc = _incident_or_404(iid)
    try:
        reply = answer(inc, body.question.strip())
    except LLMError as e:
        raise HTTPException(503, f"No AI service could answer right now: {str(e)[:200]}")
    eid = store.record_event(iid, {"actor": "human", "type": "question",
                                   "payload": {"by": user["name"], "question": body.question.strip(), **reply}}, user)
    return {"id": eid, **reply}


@app.get("/incidents/{iid}/report")
def report(iid: int, download: bool = False):
    inc = _incident_or_404(iid)
    if not inc["report"]:
        raise HTTPException(404, "report not ready")
    headers = {"Content-Disposition": f'attachment; filename="INC-{iid}-report.md"'} if download else {}
    return PlainTextResponse(inc["report"], media_type="text/markdown; charset=utf-8", headers=headers)


@app.get("/incidents/{iid}/commits/{sha}")
def commit(iid: int, sha: str):
    """The suspect commit's diff, for the ticket page's suspect panel."""
    inc = _incident_or_404(iid)
    if not sha.isalnum() or len(sha) > 40:
        raise HTTPException(422, "bad sha")
    ws = Workspace(ROOT / "scenarios" / inc["scenario"])
    try:
        return PlainTextResponse(ws.git("show", "--no-color", "--format=medium", sha))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/incidents/{iid}/events")
async def events(iid: int, request: Request, after: int = 0):
    """SSE stream of the incident's audit log, from `after` (or Last-Event-ID) onwards, then live."""
    _incident_or_404(iid)
    last = int(request.headers.get("last-event-id") or after)

    async def gen():
        nonlocal last
        while not await request.is_disconnected():
            batch = await asyncio.to_thread(store.events_after, iid, last)
            for e in batch:
                last = e["id"]
                yield {"id": str(e["id"]), "data": json.dumps(e, default=str)}
            if not batch:
                await asyncio.sleep(SSE_POLL)

    return EventSourceResponse(gen(), ping=15)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

