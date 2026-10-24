"""Checks a connected service's configuration before Argus runs an incident on it.

quick: static checks, fast enough to run before every incident (config, repo, alert formats, logs, commands on PATH).
full:  also clones the repo into a sandbox and runs the repro and test commands once, the way a fix will be tested.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import ValidationError

from connect.adapters import alerts as alert_adapters
from connect.adapters.logs import logs_for
from connect.adapters.repos import repo_for
from connect.config import ROOT, ConfigError, ServiceConfig, load, service_folders
from connect.intake import alert_for
from connect.schema import IncidentInput

ISO_START = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
RESERVED = ("report",)


@dataclass
class Check:
    status: str  # ok | info | warn | error
    area: str
    message: str


@dataclass
class Report:
    service: str
    file: str | None
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.status == "error"]

    def as_dict(self) -> dict:
        return {"service": self.service, "file": self.file, "ok": self.ok, "errors": len(self.errors),
                "warnings": sum(c.status == "warn" for c in self.checks), "checks": [asdict(c) for c in self.checks]}


def _exe(argv0: str) -> str | None:
    import sys
    return sys.executable if argv0 == "python" else shutil.which(argv0)


def check_folder(folder: str | Path, full: bool = False, users: list[str] | None = None) -> Report:
    folder = Path(folder)
    try:
        cfg = load(folder)
    except (ConfigError, OSError, ValueError) as e:
        return Report(folder.name, str(folder / "argus.yaml"), [Check("error", "config", str(e))])
    return check(cfg, full, users)


def check(cfg: ServiceConfig, full: bool = False, users: list[str] | None = None) -> Report:
    out: list[Check] = []
    add = lambda status, area, message: out.append(Check(status, area, message))  # noqa: E731

    # ---- config ----------------------------------------------------------------
    if cfg.file:
        add("ok", "config", f"{cfg.file.name} is valid (service {cfg.service.name})")
    else:
        add("info", "config", "no argus.yaml: using the built-in scenario layout")
    if users is not None and cfg.service.on_call and cfg.service.on_call not in users:
        add("warn", "config", f"on_call {cfg.service.on_call!r} is not an Argus user; new incidents will be unassigned")

    # ---- repo ----------------------------------------------------------------
    repo = repo_for(cfg)
    if not repo.exists() and cfg.repo.url:
        if full:
            try:
                repo.ensure()
                add("ok", "repo", f"cloned {cfg.repo.url}")
            except ValueError as e:
                add("error", "repo", str(e))
        else:
            add("info", "repo", f"{cfg.repo.url} will be cloned on first use")
    if repo.exists():
        try:
            head = repo.git("log", "-1", "--format=%h %s").strip()
            count = repo.git("rev-list", "--count", "HEAD").strip()
            add("ok", "repo", f"git repo at {repo.path} ({count} commits, HEAD {head})")
            if int(count) < 2:
                add("warn", "repo", "only one commit: the change finder has no history to compare against")
        except ValueError as e:
            add("error", "repo", f"{repo.path} has no commits: {e}")
    elif not cfg.repo.url:
        hint = " (build it with python -m scenarios.build " + cfg.key + ")" if (cfg.base_dir / "history.py").is_file() else ""
        add("error", "repo", f"no git repo at {repo.path}{hint}")

    # ---- alerts ----------------------------------------------------------------
    try:
        base = cfg.base_alert()
        if base:
            IncidentInput.model_validate({"service": cfg.service.name, **base})
            add("ok", "alerts", f"base alert {cfg.alerts.base} matches the standard incident schema")
            if base.get("service", cfg.service.name) != cfg.service.name:
                add("warn", "alerts", f"base alert is for {base['service']!r}, not {cfg.service.name!r}")
    except (OSError, ValueError, ValidationError) as e:
        add("error", "alerts", f"base alert {cfg.alerts.base}: {str(e).splitlines()[0]}")
    try:
        sample = cfg.sample_payload() if cfg.alerts.sample else None
    except (OSError, ValueError) as e:
        sample = None
        add("error", "alerts", f"sample payload {cfg.alerts.sample}: {e}")
    if sample is not None:
        fmt = alert_adapters.detect(sample)
        if fmt != cfg.alerts.adapter:
            add("error", "alerts", f"sample payload looks like {fmt or 'an unknown format'}, but the adapter is {cfg.alerts.adapter}")
        else:
            try:
                others = [load(d) for d in service_folders()] if cfg.base_dir.parent == ROOT / "scenarios" else []
                others = [o for o in others if o.key != cfg.key] + [cfg]
                target, inc, _ = alert_adapters.route(sample, others)
                sev = alert_for(cfg, inc)["severity"]
                add("ok", "alerts", f"{alert_adapters.ADAPTERS[fmt].label} sample parses: {inc.title!r} "
                                    f"({inc.severity}{f', {sev} by this service policy' if sev != inc.severity else ''})")
                if target.key != cfg.key:
                    add("warn", "alerts", f"that alert would be routed to {target.key}, not here; add a distinguishing label to alerts.match")
            except alert_adapters.RouteError as e:
                add("error", "alerts", f"sample payload: {e}")
    elif not cfg.alerts.sample:
        add("info", "alerts", f"no sample payload; {cfg.alerts.adapter} alerts can't be test-fired from the dashboard")

    # ---- logs ----------------------------------------------------------------
    names = [l.name for l in cfg.logs]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        add("error", "logs", f"two log sources are named {dup!r}")
    for n in names:
        if n in RESERVED or n.startswith(("repo/", "git:")):
            add("error", "logs", f"{n!r} is reserved; rename that source")
    for name, src in logs_for(cfg).items():
        if src.cfg.adapter == "command" and not full:
            add("info", "logs", f"{name}: command {src.cfg.run!r} runs at the start of each incident")
            continue
        try:
            lines = src.lines()
        except ValueError as e:
            add("error", "logs", f"{name}: {e}")
            continue
        if not lines:
            add("warn", "logs", f"{name} ({src.cfg.adapter}) is empty")
            continue
        stamped = sum(bool(ISO_START.match(l)) for l in lines)
        detail = f"{len(lines)} lines, {lines[0][:19]} .. {lines[-1][:19]}"
        if stamped < 0.9 * len(lines):
            add("warn", "logs", f"{name}: only {stamped}/{len(lines)} lines start with a time, so time-window searches miss the rest"
                                + ("; set fields.time" if src.cfg.adapter in ("jsonl", "csv") else ""))
        elif src.bad_lines:
            add("warn", "logs", f"{name}: {src.bad_lines} lines could not be parsed as {src.cfg.adapter} and are kept as raw text")
        else:
            add("ok", "logs", f"{name} ({src.cfg.adapter}, {src.role}): {detail}")
    if not cfg.sources("service"):
        add("warn", "logs", "no source has role 'service'; the investigator has none of the service's own logs")
    if not cfg.sources("deploy"):
        add("warn", "logs", "no source has role 'deploy'; the change finder can only use git history")
    if cfg.metrics.source and cfg.metrics.source not in names:
        add("error", "metrics", f"metrics.source {cfg.metrics.source!r} is not one of the log sources")
    elif not cfg.metrics_source:
        add("info", "metrics", "no metrics source; the get_metrics tool will say so and agents use the logs")
    for dep in cfg.dependencies:
        for n in dep.logs:
            if n not in names:
                add("error", "dependencies", f"{dep.name}: log {n!r} is not one of the log sources")

    # ---- commands ----------------------------------------------------------------
    from tools.sandbox import _argv
    for label, command in (("repro", cfg.commands.repro), ("test", cfg.commands.test)):
        if not command:
            continue
        try:
            argv = _argv(command)
        except ValueError as e:
            add("error", "commands", f"{label}: cannot parse {command!r}: {e}")
            continue
        if _exe(argv[0]):
            add("ok", "commands", f"{label}: {command}")
        else:
            add("error", "commands", f"{label}: {argv[0]!r} is not installed or not on PATH")
    if not cfg.commands.test:
        add("warn", "commands", "no test command: a fix is only checked by the repro")
    for f in cfg.commands.files:
        if not cfg.resolve(f).is_file():
            add("error", "commands", f"file {f} does not exist")

    # ---- policy and business ----------------------------------------------------------------
    for key in cfg.severity.approvals:
        if key not in ("sev1", "sev2", "sev3", "sev4", "default"):
            add("error", "severity", f"approvals key {key!r} must be sev1..sev4 or default")
    if cfg.severity.rules:
        add("ok", "severity", f"{len(cfg.severity.rules)} severity rules; approvals for {', '.join(cfg.severity.approvals) or 'none (global default)'}")
    if cfg.business:
        if cfg.resolve(cfg.business).is_file():
            add("ok", "business", f"business assumptions in {cfg.business}")
        else:
            add("warn", "business", f"{cfg.business} not found: no revenue-at-risk figures")
    if not (ROOT / "scenarios" / "service_maps" / f"{cfg.service.name}.json").is_file():
        add("info", "impact", f"no service map scenarios/service_maps/{cfg.service.name}.json: the impact graph is off")

    if full and not any(c.status == "error" and c.area in ("repo", "commands") for c in out):
        out += _dry_run(cfg)
    return Report(cfg.key, str(cfg.file) if cfg.file else None, out)


def _summary_line(output: str) -> str:
    """The line of a run's output that says the most: a test summary if there is one, else the last line."""
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    if not lines:
        return "(no output)"
    summary = [l for l in lines if re.search(r"(passed|failed)|^# (pass|fail) \d", l)]
    return "; ".join(summary[-2:])[:160] if summary else lines[-1][:160]


def _dry_run(cfg: ServiceConfig) -> list[Check]:
    """Run the repro and tests on a clean clone, exactly as the sandbox will."""
    from tools.sandbox import _argv, _mode, _run, prepare
    from tools.workspace import Workspace
    out = []
    mode = _mode()
    work = Path(tempfile.mkdtemp(prefix="ic-check-"))
    try:
        prepare(Workspace(cfg.base_dir, cfg), work)
        r = _run(mode, work, _argv(cfg.commands.repro), cfg.commands.image, cfg.commands.timeout)
        tail = _summary_line(r.output)
        if r.exit_code in (124, 127):
            out.append(Check("error", "sandbox", f"repro could not run ({mode}): {tail}"))
        else:
            state = "healthy now (exit 0)" if r.exit_code == 0 else f"reproduces the failure now (exit {r.exit_code})"
            out.append(Check("ok", "sandbox", f"repro ran in {r.seconds}s ({mode}) and {state}: {tail}"))
        if cfg.commands.test and (not cfg.commands.test_if_exists or (work / _argv(cfg.commands.test_if_exists)[0]).exists()):
            t = _run(mode, work, _argv(cfg.commands.test), cfg.commands.image, cfg.commands.timeout)
            tail = _summary_line(t.output)
            out.append(Check("ok" if t.exit_code == 0 else "warn", "sandbox",
                             f"tests {'pass' if t.exit_code == 0 else f'fail (exit {t.exit_code})'} on HEAD in {t.seconds}s: {tail}"))
    except Exception as e:  # a broken clone or copy is a finding, not a crash
        out.append(Check("error", "sandbox", f"could not prepare the sandbox: {e}"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out
