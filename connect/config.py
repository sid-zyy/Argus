"""Per-service configuration: argus.yaml in a service folder says where the code, alerts and logs come from, how to
reproduce and test a failure, what the service depends on, and its severity and approval policy.

A folder without argus.yaml (the original recorded scenarios) gets the built-in layout: alert.json, logs/*.log,
repo/, repro.py and repo/tests.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from connect.schema import Severity

ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = ROOT / "scenarios"
CONFIG_NAME = "argus.yaml"

LogRole = Literal["service", "metrics", "dependency", "deploy", "other"]
DEFAULT_METRICS_DESCRIPTION = ("Service metrics per 30s bucket (rps, error_rate_5xx, p95_ms, pool_in_use, mem_mb, restarts). "
                               "Database health is in db.log; use search_logs for it.")


class ConfigError(ValueError):
    """argus.yaml is missing required fields or has invalid values."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceInfo(_Strict):
    name: str = Field(description="The service name alerts carry, e.g. 'checkout-api'")
    label: Optional[str] = Field(default=None, description="A friendly name for people, e.g. 'ShopFast checkout'")
    team: Optional[str] = None
    language: Optional[str] = None
    on_call: Optional[str] = Field(default=None, description="Who gets new incidents (an Argus user's name)")
    description: Optional[str] = None


class RepoConfig(_Strict):
    adapter: Literal["git"] = "git"
    path: Optional[str] = Field(default=None, description="A local checkout, relative to this file")
    url: Optional[str] = Field(default=None, description="Or a remote to clone into runs/repos/<service>")
    branch: Optional[str] = None


class AlertConfig(_Strict):
    adapter: Literal["alertmanager", "grafana", "standard"] = "standard"
    match: dict[str, str] = Field(default_factory=dict, description="Labels an alert must carry to be routed here; "
                                                                    "defaults to {service: <service.name>}")
    base: Optional[str] = Field(default=None, description="Standard-format alert whose fields fill in what a payload leaves out")
    sample: Optional[str] = Field(default=None, description="An example payload from the monitoring tool, for checks and demos")
    severity_map: dict[str, Severity] = Field(default_factory=dict, description="The tool's severity labels -> sev1..sev4")


class LogConfig(_Strict):
    name: str = Field(description="What the agents call this source and cite as evidence, e.g. 'app.log'")
    adapter: Literal["text", "jsonl", "csv", "command"] = "text"
    path: Optional[str] = None
    run: Optional[str] = Field(default=None, description="command adapter: a command whose output is the log")
    role: LogRole = "service"
    dependency: Optional[str] = Field(default=None, description="For role 'dependency': which system writes it")
    fields: dict[str, str] = Field(default_factory=dict, description="jsonl/csv: which keys hold time, level and message")
    time_format: Literal["iso", "epoch_ms", "epoch_s"] = "iso"
    level_names: dict[str, str] = Field(default_factory=dict, description="e.g. {'30': 'INFO', '50': 'ERROR'} for pino")
    drop: list[str] = Field(default_factory=list, description="jsonl/csv: keys to leave out of the rendered line")
    timeout: int = 20


class MetricsConfig(_Strict):
    source: Optional[str] = Field(default=None, description="Which log source holds the metrics")
    description: Optional[str] = Field(default=None, description="What the metric fields mean, shown to the agents")


class DependencyConfig(_Strict):
    name: str
    kind: Literal["external", "data", "infra", "service", "queue", "cache"] = "service"
    logs: list[str] = Field(default_factory=list, description="Log sources that show this dependency's own health")
    note: Optional[str] = None


class CommandsConfig(_Strict):
    repro: str = Field(description="Exits 0 when healthy and non-zero while the failure reproduces. {repo} is the clone")
    test: Optional[str] = Field(default=None, description="The repo's test suite, run after the fix")
    test_if_exists: Optional[str] = Field(default=None, description="Only run tests when this path exists in the sandbox")
    files: list[str] = Field(default_factory=list, description="Files copied next to the clone, e.g. the repro script")
    image: Optional[str] = Field(default=None, description="Docker image for sandbox runs (default: the Argus sandbox image)")
    timeout: int = 60


class SeverityRule(_Strict):
    metric: str
    above: Optional[float] = None
    below: Optional[float] = None
    severity: Severity


class ApprovalRule(_Strict):
    count: int = Field(default=1, ge=1, le=3)
    lead: bool = False


class SeverityConfig(_Strict):
    default: Severity = "sev3"
    rules: list[SeverityRule] = Field(default_factory=list, description="First match wins; overrides the alert's own severity")
    approvals: dict[str, ApprovalRule] = Field(default_factory=dict, description="sev1..sev4 or 'default' -> who must approve")


class DeployConfig(_Strict):
    on_approve: bool = Field(default=False, description="Commit the approved fix to the repo (its deploy pipeline ships it)")
    app: Optional[str] = None
    url: Optional[str] = None


class ServiceConfig(_Strict):
    version: int = 1
    service: ServiceInfo
    repo: RepoConfig
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    logs: list[LogConfig] = Field(default_factory=list)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    dependencies: list[DependencyConfig] = Field(default_factory=list)
    commands: CommandsConfig
    severity: SeverityConfig = Field(default_factory=SeverityConfig)
    deploy: DeployConfig = Field(default_factory=DeployConfig)
    business: Optional[str] = "business.json"

    _base_dir: Path = PrivateAttr(default=ROOT)
    _file: Optional[Path] = PrivateAttr(default=None)

    # ---- paths -------------------------------------------------------------------
    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def file(self) -> Optional[Path]:
        """The argus.yaml this came from; None for a folder using the built-in layout."""
        return self._file

    @property
    def key(self) -> str:
        """The folder name: how the dashboard, the API and the CLI refer to this service."""
        return self._base_dir.name

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return (p if p.is_absolute() else self._base_dir / p).resolve()

    def repo_path(self) -> Path:
        if self.repo.path:
            return self.resolve(self.repo.path)
        return ROOT / "runs" / "repos" / self.key

    # ---- what the agents and checks need -------------------------------------------
    def sources(self, role: str) -> list[str]:
        return [l.name for l in self.logs if l.role == role]

    @property
    def symptom_sources(self) -> tuple[str, ...]:
        """This service's own logs and metrics: they show the symptom, never a dependency's health or the cause."""
        return tuple(l.name for l in self.logs if l.role in ("service", "metrics"))

    @property
    def change_sources(self) -> tuple[str, ...]:
        return tuple(self.sources("deploy"))

    @property
    def metrics_source(self) -> Optional[str]:
        return self.metrics.source or next(iter(self.sources("metrics")), None)

    def alert_match(self) -> dict[str, str]:
        return self.alerts.match or {"service": self.service.name}

    def base_alert(self) -> dict:
        if not self.alerts.base:
            return {}
        return json.loads(self.resolve(self.alerts.base).read_text(encoding="utf-8"))

    def sample_payload(self) -> Optional[dict]:
        path = self.alerts.sample or self.alerts.base
        return json.loads(self.resolve(path).read_text(encoding="utf-8")) if path else None

    def summary(self) -> dict:
        """A short description for the dashboard and the wizard."""
        return {"key": self.key, "service": self.service.name, "label": self.service.label or self.service.name,
                "team": self.service.team, "language": self.service.language, "on_call": self.service.on_call,
                "configured": self.file is not None, "repo": self.repo.adapter, "alerts": self.alerts.adapter,
                "logs": [{"name": l.name, "adapter": l.adapter, "role": l.role} for l in self.logs],
                "dependencies": [d.name for d in self.dependencies]}


def _with_paths(cfg: ServiceConfig, base: Path, file: Optional[Path]) -> ServiceConfig:
    cfg._base_dir = base.resolve()
    cfg._file = file
    return cfg


def parse(data: dict, base: Path, file: Optional[Path] = None) -> ServiceConfig:
    try:
        return _with_paths(ServiceConfig.model_validate(data or {}), base, file)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or '(top level)'}: {err['msg']}" for err in e.errors()[:10])
        raise ConfigError(f"{file or base}: {problems}") from None


def load(folder: str | Path) -> ServiceConfig:
    """The service config for a folder: its argus.yaml, or the built-in layout if there is none."""
    folder = Path(folder).resolve()
    file = folder / CONFIG_NAME
    if file.is_file():
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ConfigError(f"{file}: not valid YAML: {e}") from None
        if not isinstance(data, dict):
            raise ConfigError(f"{file}: expected a mapping at the top level")
        return parse(data, folder, file)
    return builtin_layout(folder)


ROLE_BY_NAME = {"app.log": "service", "metrics.log": "metrics", "deploy.log": "deploy"}


def builtin_layout(folder: Path) -> ServiceConfig:
    """The layout the recorded scenarios were written for, expressed as a config."""
    alert = json.loads((folder / "alert.json").read_text(encoding="utf-8")) if (folder / "alert.json").is_file() else {}
    logs = [{"name": p.name, "path": f"logs/{p.name}", "role": ROLE_BY_NAME.get(p.name, "dependency")}
            for p in sorted((folder / "logs").glob("*.log"))]
    live = folder / "live.json"
    deploy = json.loads(live.read_text(encoding="utf-8")) if live.is_file() else {}
    data = {
        "service": {"name": alert.get("service", folder.name)},
        "repo": {"path": "repo"},
        "alerts": {"adapter": "standard", "base": "alert.json" if alert else None},
        "logs": logs,
        "metrics": {"source": "metrics.log" if (folder / "logs" / "metrics.log").exists() else None,
                    "description": DEFAULT_METRICS_DESCRIPTION},
        "commands": {"repro": "python repro.py {repo}", "files": ["repro.py"],
                     "test": "python -m pytest -q -p no:cacheprovider {repo}/tests", "test_if_exists": "{repo}/tests"},
        "deploy": {"on_approve": bool(deploy.get("deploy_on_approve")), "app": deploy.get("app"), "url": deploy.get("url")},
    }
    return parse(data, folder)


def service_folders(root: Path = SERVICES_DIR) -> list[Path]:
    """Folders Argus can run incidents for: a connected service (argus.yaml) or a recorded scenario (history.py)."""
    return sorted(d for d in root.iterdir() if d.is_dir() and ((d / CONFIG_NAME).is_file() or (d / "history.py").is_file()))


def find(name: str, root: Path = SERVICES_DIR) -> Path:
    """A service folder by its key, or by a path to the folder or to its argus.yaml."""
    p = Path(name)
    if p.name == CONFIG_NAME:
        p = p.parent
    if p.is_dir() and ((p / CONFIG_NAME).is_file() or (p / "history.py").is_file()):
        return p.resolve()
    d = root / name
    if d in service_folders(root):
        return d
    raise ConfigError(f"no service folder named {name!r} (looked in {root})")
