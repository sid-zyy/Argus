"""Incident report: evidence tables come straight from the trace; only the narrative is LLM-written."""
from __future__ import annotations

from agents import investigators as ag
from agents.base import AgentError
from llm import LLMError
from orchestrator.trace import Trace


def _record(alert: dict, state: dict, trace: Trace) -> dict:
    return {
        "alert": alert,
        "status": state["status"],
        "attempts": state["attempts"],
        "claims": [e["payload"] for e in trace.of_type("claim")],
        "verdicts": [e["payload"] for e in trace.of_type("verdict")],
        "suspects": state.get("suspects", []),
        "fix": state["fix"].model_dump() if state.get("fix") else None,
        "sandbox": state["sandbox"].model_dump(exclude={"diff"}) if state.get("sandbox") else None,
        "approval": state.get("approval"),
        "escalation": state.get("escalation"),
    }


def build_report(alert: dict, state: dict, trace: Trace, llm) -> str:
    record = _record(alert, state, trace)
    emit = lambda a, t, p: trace.emit(a, t, p)  # noqa: E731
    trace.emit("orchestrator", "phase", {"key": "report", "label": "Writing the summary",
                                         "detail": "The writer turns everything that happened into a short report."})
    try:
        narrative = ag.reporter(record, llm, emit)
    except (AgentError, LLMError) as e:
        narrative = None
        trace.emit("reporter", "error", {"message": str(e)})

    verdict_by = {v["claim_id"]: v for v in record["verdicts"]}
    models = sorted({f"{e['payload']['provider']}/{e['payload']['model']}" for e in trace.of_type("llm_step") if e["payload"].get("provider")})
    md = [f"# Incident report: {alert['title']}", "",
          f"- **Alert:** {alert['id']} on `{alert['service']}` ({alert['severity']}), started {alert.get('started_at')}, fired {alert.get('fired_at')}",
          f"- **Outcome:** {state['status']}" + (f" ({state['escalation']})" if state.get("escalation") else ""),
          f"- **Investigation attempts:** {state['attempts']}",
          f"- **Models:** {', '.join(models) or 'n/a'}", ""]
    if narrative:
        md += ["## Summary", narrative.summary, "", "## Impact", narrative.impact, "", "## Root cause", narrative.root_cause, ""]
    else:
        md += ["## Summary", "_Narrative unavailable (reporter model failed). Facts below are taken directly from the trace._", ""]

    md += ["## Hypotheses and verdicts", "",
           "Every cited line below was checked by code against the source file.", ""]
    for c in record["claims"]:
        v = verdict_by.get(c["id"], {})
        md.append(f"### {c['id']}: {c['statement']}")
        md.append(f"- Category: {c['category']}")
        md.append(f"- **Verdict: {v.get('verdict', 'n/a').upper()}**: {v.get('reasoning', '')}")
        for e in c["evidence"]:
            md.append(f"  - Claim evidence `{e['source']}:{e['line']}`: `{e['quote'][:200]}`")
        for e in v.get("evidence", []):
            md.append(f"  - Verifier evidence `{e['source']}:{e['line']}`: `{e['quote'][:200]}`")
        md.append("")

    if record["suspects"]:
        md += ["## Suspect changes", ""] + [f"- `{s['sha']}` {s['summary']}: {s['reason']}" for s in record["suspects"]] + [""]

    sb = state.get("sandbox")
    if state.get("fix"):
        f = state["fix"]
        md += ["## Fix", f"- Type: {f.fix_type}" + (f" to `{f.rollback_to}`" if f.rollback_to else ""), f"- Rationale: {f.rationale}", ""]
        if sb and sb.diff:
            md += ["```diff", sb.diff.rstrip(), "```", ""]
    if sb:
        md += ["## Sandbox test", f"- Mode: {sb.mode}" + (" (no network isolation)" if sb.mode == "subprocess" else " (no network, CPU/memory limits)"),
               f"- Before fix: exit {sb.before.exit_code}: `{sb.before.output[:300]}`"]
        if sb.after:
            md.append(f"- After fix: exit {sb.after.exit_code}: `{sb.after.output[:300]}`")
        if sb.tests:
            md.append(f"- Repo tests: exit {sb.tests.exit_code}: `{(sb.tests.output.splitlines() or [''])[-1]}`")
        md.append(f"- **Result: {'PASS' if sb.passed else 'FAIL'}**")
        md.append("")
    if state.get("approval"):
        a = state["approval"]
        md += ["## Approval", f"- {'Approved' if a['approved'] else 'Rejected'} by {a['approver']} ({a['role']}). {a.get('reason') or ''}",
               "- Apply is simulated; no production system was changed.", ""]
    if narrative and narrative.follow_ups:
        md += ["## Follow-ups"] + [f"- {x}" for x in narrative.follow_ups] + [""]

    md += ["## Timeline", ""]
    for e in trace.events:
        if e["type"] in ("incident_opened", "attempt", "claim", "verdict", "suspects", "patch", "sandbox", "awaiting_approval", "approval", "escalated"):
            label = e["payload"].get("id") or e["payload"].get("claim_id") or e["payload"].get("verdict") or e["payload"].get("stage") or ""
            md.append(f"- {e['ts']} **{e['actor']}** {e['type']} {label}")
    return "\n".join(md) + "\n"
