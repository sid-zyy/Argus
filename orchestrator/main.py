"""Run one incident end to end in the terminal, on any connected service.

python -m orchestrator.main scenarios/incident1_conn_leak [--provider gemini] [--approve ask|yes|no] [--save-replay] [-v]
python -m orchestrator.main scenarios/quickbite_orders --alert scenarios/quickbite_orders/alerts/grafana_alert.json

The service's argus.yaml is checked first (as `python -m connect.wizard check` does); a config with errors is refused.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from connect.adapters.alerts import RouteError, route
from connect.adapters.repos import repo_for
from connect.config import ConfigError, load
from connect.intake import alert_for
from connect.validate import check
from llm import LLM
from orchestrator import console
from orchestrator.approval import make_approver
from orchestrator.pipeline import run_incident
from orchestrator.report import build_report
from orchestrator.trace import Trace
from scenarios.build import build
from tools.workspace import Workspace

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="scenarios/incident1_conn_leak")
    ap.add_argument("--provider", help="gemini | groq | ollama (default: LLM_PROVIDER)")
    ap.add_argument("--approve", default="ask", choices=["ask", "yes", "no"], help="'yes'/'no' only for unattended eval runs")
    ap.add_argument("--save-replay", action="store_true", help="copy the trace to replay/<scenario>.json for the UI's Replay button")
    ap.add_argument("--alert", help="a monitoring payload (Alertmanager, Grafana or standard) to start from instead of the base alert")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    scenario = Path(args.scenario)
    try:
        cfg = load(scenario)
    except ConfigError as e:
        sys.exit(f"Invalid service config: {e}")
    if not (cfg.repo_path() / ".git").exists():
        build(scenario.name) if (scenario / "history.py").is_file() else repo_for(cfg).ensure()
    checked = check(cfg)
    if not checked.ok:
        sys.exit("Not running: the service config has errors:\n" + "\n".join(f"  - [{c.area}] {c.message}" for c in checked.errors)
                 + f"\nSee: python -m connect.wizard check {scenario}")
    ws = Workspace(scenario, cfg)
    incoming = None
    if args.alert:
        try:
            _, incoming, fmt = route(json.loads(Path(args.alert).read_text(encoding="utf-8")), [cfg])
        except RouteError as e:
            sys.exit(f"That alert is not for {cfg.service.name}: {e}")
        console.console.print(f"[dim]Alert received through the {fmt} adapter.[/dim]")
    ws.alert = alert_for(cfg, incoming)
    llm = LLM(provider=args.provider)
    trace = Trace([lambda e: console.render(e, args.verbose)])
    state = run_incident(ws, llm, trace, make_approver(args.approve))

    out = ROOT / "runs" / f"{scenario.name}-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    report = build_report(ws.alert, state, trace, llm, scenario.name)
    (out / "incident_report.md").write_text(report, encoding="utf-8")
    trace.emit("reporter", "report", {"path": str(out / "incident_report.md"), "markdown": report})
    trace.save(out / "trace.json")
    if args.save_replay and state["status"] == "escalated":
        console.console.print("[yellow]Not saving a replay: this run was escalated. Replays should show a run that reached a fix.[/yellow]")
    elif args.save_replay:
        (ROOT / "replay").mkdir(exist_ok=True)
        shutil.copy(out / "trace.json", ROOT / "replay" / f"{scenario.name}.json")
    console.console.print(f"[bold]Status:[/bold] {state['status']}   trace: {out / 'trace.json'}")
    return state


if __name__ == "__main__":
    main()
