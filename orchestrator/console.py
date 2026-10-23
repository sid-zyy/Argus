"""Rich terminal view of the event stream (the fallback demo view; the web UI consumes the same events)."""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()
VERDICT_STYLE = {"supported": "bold green", "refuted": "bold red", "insufficient": "bold yellow"}


def _evidence(evs: list[dict]) -> str:
    return "\n".join(f"  [dim]{e['source']}:{e['line']}[/dim] {e['quote'][:140]}" for e in evs)


def render(event: dict, verbose: bool = False) -> None:
    t, p, actor = event["type"], event["payload"], event["actor"]
    if t == "incident_opened":
        a = p["alert"]
        console.print(Panel(f"[bold]{a['title']}[/bold]\nservice={a['service']} severity={a['severity']} "
                            f"started={a.get('started_at')} fired={a.get('fired_at')}", title=f"ALERT {a['id']}", border_style="red"))
    elif t == "attempt":
        console.rule(f"Attempt {p['n']}/{p['max']}")
    elif t == "tool_call":
        args = ", ".join(f"{k}={v!r}" for k, v in p["args"].items())
        console.print(f"  [cyan]{actor}[/cyan] → {p['tool']}({args})")
        if verbose:
            console.print(f"    [dim]{p['result_preview'][:200]}[/dim]")
    elif t == "llm_step" and verbose:
        console.print(f"  [dim]{actor} step {p['step']} via {p['provider']}/{p['model']}[/dim]")
    elif t == "rejected":
        console.print(f"  [yellow]{actor} submission rejected by checks:[/yellow] {'; '.join(p['errors'])[:300]}")
    elif t == "claim":
        console.print(Panel(f"[bold]{p['statement']}[/bold]\ncategory: {p['category']}\n{_evidence(p['evidence'])}",
                            title=f"Claim {p['id']} (triage)", border_style="blue"))
    elif t == "verdict":
        style = VERDICT_STYLE[p["verdict"]]
        console.print(Panel(f"[{style}]{p['verdict'].upper()}[/{style}]  {p['reasoning']}\n{_evidence(p['evidence'])}",
                            title=f"Verdict on {p['claim_id']} (verifier)", border_style=style.split()[-1]))
    elif t == "suspects":
        for s in p["suspects"]:
            console.print(f"  [magenta]suspect[/magenta] {s['sha']}: {s['summary']} [dim]({s['reason'][:160]})[/dim]")
    elif t == "patch":
        console.print(Panel(f"{p['fix_type']}: {p['rationale']}", title="Proposed fix", border_style="cyan"))
        if p.get("diff"):
            console.print(Syntax(p["diff"], "diff", theme="ansi_dark", word_wrap=True))
    elif t == "sandbox":
        ok = "[bold green]PASS[/bold green]" if p["passed"] else "[bold red]FAIL[/bold red]"
        lines = [f"mode={p['mode']}  result={ok}",
                 f"before: exit={p['before']['exit_code']} {p['before']['output'][:200]}"]
        if p.get("after"):
            lines.append(f"after:  exit={p['after']['exit_code']} {p['after']['output'][:200]}")
        if p.get("tests"):
            lines.append(f"tests:  exit={p['tests']['exit_code']} {p['tests']['output'].splitlines()[-1] if p['tests']['output'] else ''}")
        if p.get("error"):
            lines.append(f"[red]{p['error']}[/red]")
        console.print(Panel("\n".join(lines), title="Sandbox", border_style="green" if p["passed"] else "red"))
    elif t == "awaiting_approval":
        console.print("[bold yellow]Awaiting human approval[/bold yellow]")
    elif t == "approval":
        word = "[green]APPROVED[/green]" if p["approved"] else "[red]REJECTED[/red]"
        console.print(f"{word} by {p['approver']} ({p['role']}) {p.get('reason') or ''}")
    elif t == "escalated":
        console.print(Panel(p["reason"], title="ESCALATED TO HUMAN", border_style="red"))
    elif t == "report":
        console.print(f"[bold]Report:[/bold] {p['path']}")
    elif t == "error":
        console.print(f"[red]{actor} error:[/red] {p['message']}")
