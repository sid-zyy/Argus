"""The standard incident input: the one shape every alert adapter produces and the pipeline reads.

Whatever the monitoring tool (Alertmanager, Grafana, a hand-written JSON), its payload is turned into an
IncidentInput before Argus sees it, so the agents never depend on a particular tool's format.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["sev1", "sev2", "sev3", "sev4"]

# How common severity labels map to Argus severities. A service's argus.yaml can add to or override this.
SEVERITY_ALIASES = {
    "critical": "sev1", "page": "sev1", "p1": "sev1", "sev1": "sev1", "fatal": "sev1",
    "high": "sev2", "error": "sev2", "major": "sev2", "p2": "sev2", "sev2": "sev2",
    "warning": "sev3", "warn": "sev3", "medium": "sev3", "minor": "sev3", "p3": "sev3", "sev3": "sev3",
    "info": "sev4", "low": "sev4", "p4": "sev4", "sev4": "sev4",
}


def severity_of(label, extra: dict | None = None, default: Severity = "sev3") -> Severity:
    key = str(label or "").strip().lower()
    return (extra or {}).get(key) or SEVERITY_ALIASES.get(key) or default


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class IncidentInput(BaseModel):
    """A firing alert, normalised. Fields the pipeline, the business figures and the UI rely on are typed; anything
    else a tool sends is kept in `labels` (for routing) or `description`."""
    model_config = ConfigDict(extra="allow")

    id: str = Field(description="The alert's id in the monitoring tool, e.g. 'AM-CheckoutHighErrorRate-a1b2c3'")
    service: str = Field(description="The service that is failing; routes the alert to a connected service")
    title: str = Field(description="One-line summary, e.g. 'checkout-api 5xx rate 38%'")
    severity: Severity = "sev3"
    metric: Optional[str] = Field(default=None, description="The metric that crossed its threshold, e.g. 'error_rate_5xx'")
    value: Optional[float] = Field(default=None, description="Its value when the alert fired (a share is 0..1)")
    threshold: Optional[float] = None
    window: Optional[str] = Field(default=None, description="How long it stayed over the threshold, e.g. '2m'")
    started_at: Optional[str] = Field(default=None, description="ISO-8601 UTC time the problem started")
    fired_at: Optional[str] = Field(default=None, description="ISO-8601 UTC time the alert fired")
    source: str = Field(default="unknown", description="Which monitoring tool sent it")
    description: Optional[str] = None
    labels: dict[str, str] = Field(default_factory=dict, description="The tool's labels/tags, used for routing")

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, v):
        return SEVERITY_ALIASES.get(str(v).strip().lower(), v) if v is not None else "sev3"

    @field_validator("value", "threshold", mode="before")
    @classmethod
    def _number(cls, v):
        if v in (None, ""):
            return None
        if isinstance(v, str):
            v = v.strip().rstrip("%")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @field_validator("labels", mode="before")
    @classmethod
    def _labels(cls, v):
        return {str(k): str(x) for k, x in (v or {}).items()}

    def as_alert(self) -> dict:
        """The dict the pipeline and the store keep (ws.alert). Empty labels and description are left out."""
        out = self.model_dump()
        for key in ("labels", "description"):
            if not out.get(key):
                out.pop(key, None)
        return out


def json_schema() -> str:
    return json.dumps(IncidentInput.model_json_schema(), indent=2)
