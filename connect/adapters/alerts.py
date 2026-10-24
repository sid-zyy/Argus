"""Alert adapters: turn a monitoring tool's webhook into IncidentInputs, and route each one to a connected service.

- alertmanager: Prometheus Alertmanager webhook (version 4)
- grafana:      Grafana Alerting (unified alerting) webhook contact point
- standard:     an IncidentInput as JSON, e.g. from a script or another tool
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from pydantic import ValidationError

from connect.config import ServiceConfig
from connect.schema import IncidentInput, severity_of, utc_now


class RouteError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _measurements(notes: dict) -> dict:
    """Optional measurements a rule can put in its annotations."""
    return {k: notes[k] for k in ("metric", "value", "threshold", "window") if k in notes}


class Alertmanager:
    name = "alertmanager"
    label = "Prometheus Alertmanager"

    @staticmethod
    def detect(payload: dict) -> bool:
        return isinstance(payload.get("alerts"), list) and not Grafana.detect(payload)

    def parse(self, payload: dict) -> list[IncidentInput]:
        out = []
        for a in payload.get("alerts", []):
            if a.get("status", "firing") != "firing":
                continue
            labels, notes = a.get("labels", {}), a.get("annotations", {})
            name = labels.get("alertname", "alert")
            out.append(IncidentInput(
                id=f"AM-{name}-{a.get('fingerprint', '')[:6]}".rstrip("-"),
                service=labels.get("service") or labels.get("job") or "unknown",
                title=notes.get("summary") or name,
                severity=severity_of(labels.get("severity"), default="sev2"),
                started_at=a.get("startsAt"), fired_at=utc_now(), source=self.label,
                description=notes.get("description"), labels=labels, **_measurements(notes)))
        return out


class Grafana:
    name = "grafana"
    label = "Grafana Alerting"

    @staticmethod
    def detect(payload: dict) -> bool:
        alerts = payload.get("alerts")
        if not isinstance(alerts, list):
            return False
        return "orgId" in payload or any(isinstance(a, dict) and ("values" in a or "valueString" in a or "dashboardURL" in a)
                                         for a in alerts)

    def parse(self, payload: dict) -> list[IncidentInput]:
        out = []
        for a in payload.get("alerts", []):
            if a.get("status", "firing") != "firing":
                continue
            labels, notes = a.get("labels", {}), a.get("annotations", {})
            name = labels.get("alertname", "alert")
            m = _measurements(notes)
            values = a.get("values") or {}
            if "value" not in m and values:  # the first refId is the query; later ones are reduce/threshold steps
                m["value"] = values.get(notes.get("value_ref", ""), next(iter(values.values())))
            out.append(IncidentInput(
                id=f"GF-{name}-{a.get('fingerprint', '')[:6]}".rstrip("-"),
                service=labels.get("service") or labels.get("app") or "unknown",
                title=notes.get("summary") or payload.get("title") or name,
                severity=severity_of(labels.get("severity"), default="sev3"),
                started_at=a.get("startsAt"), fired_at=notes.get("fired_at") or utc_now(), source=self.label,
                description=notes.get("description"), labels={**labels, **({"dashboard": a["dashboardURL"]} if a.get("dashboardURL") else {})},
                **m))
        return out


class Standard:
    name = "standard"
    label = "Argus standard format"

    @staticmethod
    def detect(payload: dict) -> bool:
        return "alerts" not in payload and "service" in payload and "title" in payload

    def parse(self, payload: dict) -> list[IncidentInput]:
        payload = {"id": f"STD-{hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:6]}", **payload}
        try:
            return [IncidentInput.model_validate(payload)]
        except ValidationError as e:
            raise RouteError(f"not a valid incident: {e.errors()[0]['loc']}: {e.errors()[0]['msg']}") from None


ADAPTERS = {"grafana": Grafana(), "alertmanager": Alertmanager(), "standard": Standard()}  # detection order matters


def detect(payload: dict) -> str | None:
    return next((name for name, a in ADAPTERS.items() if a.detect(payload)), None)


def parse(payload: dict, adapter: str | None = None) -> tuple[str, list[IncidentInput]]:
    name = adapter or detect(payload)
    if not name:
        raise RouteError("unrecognised alert format: expected Alertmanager, Grafana, or the standard incident format")
    return name, ADAPTERS[name].parse(payload)


def matches(cfg: ServiceConfig, inc: IncidentInput) -> bool:
    labels = {**inc.labels, "service": inc.service}
    return all(labels.get(k) == v for k, v in cfg.alert_match().items())


def route(payload: dict, services: Iterable[ServiceConfig]) -> tuple[ServiceConfig, IncidentInput, str]:
    """The first firing alert in the payload and the connected service it belongs to.

    A `scenario` label names the folder directly. Otherwise the service whose `alerts.match` labels the alert carries
    wins, preferring one configured for the tool that sent it."""
    services = list(services)
    fmt, incidents = parse(payload)
    if not incidents:
        raise RouteError("no firing alerts in the payload")
    inc = incidents[0]
    by_key = {s.key: s for s in services}
    if inc.labels.get("scenario") in by_key:
        return by_key[inc.labels["scenario"]], inc, fmt
    candidates = [s for s in services if matches(s, inc)]
    if not candidates:
        raise RouteError(f"no connected service for service {inc.service!r}", 404)
    best = next((s for s in candidates if s.alerts.adapter == fmt), candidates[0])
    return best, inc, fmt
