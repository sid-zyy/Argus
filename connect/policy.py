"""A service's severity and approval policy, from the `severity` section of its argus.yaml."""
from __future__ import annotations

import hashlib

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


def service_approvals(severity: str, cfg: ServiceConfig | None) -> dict | None:
    """The service's own approval rule for this severity (argus.yaml `severity.approvals`), or None if it has none.
    Like policy.yaml's rules, it names the rule and the file's fingerprint for the audit log."""
    if not cfg:
        return None
    key = severity if severity in cfg.severity.approvals else "default" if "default" in cfg.severity.approvals else None
    if not key:
        return None
    rule = cfg.severity.approvals[key]
    if rule.count > 1:
        label = f"{rule.count} people" + (", one a team lead" if rule.lead else "")
    else:
        label = "A team lead" if rule.lead else "One developer or lead"
    sha = hashlib.sha256(cfg.file.read_bytes()).hexdigest()[:12] if cfg.file else "built-in"
    return {"count": rule.count, "lead": rule.lead, "label": label, "policy": cfg.key,
            "rule": f"severity.approvals.{key}", "policy_sha": sha, "policy_file": f"{cfg.key}/argus.yaml"}


def required_approvals(severity: str, cfg: ServiceConfig | None = None) -> dict:
    """Who must approve a fix: the service's own rule wins; otherwise the governance policy (policy.yaml)."""
    from orchestrator import policy
    return service_approvals(severity, cfg) or policy.approval_rule(severity)
