"""Incident report: business summary first, then the evidence. Tables come straight from the trace; only the
narrative is LLM-written, and business figures are computed from the scenario's stated assumptions."""
from __future__ import annotations

from agents import investigators as ag
from agents.base import AgentError
from db.business import impact as business_impact
from db.store import WH_PER_1K_TOKENS
from llm import LLMError
from orchestrator.trace import Trace

VERDICT = {"supported": "Confirmed", "refuted": "Ruled out", "insufficient": "Not enough proof"}
FIX = {"patch": "Code fix", "config_change": "Settings change", "rollback": "Roll back"}


def money(value: float, currency: str = "INR") -> str:
    """₹ amounts in lakh/crore, the way Indian business readers expect them."""
    if currency != "INR":
        return f"{currency} {value:,.0f}"
    if value >= 1e7:
        return f"₹{value / 1e7:.2f} crore"
    if value >= 1e5:
        return f"₹{value / 1e5:.1f} lakh"
    return f"₹{value:,.0f}"


def incident_impact(scenario: str | None, alert: dict, state: dict, trace: Trace) -> dict | None:
    if not scenario or not trace.events:
        return None
    approval = next((e for e in reversed(trace.events) if e["type"] == "approval"), None)
    fixed = bool(approval and approval["payload"].get("approved"))
    return business_impact({"scenario": scenario, "alert": alert, "created_at": trace.events[0]["ts"],
                            "resolved_at": approval["ts"] if fixed else None, "status": "resolved" if fixed else state["status"]})


def impact_section(impact: dict | None) -> list[str]:
    """The '## Business impact' section. Replays rebuild it, so a recorded report shows today's assumptions."""
    if not impact:
        return []
    cur = impact["currency"]
    md = ["## Business impact", "",
          "| | |", "|---|---|",
          f"| Customers affected | about {impact['affected_per_minute']:,.0f} {impact['unit']} per minute |",
          f"| Revenue at risk | {money(impact['at_risk_per_minute'], cur)} per minute |",
          f"| Time to fix with Argus | {impact['minutes_so_far']:.0f} minutes (including detection and human approval) |",
          f"| Typical manual fix | about {impact['manual_minutes']:.0f} minutes |"]
    if impact["fixed"]:
        md += [f"| Revenue protected | {money(impact['saved_value'], cur)} (estimate) |",
               f"| Engineer time saved | about {impact['engineer_hours_saved']} hours |"]
    md += ["", "Estimates, based on: " + "; ".join(impact["assumptions"]) + ".", ""]
    if impact.get("sources"):
        md += ["Sources: " + ", ".join(f"<{u}>" for u in impact["sources"]), ""]
    return md


def _record(alert: dict, state: dict, trace: Trace, impact: dict | None) -> dict:
    return {
        "alert": alert,
        "status": state["status"],
        "attempts": state["attempts"],
        "business_impact": impact and {k: impact[k] for k in ("service_label", "unit", "affected_per_minute", "at_risk_per_minute",
                                                             "minutes_so_far", "manual_minutes", "saved_value", "currency")},
        "claims": [e["payload"] for e in trace.of_type("claim")],
        "verdicts": [e["payload"] for e in trace.of_type("verdict")],
        "suspects": state.get("suspects", []),
        "fix": state["fix"].model_dump() if state.get("fix") else None,
        "sandbox": state["sandbox"].model_dump(exclude={"diff"}) if state.get("sandbox") else None,
        "approval": state.get("approval"),
        "escalation": state.get("escalation"),
    }


def build_report(alert: dict, state: dict, trace: Trace, llm, scenario: str | None = None) -> str:
    impact = incident_impact(scenario, alert, state, trace)
    record = _record(alert, state, trace, impact)
    emit = lambda a, t, p: trace.emit(a, t, p)  # noqa: E731
    trace.emit("orchestrator", "phase", {"key": "report", "label": "Writing the summary",
                                         "detail": "The writer turns everything that happened into a short report."})
    try:
        narrative = ag.reporter(record, llm, emit)
    except (AgentError, LLMError) as e:
        narrative = None
        trace.emit("reporter", "error", {"message": str(e)})

    verdict_by = {v["claim_id"]: v for v in record["verdicts"]}
    steps = trace.of_type("llm_step")
    tokens = sum(e["payload"].get("tokens", 0) or 0 for e in steps)
    models = sorted({f"{e['payload']['provider']}/{e['payload']['model']}" for e in steps if e["payload"].get("provider")})
    outcome = {"applied": "Fixed (approved)", "rejected": "Fix declined", "escalated": "Handed to a person"}.get(state["status"], state["status"].capitalize())

    md = [f"# Incident report: {alert['title']}", "",
          f"- **Outcome:** {outcome}" + (f". {state['escalation']}" if state.get("escalation") else ""),
          f"- **Service:** {alert['service']} · **Severity:** {alert['severity']} · **Alert:** {alert['id']}",
          f"- **Started:** {alert.get('started_at') or '–'} · **Alert sent:** {alert.get('fired_at') or '–'}", ""]

    md += ["## Business summary", narrative.business_summary if narrative else "_Summary unavailable; the facts below come straight from the record._", ""]
    md += impact_section(impact)

    if narrative:
        md += ["## What happened", narrative.summary, "", "## Root cause", narrative.root_cause, ""]

    md += ["## Causes considered", "", "Every quoted line below was checked by code against the real file.", ""]
    for c in record["claims"]:
        v = verdict_by.get(c["id"], {})
        md.append(f"### {c['id']}: {c['statement']}")
        md.append(f"- **{VERDICT.get(v.get('verdict'), 'Not checked')}.** {v.get('reasoning', '')}")
        for e in c["evidence"] + v.get("evidence", []):
            md.append(f"  - `{e['source']}:{e['line']}`: `{e['quote'][:200]}`")
        md.append("")

    if record["suspects"]:
        md += ["## Change that caused it", ""] + [f"- `{s['sha']}` {s['summary']}. {s['reason']}" for s in record["suspects"]] + [""]

    sb = state.get("sandbox")
    if state.get("fix"):
        f = state["fix"]
        md += ["## Fix", f"- **{FIX.get(f.fix_type, f.fix_type)}**" + (f" to `{f.rollback_to}`" if f.rollback_to else "") + f". {f.rationale}", ""]
        if sb and sb.diff:
            md += ["```diff", sb.diff.rstrip(), "```", ""]
    if sb:
        md += ["## Test result",
               f"- Without the fix: {'working' if sb.before.exit_code == 0 else 'broken'} (`{sb.before.output[:200]}`)"]
        if sb.after:
            md.append(f"- With the fix: {'working' if sb.after.exit_code == 0 else 'broken'} (`{sb.after.output[:200]}`)")
        if sb.tests:
            md.append(f"- Project tests: {'all pass' if sb.tests.exit_code == 0 else 'failing'}")
        md += [f"- **{'Pass' if sb.passed else 'Fail'}** · tested in a {'container with no network' if sb.mode == 'docker' else 'temporary copy (no network isolation)'}", ""]

    md += ["## Decisions made by people", ""]
    people = [e for e in trace.events if e["type"] in ("approval_vote", "approval", "qa", "rerun")]
    if not people:
        md.append("- No human decision was needed or made.")
    for e in people:
        p = e["payload"]
        if e["type"] == "approval":
            md.append(f"- {e['ts'][:16].replace('T', ' ')}: Fix **{'approved' if p['approved'] else 'declined'}** by {p['approver']} ({p['role']}). {p.get('reason') or ''}")
        elif e["type"] == "approval_vote":
            md.append(f"- {e['ts'][:16].replace('T', ' ')}: {p['approver']} ({p['role']}) {p['decision']} the fix. {p.get('reason') or ''}")
        elif e["type"] == "qa":
            md.append(f"- {e['ts'][:16].replace('T', ' ')}: {p['tester']} tested it: {'fixed' if p['result'] == 'verified' else 'still broken'}. {p.get('note') or ''}")
        else:
            md.append(f"- {e['ts'][:16].replace('T', ' ')}: {p['by']} asked Argus to try again. {p.get('note') or ''}")
    deployed = next((e["payload"] for e in trace.events if e["type"] == "deployed"), None)
    md += [f"- The approved fix was committed as `{deployed['commit']}` and deployed to {deployed['app']}." if deployed
           else "- This is a demo: applying the fix is simulated, no real system was changed.", ""]

    if narrative and narrative.follow_ups:
        md += ["## Follow-ups"] + [f"- {x}" for x in narrative.follow_ups] + [""]

    md += ["## AI usage", f"- {len(steps)} AI calls, {tokens:,} tokens, about {tokens / 1000 * WH_PER_1K_TOKENS:.1f} Wh of energy "
           f"(rough estimate at {WH_PER_1K_TOKENS} Wh per 1,000 tokens)", f"- Models: {', '.join(models) or '–'}", ""]
    return "\n".join(md) + "\n"
