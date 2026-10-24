"""A service's severity and approval policy, from the `severity` section of its argus.yaml."""
from __future__ import annotations

import os

from connect.config import ServiceConfig
from connect.schema import severity_of


def classify(cfg: ServiceConfig, alert: dict) -> str:
    """The incident's severity: the first matching rule, else the tool's label mapped by the service, else what the
    adapter decided, else the service default."""
    value, metric = alert.get("value"), alert.get("metric")
    for rule in cfg.severity.rules:
        if rule.metric != metric or value is None:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if (rule.above is None or v >= rule.above) and (rule.below is None or v <= rule.below):
            return rule.severity
    raw = (alert.get("labels") or {}).get("severity")
    if raw and raw.lower() in cfg.alerts.severity_map:
        return severity_of(raw, cfg.alerts.severity_map)
    return alert.get("severity") or cfg.severity.default


def required_approvals(severity: str, cfg: ServiceConfig | None = None) -> dict:
    """Who must approve a fix. A service's own policy wins; otherwise one developer or lead, or with
    TWO_PERSON_APPROVAL=1, two people including a lead for sev 1/2."""
    rule = cfg and (cfg.severity.approvals.get(severity) or cfg.severity.approvals.get("default"))
    if rule:
        if rule.count > 1:
            label = f"{rule.count} people" + (", one a team lead" if rule.lead else "")
        else:
            label = "A team lead" if rule.lead else "One developer or lead"
        return {"count": rule.count, "lead": rule.lead, "label": label, "policy": cfg.key}
    if severity in ("sev1", "sev2") and os.getenv("TWO_PERSON_APPROVAL", "0").lower() in ("1", "true", "yes"):
        return {"count": 2, "lead": True, "label": "Developer + Lead (two people)"}
    return {"count": 1, "lead": False, "label": "One developer or lead"}
