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
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

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
    for s in runner.scenarios():
        rows.append({"scenario": s["name"], "service": s["service"], "title": s["title"],
                     "argus": argus_from_replay(s["name"]), "chatbot": by_name.get(s["name"])})
    return {"rows": rows, "baseline_recorded_at": baseline.get("recorded_at")}


@app.get("/incidents/{iid}/view", include_in_schema=False)
def ticket_page(iid: int):
    _incident_or_404(iid)
    return _page("ticket.html")


# ---- intake --------------------------------------------------------------------
AM_SEVERITY = {"critical": "sev1", "page": "sev1", "high": "sev2", "error": "sev2", "warning": "sev3", "info": "sev4"}


@app.post("/alerts", status_code=201)
def post_alert(body: dict):
    """Webhook intake from monitoring. Accepts Prometheus Alertmanager's webhook format, or {"scenario": ..., "alert": {...}}.
    Unauthenticated, like a real alert webhook on an internal network."""
    if "alerts" in body:  # Alertmanager
        firing = [a for a in body.get("alerts", []) if a.get("status", "firing") == "firing"]
        if not firing:
            raise HTTPException(422, "no firing alerts in the payload")
        a = firing[0]
        labels, notes = a.get("labels", {}), a.get("annotations", {})
        names = {s["name"]: s for s in runner.scenarios()}
        scenario = labels.get("scenario") if labels.get("scenario") in names else next(
            (n for n, s in names.items() if s["service"] == labels.get("service")), None)
        if not scenario:
            raise HTTPException(404, f"no environment for service {labels.get('service')!r}")
        override = {"id": f"AM-{labels.get('alertname', 'alert')}-{a.get('fingerprint', '')[:6]}".rstrip("-"),
                    "title": notes.get("summary") or labels.get("alertname") or "Alert",
                    "severity": AM_SEVERITY.get(str(labels.get("severity", "")).lower(), "sev2"),
                    "source": "Prometheus Alertmanager"}
        return {**_start(scenario, "live", override), "received_from": "alertmanager"}
    try:
        parsed = AlertIn.model_validate(body)
    except ValueError as e:
        raise HTTPException(422, f"not an Alertmanager payload or {{scenario, alert}}: {e}")
    return _start(parsed.scenario, parsed.mode, parsed.alert)


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
    return {"id": iid, "key": f"INC-{iid}", "url": f"/incidents/{iid}/view"}


@app.get("/scenarios")
def scenarios():
    return runner.scenarios()


# ---- dashboard -----------------------------------------------------------------
@app.get("/metrics")
def metrics():
    return store.metrics()


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

