"""Turning an incoming alert into the incident Argus opens for a connected service."""
from __future__ import annotations

from connect.config import ServiceConfig
from connect.policy import classify
from connect.schema import IncidentInput


def alert_for(cfg: ServiceConfig, incoming: IncidentInput | dict | None = None, apply_policy: bool = True) -> dict:
    """The service's base alert, overlaid with what the monitoring tool sent, with the severity policy applied."""
    alert = {"service": cfg.service.name, **cfg.base_alert()}
    if isinstance(incoming, IncidentInput):
        incoming = incoming.as_alert()
    alert.update({k: v for k, v in (incoming or {}).items() if v is not None})
    alert = IncidentInput.model_validate(alert).as_alert()
    if apply_policy:
        alert["severity"] = classify(cfg, alert)
    return alert
