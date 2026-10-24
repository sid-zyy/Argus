"""Runs incidents in background threads for the web app: live (real agents) or replay (a saved trace).

Both feed the same store, so the UI cannot tell them apart except by the mode label. Either way the
run blocks at the approval gate until the required humans approve (or one rejects).
"""
from __future__ import annotations

import json
import re
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from connect.adapters.repos import repo_for
from connect.config import ConfigError, ServiceConfig, load, service_folders
from connect.intake import alert_for
from connect.validate import check as check_config
from db.store import Store, required_approvals
from llm import LLM
from orchestrator import policy
from orchestrator.pipeline import run_incident
from orchestrator.report import build_report, impact_section
from orchestrator.state import FixOutput
from orchestrator.trace import Trace
from scenarios.build import build
from tools.sandbox import preview_fix
from tools.workspace import Workspace

ROOT = Path(__file__).resolve().parent.parent
incKey = lambda iid: f"INC-{iid}"  # noqa: E731
REPLAY_DELAY_CAP = 3.5  # seconds between replayed events, at most
REPLAY_MIN_GAP = 0.5    # seconds, so bursts of steps stay readable
# handoffs between agents get time for their animation to finish
REPLAY_HANDOFF_GAP = 2.2
HANDOFFS = {"plan", "claim", "verdict", "suspects", "patch", "sandbox", "awaiting_approval", "rejected"}


class Denied(Exception):
    """A human action the rules do not allow (wrong role, wrong state, separation of duties)."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


class _Gate:
    def __init__(self):
        self.event = threading.Event()
        self.decision: dict | None = None


class IncidentRunner:
    def __init__(self, store: Store, replay_speed: float = 2.0, llm_factory=LLM):
        self.store = store
        self.replay_speed = replay_speed
        self.llm_factory = llm_factory
        self._gates: dict[int, _Gate] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._watch_sla, daemon=True, name="approval-sla").start()

    @property
    def sla_minutes(self) -> float:
        """A fix waiting longer than this for approval is escalated to the lead (0 turns it off). From policy.yaml."""
        return policy.sla_minutes()

    # ---- public ------------------------------------------------------------
    def scenarios(self) -> list[dict]:
        """Every service Argus can run: connected services (argus.yaml) and recorded scenarios."""
        out = []
        for d in service_folders():
            try:
                cfg = load(d)
                alert = cfg.base_alert()
            except (ConfigError, OSError, ValueError) as e:
                out.append({"name": d.name, "service": d.name, "title": d.name, "replay_available": False, "live_app": None,
                            "connect": None, "config_error": str(e)})
                continue
            out.append({"name": d.name, "service": cfg.service.name, "title": alert.get("title", cfg.service.label or d.name),
                        "replay_available": self.replay_file(d.name) is not None, "live_app": self.live_app(d.name),
                        "connect": cfg.summary()})
        return out

    @staticmethod
    def config(scenario: str) -> ServiceConfig:
        return load(ROOT / "scenarios" / scenario)

    @staticmethod
    def live_app(scenario: str) -> dict | None:
        """A service backed by a running app (argus.yaml `deploy.on_approve`, or live.json): approved fixes are really deployed."""
        try:
            deploy = load(ROOT / "scenarios" / scenario).deploy
        except (ConfigError, OSError, ValueError):
            return None
        if deploy.on_approve:
            return {"app": deploy.app or scenario, "url": deploy.url, "deploy_on_approve": True}
        return None

    def replay_file(self, scenario: str) -> Path | None:
        p = ROOT / "replay" / f"{scenario}.json"
        return p if p.is_file() else None

    def recorded_fix(self, scenario: str) -> FixOutput | None:
        """The fix a recorded run proposed (its last patch)."""
        path = self.replay_file(scenario)
        if not path:
            return None
        patch = next((e["payload"] for e in reversed(json.loads(path.read_text(encoding="utf-8"))) if e["type"] == "patch"), None)
        return FixOutput.model_validate({k: patch[k] for k in ("fix_type", "rationale", "edits", "rollback_to") if k in patch}) if patch else None

    def replay_fits(self, scenario: str) -> bool:
        """A recorded run may stand in for the agents on a live app only if its fix applies to the code that is live now,
        i.e. the same bug was pushed. Otherwise the real agents investigate."""
        fix = self.recorded_fix(scenario)
        return bool(fix) and not preview_fix(Workspace(ROOT / "scenarios" / scenario), fix)[1]

    def start(self, scenario: str, mode: str = "live", alert_override: dict | None = None,
              reporter: dict | None = None, report: dict | None = None) -> int:
        scenario_dir = self._scenario_dir(scenario)
        cfg = load(scenario_dir)
        checked = check_config(cfg)
        if not checked.ok:  # the setup wizard's checks gate every incident
            raise Denied(f"{scenario}'s configuration has {len(checked.errors)} error(s): "
                         + "; ".join(c.message for c in checked.errors[:3]) + f". Run: python -m connect.wizard check {scenario}", 422)
        source = "alert"
        if report:  # a tester's bug report becomes a synthetic alert; the symptoms are what the agents start from
            source = "tester_report"
            alert = {"id": "BUG", "service": cfg.service.name, "title": report["title"], "severity": report.get("severity", "sev3"),
                     "metric": None, "value": None, "threshold": None, "window": None, "started_at": None,
                     "fired_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "source": f"bug report by {reporter['name']}"}
            alert.update(alert_override or {})
        else:  # the base alert, overlaid with what the monitoring tool sent, with the service's severity policy applied
            alert = alert_for(cfg, alert_override)
        if mode == "replay" and not self.replay_file(scenario):
            raise FileNotFoundError(f"no saved replay for {scenario}; record one with --save-replay")
        iid = self.store.create_incident(alert, scenario, mode, source=source,
                                         reported_by=reporter["id"] if reporter else None, report=report)
        if report:
            alert["id"] = f"BUG-{iid}"
            self.store.update_incident(iid, alert=alert)
        self._assign(iid, alert["service"], cfg.service.on_call)
        self._launch(iid, scenario_dir, mode)
        return iid

    # ---- workflow automation: on-call assignment, notifications, approval SLA -------
    def _assign(self, iid: int, service: str, configured: str | None = None) -> None:
        """The on-call roster first, then the on_call named in the service's argus.yaml."""
        person = self.store.on_call_for(service) or self.store.user_by_name(configured)
        if person:
            self.store.update_incident(iid, assignee=person["name"], assignee_id=person["id"])
            self.store.record_event(iid, {"actor": "orchestrator", "type": "assigned",
                                          "payload": {"to": person["name"], "reason": f"On call for {service}"}})

    def _send(self, iid: int, to: str, message: str, kind: str = "info") -> None:
        """Simulated chat notification, recorded in the audit log like a real one would be. The app also shows it to
        signed-in people as an in-app (and, if they allow it, desktop) notification."""
        self.store.record_event(iid, {"actor": "orchestrator", "type": "notified",
                                      "payload": {"channel": "Slack #incidents", "to": to, "message": message, "kind": kind, "simulated": True}})

    def _record(self, iid: int, event: dict) -> None:
        self.store.record_event(iid, event)
        t, p = event["type"], event["payload"]
        if t not in ("incident_opened", "verdict", "awaiting_approval", "approval", "escalated", "deployed"):
            return
        if t == "verdict" and p.get("verdict") != "supported":
            return
        inc = self.store.get_incident(iid)
        owner = inc["assignee"] or "on-call"
        lead = (self.store.lead() or {}).get("name", "the lead")
        if t == "incident_opened":
            self._send(iid, owner, f"New incident {incKey(iid)}: {inc['title']}. Argus is investigating.", "opened")
        elif t == "verdict":
            claim = next((c for c in inc["claims"] if c["claim_key"] == p.get("claim_id")), None)
            if claim and sum(c["verdict"] == "supported" for c in inc["claims"]) == 1:  # only the first confirmed cause
                self._send(iid, owner, f"{incKey(iid)}: cause found. {claim['statement']}", "cause")
        elif t == "deployed":
            self._send(iid, "#incidents", f"{incKey(iid)}: approved fix deployed to {p.get('app')} as commit {p.get('commit')}.", "deployed")
        elif t == "awaiting_approval":
            who = f"{owner} and {lead}" if inc["required_approvals"]["count"] > 1 else owner
            self._send(iid, who, f"{incKey(iid)}: a tested fix is ready for approval"
                                 + (f" within {self.sla_minutes:.0f} minutes." if self.sla_minutes else "."), "approval")
        elif t == "approval":
            self._send(iid, "#incidents", f"{incKey(iid)}: fix {'approved' if p.get('approved') else 'declined'} by {p.get('approver')}.", "decision")
        elif t == "escalated":
            self._send(iid, lead, f"{incKey(iid)} needs a person: {p.get('reason')}", "escalated")

    def _watch_sla(self) -> None:
        while True:
            time.sleep(10)
            try:
                if self.sla_minutes <= 0:
                    continue
                for r in self.store.awaiting_approval():
                    waited = (datetime.now(timezone.utc) - datetime.fromisoformat(r["since"])).total_seconds() / 60
                    if not r["breached"] and waited >= self.sla_minutes:
                        lead = (self.store.lead() or {}).get("name", "the lead")
                        self.store.record_event(r["id"], {"actor": "orchestrator", "type": "sla_breach",
                                                          "payload": {"minutes": self.sla_minutes, "escalated_to": lead,
                                                                      **policy.ref("approval_sla_minutes")}})
                        self._send(r["id"], lead, f"{incKey(r['id'])}: fix has waited {self.sla_minutes:.0f} minutes for approval. Please review.", "sla")
            except Exception:
                pass

    def rerun(self, iid: int, user: dict, note: str = "") -> None:
        inc = self.store.get_incident(iid)
        if inc["mode"] != "live":
            raise Denied("only live incidents can be re-run", 409)
        self.store.update_incident(iid, status="open", resolved_at=None, escalation=None)
        self.store.record_event(iid, {"actor": "human", "type": "rerun", "payload": {"by": user["name"], "note": note}}, user)
        with self._lock:
            self._gates.pop(iid, None)
        self._launch(iid, self._scenario_dir(inc["scenario"]), "live", note)

    def decide(self, iid: int, user: dict, approved: bool, reason: str) -> dict:
        """Record one human vote. Returns the approval state; raises Denied when the vote is not allowed."""
        inc = self.store.get_incident(iid)
        if not inc:
            raise Denied("incident not found", 404)
        if inc["status"] != "awaiting_approval":
            raise Denied("this incident is not awaiting approval", 409)
        if user["role"] not in ("developer", "lead"):
            raise Denied("only a developer or lead can approve or reject a fix")
        need = required_approvals(inc["severity"], inc["scenario"])
        if need["count"] > 1 and policy.separation_of_duties() and inc["reported_by"] == user["id"]:
            raise Denied("separation of duties: you reported this issue, so someone else must approve the fix")
        if any(v["user_id"] == user["id"] for v in inc["votes"]):
            raise Denied("you have already voted on this fix", 409)
        if not approved and not reason.strip():
            raise Denied("a rejection needs a reason", 422)
        decision = "approved" if approved else "rejected"
        self.store.add_vote(iid, user, decision, reason)
        self.store.record_event(iid, {"actor": "human", "type": "approval_vote",
                                      "payload": {"approver": user["name"], "role": user["role"], "decision": decision, "reason": reason,
                                                  **policy.ref(need["rule"])}}, user)
        votes = self.store.get_incident(iid)["votes"]
        gate = self._gate(iid)
        if not approved:
            gate.decision = {"approved": False, "approver": user["name"], "role": user["role"], "reason": reason, **policy.ref(need["rule"])}
            gate.event.set()
            return {"complete": True, "approved": False}
        yes = [v for v in votes if v["decision"] == "approved"]
        if len(yes) >= need["count"] and (not need["lead"] or any(v["role"] == "lead" for v in yes)):
            gate.decision = {"approved": True, "approver": ", ".join(v["name"] for v in yes),
                             "role": " + ".join(v["role"] for v in yes), "reason": "; ".join(v["reason"] for v in yes if v["reason"]),
                             "approvers": [{"name": v["name"], "role": v["role"]} for v in yes], **policy.ref(need["rule"])}
            gate.event.set()
            return {"complete": True, "approved": True}
        return {"complete": False, "still_needed": "a team lead" if need["lead"] and not any(v["role"] == "lead" for v in yes) else "one more approver"}

    def qa(self, iid: int, user: dict, verified: bool, note: str) -> None:
        inc = self.store.get_incident(iid)
        if not inc:
            raise Denied("incident not found", 404)
        if user["role"] != "tester":
            raise Denied("only a tester can confirm a fix")
        if inc["status"] != "awaiting_qa":
            raise Denied("this incident is not awaiting QA", 409)
        result = "verified" if verified else "still_failing"
        self.store.add_qa(iid, user, result, note)
        self.store.record_event(iid, {"actor": "human", "type": "qa", "payload": {"tester": user["name"], "result": result, "note": note}}, user)
        if verified:
            self.store.update_incident(iid, status="closed")
        else:
            self.rerun(iid, user, f"Tester reports the issue is still failing after the fix: {note}")

    # ---- internals ---------------------------------------------------------
    def _scenario_dir(self, scenario: str) -> Path:
        """The service folder, with its repo ready: built from history.py for a sample, cloned for a remote repo."""
        d = ROOT / "scenarios" / scenario
        cfg = load(d)
        if not (cfg.repo_path() / ".git").exists():
            if (d / "history.py").is_file():
                build(scenario)
            else:
                repo_for(cfg).ensure()
        return d

    def _launch(self, iid: int, scenario_dir: Path, mode: str, note: str = "") -> None:
        target = self._live if mode == "live" else self._replay
        threading.Thread(target=self._guard, args=(target, iid, scenario_dir, note), daemon=True, name=f"INC-{iid}").start()

    def _gate(self, iid: int) -> _Gate:
        with self._lock:
            return self._gates.setdefault(iid, _Gate())

    def _wait_for_human(self, iid: int) -> dict:
        gate = self._gate(iid)
        gate.event.wait()
        return gate.decision

    def _guard(self, target, iid: int, scenario_dir: Path, note: str) -> None:
        try:
            target(iid, scenario_dir, note)
        except Exception as e:  # a crashed run must not leave the ticket stuck in 'investigating'
            self.store.record_event(iid, {"actor": "orchestrator", "type": "error", "payload": {"message": repr(e)}})
            self.store.record_event(iid, {"actor": "orchestrator", "type": "escalated", "payload": {"reason": f"Pipeline crashed: {e}"}})

    def _workspace(self, iid: int, scenario_dir: Path, note: str) -> Workspace:
        inc = self.store.get_incident(iid)
        ws = Workspace(scenario_dir)
        ws.alert = inc["alert"]
        if inc["source"] == "tester_report":
            lines = [f"Bug report by {inc['reported_by_name']}: {inc['title']}",
                     f"Environment: {inc['environment'] or '-'}", "Steps to reproduce:"]
            lines += [f"  {s}" for s in (inc["repro_steps"] or "-").splitlines()]
            lines += [f"Expected: {inc['expected'] or '-'}", f"Actual: {inc['actual'] or '-'}"]
            if inc["log_snippet"]:
                lines += ["Log snippet from the tester:"] + [f"  {s}" for s in inc["log_snippet"].splitlines()]
            if note:
                lines.append(f"Follow-up: {note}")
            ws.extra_sources["report"] = lines
        elif note:
            ws.extra_sources["report"] = [f"Follow-up from a human: {note}"]
        return ws

    def _live(self, iid: int, scenario_dir: Path, note: str) -> None:
        ws = self._workspace(iid, scenario_dir, note)
        llm = self.llm_factory()
        trace = Trace([lambda e: self._record(iid, e)])
        state = run_incident(ws, llm, trace, lambda ctx: self._wait_for_human(iid), claim_offset=self.store.claim_count(iid))
        live = self.live_app(scenario_dir.name)
        if live and live.get("deploy_on_approve") and state["status"] == "applied":
            self._deploy(iid, ws, state, trace, live)
        md = build_report(ws.alert, state, trace, llm, scenario_dir.name)
        out = ROOT / "runs" / f"{scenario_dir.name}-INC-{iid}-{datetime.now():%Y%m%d-%H%M%S}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "incident_report.md").write_text(md, encoding="utf-8")
        trace.emit("reporter", "report", {"path": str(out / "incident_report.md"), "markdown": md})
        trace.save(out / "trace.json")

    def _deploy(self, iid: int, ws: Workspace, state: dict, trace: Trace, live: dict) -> None:
        """Commit the approved, sandbox-tested fix to the live app's repo; its deploy pipeline picks it up."""
        fix, approval = state["fix"], state["approval"] or {}
        try:
            if fix.fix_type == "rollback":
                ws.git("checkout", fix.rollback_to, "--", ".")
            else:
                for ed in fix.edits:
                    p = ws.repo_dir / ed.file.removeprefix("repo/")
                    p.write_text(p.read_text(encoding="utf-8").replace(ed.old, ed.new, 1), encoding="utf-8", newline="\n")
            ws.git("add", "-A")
            title = textwrap.shorten(fix.rationale.split(". ")[0] or "Fix", 72, placeholder="…")
            ws.git("-c", "user.name=Argus", "-c", "user.email=argus@argus.local", "commit", "-q", "-m",
                   f"{title}\n\nArgus {incKey(iid)}, tested in the sandbox, approved by {approval.get('approver', 'a person')}.")
            sha = ws.git("rev-parse", "--short", "HEAD").strip()
            trace.emit("orchestrator", "deployed", {"commit": sha, "app": live.get("app", "the app"), "message": title})
        except ValueError as e:
            trace.emit("orchestrator", "error", {"message": f"Could not deploy the approved fix: {e}"})

    def _replay(self, iid: int, scenario_dir: Path, note: str) -> None:
        """Play a recorded run back. On a live app (ShopFast) the shop, its alert and the deploy are real: the recorded
        commit ids are mapped to the bad commit that is live now, and an approved fix is really committed and deployed."""
        path = self.replay_file(scenario_dir.name)
        events = json.loads(path.read_text(encoding="utf-8"))
        live = self.live_app(scenario_dir.name)
        ws = Workspace(scenario_dir) if live else None
        shas = self._live_shas(ws, events) if live else {}
        prev = None
        decision = None
        deployed = None
        for e in events:
            t = datetime.fromisoformat(e["ts"])
            if live and e["type"] == "deployed":
                continue  # the recorded deploy happened elsewhere; the real one runs right after the approval
            if prev is not None and self.replay_speed > 0:
                gap = REPLAY_HANDOFF_GAP if e["type"] in HANDOFFS else REPLAY_MIN_GAP
                time.sleep(min(REPLAY_DELAY_CAP, max(gap, (t - prev).total_seconds() / self.replay_speed)))
            prev = t
            payload = _remap(dict(e["payload"]), shas)
            if e["type"] == "approval":
                decision = self._wait_for_human(iid)
                payload = decision
                prev = None  # don't replay the recorded human think-time
            elif e["type"] == "report":
                md = payload.get("markdown") or _report_text(payload.get("path"))
                if md and decision:
                    md = _replace_approval(md, decision, deployed)
                if md:
                    md = _replace_impact(md, self.store.get_incident(iid)["impact"])
                if md and live:  # the title names this incident's alert, not the recorded one
                    md = re.sub(r"^# Incident report: .*$", f"# Incident report: {self.store.get_incident(iid)['title']}", md, count=1, flags=re.M)
                origin = (f"> The agents' work was replayed from a recorded run of this same bug ({path.name}); the app, its alert "
                          "and the deploy were live." if live else f"> Replayed from a recorded run ({path.name}).")
                payload = {"markdown": f"{origin}\n\n{md}" if md else None}
            self._record(iid, {"actor": e["actor"], "type": e["type"], "payload": payload})
            if e["type"] == "approval" and live and live.get("deploy_on_approve") and decision.get("approved"):
                deployed = self._deploy_recorded(iid, ws, scenario_dir.name, decision, live)

    def _live_shas(self, ws: Workspace, events: list[dict]) -> dict[str, str]:
        """Recorded commit id -> the commit that is live now, for the culprit the recorded run named."""
        recorded = next((s["sha"] for e in events if e["type"] == "suspects" for s in e["payload"].get("suspects", [])), None)
        fix = self.recorded_fix(ws.root.name)
        if not recorded or not fix or not fix.edits:
            return {}
        try:
            ws.git("rev-parse", "--verify", f"{recorded}^{{commit}}")
            return {}  # the same commit exists here
        except ValueError:
            pass
        rows = ws.git("log", "-20", "--format=%h%x1f%an", "--", fix.edits[0].file.removeprefix("repo/")).splitlines()
        live_sha = next((sha for sha, author in (r.split("\x1f") for r in rows) if author != "Argus"), None)
        return {recorded[:7]: live_sha} if live_sha else {}

    def _deploy_recorded(self, iid: int, ws: Workspace, scenario: str, decision: dict, live: dict) -> str | None:
        trace = Trace([lambda e: self._record(iid, e)])
        self._deploy(iid, ws, {"fix": self.recorded_fix(scenario), "approval": decision}, trace, live)
        done = trace.of_type("deployed")
        return done[-1]["payload"]["commit"] if done else None


def _remap(payload: dict, shas: dict[str, str]) -> dict:
    """Replace recorded commit ids (short or full) with the live ones throughout an event."""
    if not shas:
        return payload
    text = json.dumps(payload)
    for old, new in shas.items():
        text = re.sub(rf"\b{old}[0-9a-f]*\b", new, text)
    return json.loads(text)


def _report_text(path: str | None) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8") if path else None
    except OSError:
        return None


def _replace_impact(md: str, impact: dict | None) -> str:
    """A recorded report keeps the figures of the day it was recorded; show the current assumptions instead."""
    header = "## Business impact"
    if not impact or header not in md:
        return md
    section = "\n".join(impact_section(impact))
    md = re.sub(re.escape(header) + r"\n.*?(?=\n## |\Z)", lambda _m: section.rstrip("\n") + "\n", md, flags=re.DOTALL)
    # the recorded business summary quotes the old volume, e.g. "affecting 121.6 orders per minute"
    volume = f"{impact['affected_per_minute']:,.0f} {impact['unit']} per minute"
    return re.sub(r"(## Business summary\n.*?)(?=\n## |\Z)",
                  lambda m: re.sub(rf"\d[\d,.]* {re.escape(impact['unit'])} per minute", volume, m.group(1)), md, count=1, flags=re.DOTALL)


def _replace_approval(md: str, d: dict, deployed: str | None = None) -> str:
    applied = (f"The approved fix was committed and deployed as commit {deployed}." if deployed
               else "This is a demo: applying the fix is simulated, no real system was changed.")
    section = (f"## Decisions made by people\n\n- Fix **{'approved' if d['approved'] else 'declined'}** by {d['approver']} ({d['role']}). "
               f"{d.get('reason') or ''}\n- {applied}\n")
    for header in ("## Decisions made by people", "## Approval"):
        if header in md:
            return re.sub(re.escape(header) + r"\n.*?(?=\n## |\Z)", lambda _m: section, md, flags=re.DOTALL)
    return md
