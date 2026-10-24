"""SQLite store. record_event() appends to the audit log and updates the read tables from the event."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from connect.config import SERVICES_DIR, ConfigError, ServiceConfig, load
from connect.policy import service_approvals
from db.business import assumptions, impact
from orchestrator import policy

SCHEMA = Path(__file__).with_name("schema.sql")
FIRST_ID = 101
RUNNING_STATUSES = ("open", "investigating", "awaiting_approval")
OPEN_STATUSES = RUNNING_STATUSES + ("awaiting_qa",)
ROLES = ("tester", "developer", "lead")
DEMO_USERS = [("Tara", "tester"), ("Siddhartha", "developer"), ("Meera", "developer"), ("Yuvika", "lead")]
DEMO_ON_CALL = [("checkout-api", "Siddhartha"), ("catalog-api", "Meera")]
WH_PER_1K_TOKENS = 0.3  # rough public estimate for a large hosted model; shown as an estimate everywhere
# columns added after the first schema version: (table, column, type)
MIGRATIONS = [
    ("incidents", "source", "TEXT NOT NULL DEFAULT 'alert'"), ("incidents", "reported_by", "INTEGER"),
    ("incidents", "assignee_id", "INTEGER"), ("incidents", "repro_steps", "TEXT"), ("incidents", "expected", "TEXT"),
    ("incidents", "actual", "TEXT"), ("incidents", "environment", "TEXT"), ("incidents", "log_snippet", "TEXT"),
    ("events", "actor_id", "INTEGER"), ("events", "actor_role", "TEXT"), ("comments", "author_id", "INTEGER"),
    ("incidents", "archived_at", "TEXT"), ("events", "prev_hash", "TEXT"), ("events", "hash", "TEXT"),
]
EVENT_COLS = ("incident_id", "actor", "type", "payload", "ts", "actor_id", "actor_role")
FABRICATION_HINTS = ("quote not found", "out of range", "unknown source")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA.read_text(encoding="utf-8"))
            self._migrate(c)
            self._seal_unsealed(c)
            if not c.execute("SELECT 1 FROM sqlite_sequence WHERE name='incidents'").fetchone():
                c.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('incidents', ?)", (FIRST_ID - 1,))
            if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                c.executemany("INSERT INTO users (name, role) VALUES (?, ?)", DEMO_USERS)
            if not c.execute("SELECT 1 FROM on_call LIMIT 1").fetchone():
                c.executemany("INSERT INTO on_call (service, user_id) SELECT ?, id FROM users WHERE name=?", DEMO_ON_CALL)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _write(self, fn):
        with self._lock, self._conn() as c:
            return fn(c)

    # ---- users -------------------------------------------------------------
    def users(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT id, name, role FROM users ORDER BY id")]

    def user_by_name(self, name: str | None) -> dict | None:
        if not name:
            return None
        with self._conn() as c:
            r = c.execute("SELECT id, name, role FROM users WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None

    def get_user(self, user_id: int | None) -> dict | None:
        if not user_id:
            return None
        with self._conn() as c:
            r = c.execute("SELECT id, name, role FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(r) if r else None

    # ---- incidents -------------------------------------------------------
    def create_incident(self, alert: dict, scenario: str, mode: str, source: str = "alert",
                        reported_by: int | None = None, report: dict | None = None) -> int:
        report = report or {}

        def tx(c):
            cur = c.execute(
                "INSERT INTO incidents (title, service, severity, status, scenario, mode, alert, created_at, source, reported_by, "
                "repro_steps, expected, actual, environment, log_snippet) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (alert["title"], alert["service"], alert.get("severity", "sev3"), "open", scenario, mode, json.dumps(alert), now(),
                 source, reported_by, report.get("repro_steps"), report.get("expected"), report.get("actual"),
                 report.get("environment"), report.get("log_snippet")))
            return cur.lastrowid
        return self._write(tx)

    def update_incident(self, incident_id: int, **fields) -> None:
        allowed = {"assignee", "assignee_id", "severity", "status", "report", "escalation", "resolved_at", "alert"}
        fields = {k: json.dumps(v) if isinstance(v, dict) else v for k, v in fields.items() if k in allowed}
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._write(lambda c: c.execute(f"UPDATE incidents SET {sets} WHERE id=?", (*fields.values(), incident_id)))

    def incident_history(self, since: str) -> list[dict]:
        """Every incident opened since `since` (ISO time), archived ones too: the service health strip's history."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, title, service, severity, status, created_at, resolved_at FROM incidents "
                "WHERE created_at >= ? ORDER BY id", (since,))]

    def list_incidents(self) -> list[dict]:
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT i.id, i.title, i.service, i.severity, i.status, i.assignee, i.mode, i.scenario, i.attempts, i.model, i.created_at, "
                "i.resolved_at, i.source, u.name AS reported_by_name FROM incidents i LEFT JOIN users u ON u.id = i.reported_by "
                "WHERE i.archived_at IS NULL ORDER BY i.id DESC")]
            for r in rows:
                r["required_approvals"] = required_approvals(r["severity"], r["scenario"])
                last = c.execute("SELECT actor, type, payload FROM events WHERE incident_id=? AND type NOT IN ('llm_step') "
                                 "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
                r["last_event"] = {"actor": last["actor"], "type": last["type"], "payload": json.loads(last["payload"])} if last else None
                r["votes"] = c.execute("SELECT COUNT(*) FROM approvals WHERE incident_id=? AND patch_id=(SELECT MAX(id) FROM patches "
                                       "WHERE incident_id=?) AND decision='approved'", (r["id"], r["id"])).fetchone()[0]
        return rows

    def archive(self, statuses: tuple[str, ...], user: dict) -> list[int]:
        """Hide finished-with incidents from the board. Nothing is deleted: the incident and its audit trail stay."""
        marks = ",".join("?" * len(statuses))
        with self._conn() as c:
            ids = [r[0] for r in c.execute(f"SELECT id FROM incidents WHERE archived_at IS NULL AND status IN ({marks})", statuses)]
        stamp = now()
        for iid in ids:
            self._write(lambda c, iid=iid: c.execute("UPDATE incidents SET archived_at=? WHERE id=?", (stamp, iid)))
            self.record_event(iid, {"actor": "human", "type": "archived", "payload": {"by": user["name"]}}, user)
        return ids

    def get_incident(self, incident_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT i.*, u.name AS reported_by_name FROM incidents i LEFT JOIN users u ON u.id = i.reported_by "
                            "WHERE i.id=?", (incident_id,)).fetchone()
            if not row:
                return None
            inc = dict(row)
            inc["alert"] = json.loads(inc["alert"])
            claims = [dict(r) for r in c.execute("SELECT * FROM claims WHERE incident_id=? ORDER BY id", (incident_id,))]
            for cl in claims:
                ev = [dict(r) for r in c.execute("SELECT role, source, line, quote FROM evidence WHERE claim_id=? ORDER BY id", (cl["id"],))]
                cl["evidence"] = [e for e in ev if e["role"] == "claim"]
                cl["verdict_evidence"] = [e for e in ev if e["role"] == "verdict"]
            inc["claims"] = claims
            patches = [dict(r) for r in c.execute("SELECT * FROM patches WHERE incident_id=? ORDER BY id", (incident_id,))]
            for p in patches:
                p["sandbox"] = json.loads(p["sandbox"]) if p["sandbox"] else None
            inc["patches"] = patches
            inc["comments"] = [dict(r) for r in c.execute("SELECT * FROM comments WHERE incident_id=? ORDER BY id", (incident_id,))]
            sus = c.execute("SELECT payload FROM events WHERE incident_id=? AND type='suspects' ORDER BY id DESC LIMIT 1", (incident_id,)).fetchone()
            inc["suspects"] = json.loads(sus["payload"])["suspects"] if sus else []
            appr = c.execute("SELECT payload FROM events WHERE incident_id=? AND type='approval' ORDER BY id DESC LIMIT 1", (incident_id,)).fetchone()
            inc["approval"] = json.loads(appr["payload"]) if appr else None
            latest_patch = patches[-1]["id"] if patches else None
            inc["votes"] = [dict(r) for r in c.execute(
                "SELECT a.decision, a.reason, a.ts, a.role, u.id AS user_id, u.name FROM approvals a JOIN users u ON u.id = a.approver_id "
                "WHERE a.incident_id=? AND a.patch_id IS ? ORDER BY a.id", (incident_id, latest_patch))]
            inc["qa_checks"] = [dict(r) for r in c.execute(
                "SELECT q.result, q.note, q.ts, u.name FROM qa_checks q JOIN users u ON u.id = q.tester_id WHERE q.incident_id=? ORDER BY q.id",
                (incident_id,))]
            inc["required_approvals"] = required_approvals(inc["severity"], inc["scenario"])
            tokens = sum(json.loads(r[0]).get("tokens", 0) or 0 for r in
                         c.execute("SELECT payload FROM events WHERE incident_id=? AND type='llm_step'", (incident_id,)))
            inc["ai_usage"] = {"tokens": tokens, "wh_estimate": round(tokens / 1000 * WH_PER_1K_TOKENS, 1), "cost": policy.ai_cost(tokens),
                               "calls": c.execute("SELECT COUNT(*) FROM events WHERE incident_id=? AND type='llm_step'", (incident_id,)).fetchone()[0]}
            waiting = c.execute("SELECT ts FROM events WHERE incident_id=? AND type='awaiting_approval' ORDER BY id DESC LIMIT 1", (incident_id,)).fetchone()
            breach = c.execute("SELECT 1 FROM events WHERE incident_id=? AND type='sla_breach' AND ts >= ? LIMIT 1",
                               (incident_id, waiting[0] if waiting else "")).fetchone()
            inc["approval_waiting_since"] = waiting[0] if waiting else None
            inc["sla_breached"] = bool(breach)
        inc["impact"] = impact(inc)
        return inc

    def on_call_for(self, service: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT u.id, u.name, u.role FROM on_call o JOIN users u ON u.id = o.user_id WHERE o.service=?", (service,)).fetchone()
        return dict(r) if r else None

    def lead(self) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT id, name, role FROM users WHERE role='lead' ORDER BY id LIMIT 1").fetchone()
        return dict(r) if r else None

    def awaiting_approval(self) -> list[dict]:
        """Incidents waiting at the gate, with when they started waiting and whether the SLA was already breached."""
        with self._conn() as c:
            rows = c.execute("SELECT id, severity, assignee FROM incidents WHERE status='awaiting_approval'").fetchall()
            out = []
            for r in rows:
                w = c.execute("SELECT ts FROM events WHERE incident_id=? AND type='awaiting_approval' ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
                if not w:
                    continue
                b = c.execute("SELECT 1 FROM events WHERE incident_id=? AND type='sla_breach' AND ts >= ?", (r["id"], w[0])).fetchone()
                out.append({**dict(r), "since": w[0], "breached": bool(b)})
        return out

    def add_comment(self, incident_id: int, user: dict, text: str) -> dict:
        ts = now()
        cid = self._write(lambda c: c.execute("INSERT INTO comments (incident_id, author, text, ts, author_id) VALUES (?,?,?,?,?)",
                                              (incident_id, user["name"], text, ts, user["id"])).lastrowid)
        return {"id": cid, "incident_id": incident_id, "author": user["name"], "text": text, "ts": ts}

    def add_vote(self, incident_id: int, user: dict, decision: str, reason: str) -> None:
        def tx(c):
            pid = c.execute("SELECT MAX(id) FROM patches WHERE incident_id=?", (incident_id,)).fetchone()[0]
            c.execute("INSERT INTO approvals (incident_id, patch_id, approver_id, role, decision, reason, ts) VALUES (?,?,?,?,?,?,?)",
                      (incident_id, pid, user["id"], user["role"], decision, reason, now()))
        self._write(tx)

    def add_qa(self, incident_id: int, user: dict, result: str, note: str) -> None:
        self._write(lambda c: c.execute("INSERT INTO qa_checks (incident_id, tester_id, result, note, ts) VALUES (?,?,?,?,?)",
                                        (incident_id, user["id"], result, note, now())))

    def claim_count(self, incident_id: int) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM claims WHERE incident_id=?", (incident_id,)).fetchone()[0]

    def mark_interrupted(self) -> int:
        """Runs live in server threads; after a restart any unfinished run is escalated."""
        reason = "Server restarted while the incident was in progress."

        def tx(c):
            ids = [r[0] for r in c.execute(f"SELECT id FROM incidents WHERE status IN {RUNNING_STATUSES}")]
            for i in ids:
                c.execute("UPDATE incidents SET status='escalated', escalation=? WHERE id=?", (reason, i))
                self._insert_event(c, i, "orchestrator", "escalated", json.dumps({"reason": reason}), now())
            return len(ids)
        return self._write(tx)

    # ---- dashboard -------------------------------------------------------
    def metrics(self) -> dict:
        with self._conn() as c:
            q = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
            by_status = {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM incidents GROUP BY status")}
            durations = [(_ts(r[1]) - _ts(r[0])).total_seconds() for r in
                         c.execute("SELECT created_at, resolved_at FROM incidents WHERE resolved_at IS NOT NULL AND status IN ('resolved','closed','awaiting_qa')")]
            rejected = [json.loads(r[0]) for r in c.execute("SELECT payload FROM events WHERE type='rejected'")]
            # a wrong guess is caught when it was not confirmed and a different cause in the same incident was
            caught = q("SELECT COUNT(*) FROM claims c WHERE c.verdict IN ('refuted','insufficient') AND EXISTS "
                       "(SELECT 1 FROM claims s WHERE s.incident_id=c.incident_id AND s.verdict='supported')")
            tokens = sum(json.loads(r[0]).get("tokens", 0) or 0 for r in c.execute("SELECT payload FROM events WHERE type='llm_step'"))
            ids = [r[0] for r in c.execute("SELECT id FROM incidents")]
            patches = c.execute("SELECT COUNT(*), COALESCE(SUM(passed),0) FROM patches WHERE passed IS NOT NULL").fetchone()
            return {
                "total": sum(by_status.values()),
                "open": sum(by_status.get(s, 0) for s in OPEN_STATUSES),
                "awaiting_approval": by_status.get("awaiting_approval", 0),
                "awaiting_qa": by_status.get("awaiting_qa", 0),
                "resolved": by_status.get("resolved", 0) + by_status.get("closed", 0),
                "escalated": by_status.get("escalated", 0),
                "mttr_seconds": sum(durations) / len(durations) if durations else None,
                "claims_refuted": caught,
                "claims_total": q("SELECT COUNT(*) FROM claims"),
                "fabrications_blocked": sum(1 for p in rejected if any(h in e for e in p.get("errors", []) for h in FABRICATION_HINTS)),
                "sandbox_runs": patches[0], "sandbox_passed": patches[1],
                "tokens": tokens, "wh_estimate": round(tokens / 1000 * WH_PER_1K_TOKENS, 1),
                **self._money(ids),
            }

    def _money(self, ids: list[int]) -> dict:
        saved = hours = at_risk_now = people_cost = ai_cost = 0
        currency = "INR"
        for i in ids:
            inc = self.get_incident(i) or {}
            ai_cost += inc.get("ai_usage", {}).get("cost", 0)
            imp = inc.get("impact")
            if not imp or inc["source"] == "tester_report":  # a bug report has no measured failure rate to price
                continue
            currency = imp["currency"]
            saved += imp["saved_value"]
            hours += imp["engineer_hours_saved"]
            people_cost += imp["engineer_cost_saved"]
            # money at risk right now: only incidents still being worked on, like the "Open right now" count
            if inc["status"] in ("open", "investigating", "awaiting_approval") and not inc.get("archived_at"):
                at_risk_now += imp["at_risk_per_minute"]
        value = saved + people_cost
        return {"saved_value": saved, "engineer_hours_saved": round(hours, 1), "engineer_cost_saved": people_cost,
                "at_risk_per_minute_now": at_risk_now, "currency": currency, "ai_cost": round(ai_cost, 2),
                "net_value": round(value - ai_cost), "roi_multiple": round(value / ai_cost) if ai_cost >= 0.01 else None}

    def roi_ledger(self) -> dict:
        """Every incident (archived ones too) with what it cost in AI and what it saved, plus the inputs behind each
        figure, so the ROI page can recompute them when someone changes an assumption."""
        with self._conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM incidents ORDER BY id DESC")]
        rows, scenarios = [], {}
        for i in ids:
            inc = self.get_incident(i)
            imp = inc["impact"]
            if imp and inc["scenario"] not in scenarios:
                scenarios[inc["scenario"]] = assumptions(inc["scenario"])
            rows.append({"id": inc["id"], "title": inc["title"], "service": inc["service"], "status": inc["status"],
                         "mode": inc["mode"], "created_at": inc["created_at"], "scenario": inc["scenario"],
                         "tokens": inc["ai_usage"]["tokens"], "ai_cost": inc["ai_usage"]["cost"], "impact": imp,
                         "counted": bool(imp) and inc["source"] != "tester_report"})
        return {"rows": rows, "scenarios": scenarios, "totals": self._money(ids),
                "ai_price": {"per_1k_tokens": policy.cost_per_1k_tokens(), "source": policy.load()["path"]}}

    def recent_events(self, limit: int = 40) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT e.*, i.title FROM events e JOIN incidents i ON i.id = e.incident_id "
                             "WHERE e.type NOT IN ('llm_step', 'tool_call') ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def notifications(self, after_id: int = 0, limit: int = 20) -> list[dict]:
        """Messages Argus sent people (the 'notified' events), newest first."""
        with self._conn() as c:
            rows = c.execute("SELECT e.id, e.incident_id, e.ts, e.payload, i.title, i.severity FROM events e "
                             "JOIN incidents i ON i.id = e.incident_id WHERE e.type='notified' AND e.id>? ORDER BY e.id DESC LIMIT ?",
                             (after_id, limit)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    # ---- settings --------------------------------------------------------
    def get_setting(self, key: str, default=None):
        with self._conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, key: str, value) -> None:
        self._write(lambda c: c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                        (key, json.dumps(value))))

    # ---- events ----------------------------------------------------------
    def events_after(self, incident_id: int, after_id: int = 0, limit: int = 500) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM events WHERE incident_id=? AND id>? ORDER BY id LIMIT ?", (incident_id, after_id, limit)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def last_event(self, incident_id: int, type_: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM events WHERE incident_id=? AND type=? ORDER BY id DESC LIMIT 1", (incident_id, type_)).fetchone()
        return {**dict(r), "payload": json.loads(r["payload"])} if r else None

    def record_event(self, incident_id: int, event: dict, user: dict | None = None) -> int:
        t, p = event["type"], event["payload"]

        def tx(c):
            eid = self._insert_event(c, incident_id, event["actor"], t, json.dumps(p, default=str), event.get("ts") or now(),
                                     user["id"] if user else None, user["role"] if user else None)
            if t == "attempt" and p.get("stage") == "investigate":
                c.execute("UPDATE incidents SET status='investigating', attempts=? WHERE id=?", (p["n"], incident_id))
            elif t == "llm_step" and p.get("provider"):
                c.execute("UPDATE incidents SET model=? WHERE id=?", (f"{p['provider']}/{p['model']}", incident_id))
            elif t == "claim":
                attempts = c.execute("SELECT attempts FROM incidents WHERE id=?", (incident_id,)).fetchone()[0]
                cid = c.execute("INSERT INTO claims (incident_id, claim_key, category, statement, attempt) VALUES (?,?,?,?,?)",
                                (incident_id, p["id"], p["category"], p["statement"], attempts)).lastrowid
                self._evidence(c, cid, "claim", p["evidence"])
            elif t == "verdict":
                row = c.execute("SELECT id FROM claims WHERE incident_id=? AND claim_key=?", (incident_id, p["claim_id"])).fetchone()
                if row:
                    c.execute("UPDATE claims SET verdict=?, reasoning=? WHERE id=?", (p["verdict"], p["reasoning"], row[0]))
                    self._evidence(c, row[0], "verdict", p["evidence"])
            elif t == "patch":
                c.execute("INSERT INTO patches (incident_id, fix_type, rationale, rollback_to, diff) VALUES (?,?,?,?,?)",
                          (incident_id, p["fix_type"], p["rationale"], p.get("rollback_to"), p.get("diff", "")))
            elif t == "sandbox":
                c.execute("UPDATE patches SET sandbox=?, passed=? WHERE id=(SELECT MAX(id) FROM patches WHERE incident_id=?)",
                          (json.dumps(p), int(p["passed"]), incident_id))
            elif t == "awaiting_approval":
                c.execute("UPDATE incidents SET status='awaiting_approval' WHERE id=?", (incident_id,))
            elif t == "approval":
                source = c.execute("SELECT source FROM incidents WHERE id=?", (incident_id,)).fetchone()[0]
                status = ("awaiting_qa" if source == "tester_report" else "resolved") if p["approved"] else "rejected"
                c.execute("UPDATE incidents SET status=?, resolved_at=? WHERE id=?", (status, now(), incident_id))
            elif t == "escalated":
                c.execute("UPDATE incidents SET status='escalated', escalation=? WHERE id=?", (p["reason"], incident_id))
            elif t == "report" and p.get("markdown"):
                c.execute("UPDATE incidents SET report=? WHERE id=?", (p["markdown"], incident_id))
            return eid

        return self._write(tx)

    # ---- tamper-evident audit log ------------------------------------------
    # Each event's hash covers its own fields and the previous event's hash, so editing, deleting or reordering any
    # past event breaks every hash after it. verify_chain() recomputes the whole chain from the stored rows.
    @staticmethod
    def _insert_event(c, incident_id: int, actor: str, type_: str, payload: str, ts: str,
                      actor_id: int | None = None, actor_role: str | None = None) -> int:
        eid = c.execute("INSERT INTO events (incident_id, actor, type, payload, ts, actor_id, actor_role) VALUES (?,?,?,?,?,?,?)",
                        (incident_id, actor, type_, payload, ts, actor_id, actor_role)).lastrowid
        prev = c.execute("SELECT hash FROM events WHERE id<? ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
        prev_hash = prev[0] if prev else ""
        row = dict(zip(EVENT_COLS, (incident_id, actor, type_, payload, ts, actor_id, actor_role)), id=eid)
        c.execute("UPDATE events SET prev_hash=?, hash=? WHERE id=?", (prev_hash, event_hash(prev_hash, row), eid))
        return eid

    @staticmethod
    def _seal_unsealed(c) -> None:
        """Chain events written before the audit log was hashed (older databases), in their original order."""
        first = c.execute("SELECT MIN(id) FROM events WHERE hash IS NULL").fetchone()[0]
        if first is None:
            return
        prev = c.execute("SELECT hash FROM events WHERE id<? ORDER BY id DESC LIMIT 1", (first,)).fetchone()
        prev_hash = (prev[0] if prev else "") or ""
        for r in c.execute(f"SELECT id, {', '.join(EVENT_COLS)} FROM events WHERE id>=? ORDER BY id", (first,)).fetchall():
            h = event_hash(prev_hash, dict(r))
            c.execute("UPDATE events SET prev_hash=?, hash=? WHERE id=?", (prev_hash, h, r["id"]))
            prev_hash = h

    def verify_chain(self, incident_id: int | None = None) -> dict:
        """Recompute every hash. ok=False names the first event that no longer matches what was written."""
        checked, prev_hash, bad, head, mine = 0, "", None, "", 0
        with self._conn() as c:
            for r in c.execute(f"SELECT id, prev_hash, hash, {', '.join(EVENT_COLS)} FROM events ORDER BY id"):
                checked += 1
                mine += r["incident_id"] == incident_id
                if bad is None:
                    if (r["prev_hash"] or "") != prev_hash:
                        bad = {"id": r["id"], "incident_id": r["incident_id"], "problem": "an earlier event was removed or changed"}
                    elif r["hash"] != event_hash(prev_hash, dict(r)):
                        bad = {"id": r["id"], "incident_id": r["incident_id"], "problem": "this event was changed after it was written"}
                prev_hash = r["hash"] or ""
                head = prev_hash
        out = {"ok": bad is None, "events": checked, "head": head[:16], "first_bad": bad, "checked_at": now()}
        if incident_id is not None:
            out["incident_events"] = mine
        return out

    @staticmethod
    def _evidence(c, claim_id: int, role: str, evidence: list[dict]) -> None:
        c.executemany("INSERT INTO evidence (claim_id, role, source, line, quote) VALUES (?,?,?,?,?)",
                      [(claim_id, role, e["source"], e["line"], e["quote"]) for e in evidence])


def event_hash(prev_hash: str, row: dict) -> str:
    body = json.dumps([prev_hash or "", row["id"], *(row[k] for k in EVENT_COLS)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def required_approvals(severity: str, scenario: str | None = None) -> dict:
    """Who must approve a fix at this severity: the service's own rule (argus.yaml `severity.approvals`) if it has one,
    else policy.yaml (with the rule and the policy's fingerprint)."""
    return service_approvals(severity, service_config(scenario)) or policy.approval_rule(severity)


def service_config(scenario: str | None) -> ServiceConfig | None:
    path = SERVICES_DIR / (scenario or "") / "argus.yaml"
    return _config_at(scenario, path.stat().st_mtime) if scenario and path.is_file() else None


@lru_cache(maxsize=32)
def _config_at(scenario: str, _mtime: float) -> ServiceConfig | None:
    try:
        return load(SERVICES_DIR / scenario)
    except (ConfigError, OSError):
        return None


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)
