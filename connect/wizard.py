"""Argus Connect setup wizard: connect a new service, and check a service's config before running incidents on it.

python -m connect.wizard list                      every service Argus can run, and whether its config passes
python -m connect.wizard check <service> [--full]  validate one config (--full also runs the repro and tests once)
python -m connect.wizard new [<folder>]            answer a few questions, get an argus.yaml, then check it
python -m connect.wizard route <payload.json>      which service an alert would open an incident for, and the incident
python -m connect.wizard schema                    the standard incident input as JSON Schema
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from connect.adapters import alerts as alert_adapters
from connect.adapters.logs import LEVEL_KEYS, MESSAGE_KEYS, TIME_KEYS
from connect.config import CONFIG_NAME, SERVICES_DIR, ConfigError, find, load, service_folders
from connect.intake import alert_for
from connect.schema import json_schema
from connect.validate import Report, check_folder

console = Console()
MARK = {"ok": "[green]✓[/green]", "info": "[blue]i[/blue]", "warn": "[yellow]![/yellow]", "error": "[red]✗[/red]"}
PINO_LEVELS = {"10": "TRACE", "20": "DEBUG", "30": "INFO", "40": "WARN", "50": "ERROR", "60": "FATAL"}


def print_report(r: Report) -> None:
    console.print(f"\n[bold]{r.service}[/bold]  {r.file or '(built-in layout)'}")
    area = None
    for c in r.checks:
        label = c.area if c.area != area else ""
        area = c.area
        console.print(f"  {MARK[c.status]} [dim]{label:<12}[/dim] {c.message}")
    warnings = sum(c.status == "warn" for c in r.checks)
    if r.ok:
        console.print(f"  [green bold]Ready.[/green bold] {warnings} warning(s). Argus can run incidents on this service.")
    else:
        console.print(f"  [red bold]Not ready:[/red bold] {len(r.errors)} error(s), {warnings} warning(s). Argus will refuse to start incidents.")


def cmd_list(_args) -> int:
    t = Table(title="Services Argus can run")
    for col in ("folder", "service", "language", "alerts", "logs", "config"):
        t.add_column(col, no_wrap=col in ("folder", "service"))
    bad = 0
    for d in service_folders():
        r = check_folder(d)
        bad += not r.ok
        try:
            s = load(d).summary()
        except ConfigError:
            t.add_row(d.name, "?", "?", "?", "?", "[red]invalid[/red]")
            continue
        status = "[green]ready[/green]" if r.ok else f"[red]{len(r.errors)} error(s)[/red]"
        t.add_row(s["key"], s["service"], s["language"] or "-", s["alerts"],
                  f"{len(s['logs'])}: " + ", ".join(sorted({l['adapter'] for l in s["logs"]})), status)
    console.print(t)
    return 1 if bad else 0


def cmd_check(args) -> int:
    try:
        folder = find(args.service)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        return 2
    r = check_folder(folder, full=args.full)
    if args.json:
        print(json.dumps(r.as_dict(), indent=2))
    else:
        print_report(r)
    return 0 if r.ok else 1


def cmd_route(args) -> int:
    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    services = [load(d) for d in service_folders()]
    try:
        cfg, inc, fmt = alert_adapters.route(payload, services)
    except alert_adapters.RouteError as e:
        console.print(f"[red]Not routed:[/red] {e}")
        return 1
    console.print(f"[bold]{alert_adapters.ADAPTERS[fmt].label}[/bold] payload -> service folder [bold]{cfg.key}[/bold] ({cfg.service.name})")
    console.print_json(json.dumps(alert_for(cfg, inc)))
    return 0


def cmd_schema(_args) -> int:
    print(json_schema())
    return 0


# ---- new service -----------------------------------------------------------------------
def _rel(path: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:  # another drive on Windows
        return str(path.resolve())


def _guess_language(repo: Path) -> str | None:
    if (repo / "package.json").is_file():
        return "javascript"
    if any((repo / f).is_file() for f in ("pyproject.toml", "requirements.txt", "setup.py")) or list(repo.glob("**/*.py"))[:1]:
        return "python"
    if (repo / "go.mod").is_file():
        return "go"
    return None


DEFAULT_COMMANDS = {
    "javascript": ("node checks/repro.js {repo}", 'node --test "{repo}/test/*.test.js"', "checks/repro.js"),
    "python": ("python checks/repro.py {repo}", "python -m pytest -q -p no:cacheprovider {repo}/tests", "checks/repro.py"),
    "go": ("go run ./checks {repo}", "go test ./...", "checks/main.go"),
}


def describe_log(path: Path) -> dict:
    """Guess a log file's adapter and field names from its first line."""
    first = ""
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                first = line.strip()
                break
    if path.suffix.lower() == ".csv":
        header = first.split(",")
        time_key = next((h for h in header if h.lower() in TIME_KEYS), header[0] if header else "timestamp")
        return {"adapter": "csv", "fields": {"time": time_key}}
    try:
        obj = json.loads(first)
    except json.JSONDecodeError:
        return {"adapter": "text"}
    if not isinstance(obj, dict):
        return {"adapter": "text"}
    spec: dict = {"adapter": "jsonl", "fields": {}}
    for field, keys in (("time", TIME_KEYS), ("level", LEVEL_KEYS), ("message", MESSAGE_KEYS)):
        if key := next((k for k in keys if k in obj), None):
            spec["fields"][field] = key
    t = obj.get(spec["fields"].get("time"))
    if isinstance(t, (int, float)):
        spec["time_format"] = "epoch_ms" if t > 1e11 else "epoch_s"
    if isinstance(obj.get(spec["fields"].get("level")), int):
        spec["level_names"] = PINO_LEVELS
    if {"pid", "hostname"} <= set(obj):
        spec["drop"] = ["pid", "hostname"]
    return spec


def cmd_new(args) -> int:
    console.print("[bold]Connect a service to Argus[/bold]. Press Enter to accept a [cyan]default[/cyan].\n")
    key = args.folder or Prompt.ask("Folder name for this service (under scenarios/)")
    folder = SERVICES_DIR / key
    if (folder / CONFIG_NAME).exists() and not Confirm.ask(f"{folder / CONFIG_NAME} exists. Overwrite it?", default=False):
        return 1
    folder.mkdir(parents=True, exist_ok=True)

    name = Prompt.ask("Service name, as your alerts label it", default=key.replace("_", "-"))
    label = Prompt.ask("Friendly name for people", default=name)
    team = Prompt.ask("Owning team", default="")
    on_call = Prompt.ask("Who is on call (an Argus user)", default="")

    repo_in = Prompt.ask("Git repo: a local path or a clone URL", default="repo")
    is_url = "://" in repo_in or repo_in.startswith("git@")
    repo_path = None if is_url else (folder / repo_in if not Path(repo_in).is_absolute() else Path(repo_in))
    language = _guess_language(repo_path) if repo_path and repo_path.is_dir() else None
    language = Prompt.ask("Language", default=language or "python")

    tool = Prompt.ask("Monitoring tool that sends alerts", choices=list(alert_adapters.ADAPTERS), default="alertmanager")
    sample = Prompt.ask("Path to an example alert payload (optional)", default="")

    logs = []
    console.print("\nLog sources: give each file's path. Leave the path empty when done.")
    while True:
        p = Prompt.ask(f"  Log file #{len(logs) + 1} path", default="")
        if not p:
            break
        path = Path(p) if Path(p).is_absolute() else folder / p
        spec = describe_log(path) if path.is_file() else {"adapter": "text"}
        if not path.is_file():
            console.print(f"  [yellow]{path} does not exist yet; assuming plain text[/yellow]")
        default_name = Path(p).stem + ".log" if spec["adapter"] != "text" else Path(p).name
        entry = {"name": Prompt.ask("    Name the agents should use", default=default_name),
                 "path": _rel(path, folder), **spec,
                 "role": Prompt.ask("    Role", choices=["service", "metrics", "dependency", "deploy", "other"], default="service")}
        if entry["role"] == "dependency":
            entry["dependency"] = Prompt.ask("    Which system writes it")
        console.print(f"    [dim]{spec['adapter']} adapter{', fields ' + json.dumps(spec['fields']) if spec.get('fields') else ''}[/dim]")
        logs.append(entry)

    repro_d, test_d, file_d = DEFAULT_COMMANDS.get(language, DEFAULT_COMMANDS["python"])
    console.print("\nThe repro command must exit 0 when the service is healthy and non-zero while the failure happens.")
    repro = Prompt.ask("Repro command ({repo} is the sandbox clone)", default=repro_d)
    test = Prompt.ask("Test command (empty for none)", default=test_d)
    files = [f.strip() for f in Prompt.ask("Files to copy next to the clone (comma separated)", default=file_d).split(",") if f.strip()]
    default_sev = Prompt.ask("Severity when an alert has none", choices=["sev1", "sev2", "sev3", "sev4"], default="sev3")

    config = {
        "version": 1,
        "service": {k: v for k, v in {"name": name, "label": label, "team": team, "language": language, "on_call": on_call}.items() if v},
        "repo": {"adapter": "git", **({"url": repo_in} if is_url else {"path": _rel(repo_path, folder)})},
        "alerts": {"adapter": tool, "match": {"service": name}, **({"sample": sample} if sample else {})},
        "logs": logs,
        "commands": {"files": files, "repro": repro, **({"test": test} if test else {})},
        "severity": {"default": default_sev},
    }
    metrics = [l["name"] for l in logs if l["role"] == "metrics"]
    if metrics:
        config["metrics"] = {"source": metrics[0]}
    header = f"# Argus Connect config for {name}, written by python -m connect.wizard new.\n# Check it with: python -m connect.wizard check {key}\n"
    (folder / CONFIG_NAME).write_text(header + yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    console.print(f"\nWrote [bold]{folder / CONFIG_NAME}[/bold]. Checking it now:")
    r = check_folder(folder, full=Confirm.ask("Also run the repro and tests once in the sandbox?", default=True))
    print_report(r)
    return 0 if r.ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m connect.wizard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    c = sub.add_parser("check")
    c.add_argument("service", help="folder name under scenarios/, or a path to a folder or argus.yaml")
    c.add_argument("--full", action="store_true", help="also run the repro and tests once in the sandbox")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)
    n = sub.add_parser("new")
    n.add_argument("folder", nargs="?")
    n.set_defaults(fn=cmd_new)
    r = sub.add_parser("route")
    r.add_argument("payload")
    r.set_defaults(fn=cmd_route)
    sub.add_parser("schema").set_defaults(fn=cmd_schema)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
