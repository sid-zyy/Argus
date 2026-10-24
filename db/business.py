"""Business impact of an incident, from each scenario's business.json assumptions.

Every figure is an estimate and the assumptions travel with it, so the UI and report can show them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios"
DONE = ("resolved", "closed", "awaiting_qa")


@lru_cache(maxsize=32)
def assumptions(scenario: str) -> dict | None:
    p = SCENARIOS / scenario / "business.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _t(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def impact(inc: dict, now: datetime | None = None) -> dict | None:
    b = assumptions(inc["scenario"])
    if not b:
        return None
    now = now or datetime.now(timezone.utc)
    alert = inc["alert"]
    share = alert.get("value") if b["failure_share"] == "alert" else b["failure_share"]
    share = float(share or 0)
    per_min = b["units_per_minute"] * share * b["value_per_unit"]
    started, fired = _t(alert.get("started_at")), _t(alert.get("fired_at"))
    detection = max(0.0, (fired - started).total_seconds() / 60) if started and fired else 0.0
    opened, closed = _t(inc["created_at"]), _t(inc.get("resolved_at"))
    fixed = inc["status"] in DONE
    handling = ((closed if fixed and closed else now) - opened).total_seconds() / 60
    argus_minutes = detection + handling
    manual_minutes = detection + b["manual_mttr_minutes"]
    saved_minutes = max(0.0, manual_minutes - argus_minutes) if fixed else 0.0
    hours_saved = round(b["manual_mttr_minutes"] * b["engineers_paged"] / 60, 1) if fixed else 0.0
    return {
        "service_label": b["service_label"],
        "unit": b["unit"],
        "currency": b["currency"],
        "failure_share": share,
        "affected_per_minute": round(b["units_per_minute"] * share, 1),
        "at_risk_per_minute": round(per_min),
        "minutes_so_far": round(argus_minutes, 1),
        "at_risk_so_far": round(per_min * argus_minutes),
        "fixed": fixed,
        "manual_minutes": round(manual_minutes, 1),
        "saved_minutes": round(saved_minutes, 1),
        "saved_value": round(per_min * saved_minutes),
        "engineer_hours_saved": hours_saved,
        "engineer_cost_saved": round(hours_saved * b.get("engineer_cost_per_hour", 0)),
        "detection_minutes": round(detection, 2),
        "assumptions": b["assumptions"],
    }
