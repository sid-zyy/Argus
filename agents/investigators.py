"""Triage, Code-History, Verifier, Fix and Reporter agents: each is run_agent with its own prompt, tools and checks."""
from __future__ import annotations

import json

from agents.base import AgentError, prompt, run_agent
from orchestrator.state import (Claim, CodeHistoryOutput, FixOutput, Plan, PlanStep, ReportOutput, SuspectChange,
                                TriageOutput, VerifierOutput)
from tools.evidence_check import cause_evidence_missing, check_all
from tools.git_tools import file_tools, git_tools
from tools.logs import log_tools
from tools.metrics import metric_tools
from tools.sandbox import preview_fix
from tools.workspace import ToolSet, Workspace


def _alert_text(ws: Workspace) -> str:
    return "ALERT:\n" + json.dumps(ws.alert, indent=2)


def triage(ws: Workspace, llm, emit, feedback: str = "", suspects: list[SuspectChange] | None = None) -> TriageOutput:
    task = [_alert_text(ws)]
    if ws.extra_sources.get("report"):
        task.append("A tester filed this bug report (source 'report'). Treat it as a lead, not as proof:\n" + "\n".join(ws.extra_sources["report"]))
    if suspects:
        task.append("Code-History agent found these recent changes:\n" + "\n".join(f"- {s.sha}: {s.summary} ({s.reason})" for s in suspects))
    if feedback:
        task.append("FEEDBACK FROM THE VERIFIER on your earlier claims:\n" + feedback)

    def check(out: TriageOutput) -> list[str]:
        errors = []
        if not 1 <= len(out.claims) <= 3:
            errors.append("return between 1 and 3 claims")
        for c in out.claims:
            if not c.evidence:
                errors.append(f"{c.id}: a claim needs at least one piece of evidence")
            errors += [f"{c.id}: {e}" for e in check_all(ws, c.evidence)]
        return errors

    tools = log_tools(ws) + metric_tools(ws)
    if suspects:
        tools = tools + git_tools(ws) + file_tools(ws)
    out, errors = run_agent("triage", prompt("triage"), "\n\n".join(task), tools, TriageOutput, llm, emit, check)
    # Drop evidence that failed the check after the retries ran out; claims left with none are dropped.
    if errors:
        for c in out.claims:
            c.evidence = [e for e in c.evidence if not check_all(ws, [e])]
        out.claims = [c for c in out.claims if c.evidence]
    return out


def code_history(ws: Workspace, llm, emit) -> CodeHistoryOutput:
    def check(out: CodeHistoryOutput) -> list[str]:
        errors = []
        for s in out.suspects:
            try:
                ws.git("rev-parse", "--verify", f"{s.sha}^{{commit}}")
            except ValueError:
                errors.append(f"{s.sha}: not a commit in this repo")
            errors += [f"{s.sha}: {e}" for e in check_all(ws, s.evidence)]
        return errors

    tools = git_tools(ws) + file_tools(ws) + log_tools(ws)
    out, errors = run_agent("code_history", prompt("code_history"), _alert_text(ws), tools, CodeHistoryOutput, llm, emit, check, max_steps=10)
    for s in out.suspects:
        s.evidence = [e for e in s.evidence if not check_all(ws, [e])]
    return out


def verifier(ws: Workspace, claims: list[Claim], llm, emit) -> tuple[VerifierOutput, list[str]]:
    """Independent context: sees the alert and the claims, never the Triage agent's reasoning."""
    task = _alert_text(ws) + "\n\nCLAIMS TO TEST:\n" + json.dumps([c.model_dump() for c in claims], indent=2)
    ids = {c.id for c in claims}
    category = {c.id: c.category for c in claims}

    def check(out: VerifierOutput) -> list[str]:
        errors = []
        got = [v.claim_id for v in out.verdicts]
        if missing := ids - set(got):
            errors.append(f"missing verdicts for {sorted(missing)}")
        if extra := set(got) - ids:
            errors.append(f"unknown claim ids {sorted(extra)}")
        for v in out.verdicts:
            if v.verdict in ("supported", "refuted") and not v.evidence:
                errors.append(f"{v.claim_id}: a '{v.verdict}' verdict must cite evidence")
            errors += [f"{v.claim_id}: {e}" for e in check_all(ws, v.evidence)]
            if v.verdict == "supported" and (why := cause_evidence_missing(category.get(v.claim_id, "code"), v.evidence, ws)):
                errors.append(f"{v.claim_id}: cannot be 'supported' yet: {why}")
        return errors

    tools = log_tools(ws) + metric_tools(ws) + git_tools(ws) + file_tools(ws)
    return run_agent("verifier", prompt("verifier"), task, tools, VerifierOutput, llm, emit, check, max_steps=10)


def fix(ws: Workspace, claim: Claim, reasoning: str, suspects: list[SuspectChange], llm, emit, failure: str = "") -> FixOutput:
    task = [f"VERIFIED ROOT CAUSE ({claim.category}): {claim.statement}", f"Verifier reasoning: {reasoning}"]
    if suspects:
        task.append("Suspect changes:\n" + "\n".join(f"- {s.sha}: {s.summary}" for s in suspects))
    if failure:
        task.append("YOUR PREVIOUS FIX FAILED IN THE SANDBOX:\n" + failure)

    def check(out: FixOutput) -> list[str]:
        return preview_fix(ws, out)[1]

    tools = file_tools(ws) + git_tools(ws)
    out, _ = run_agent("fix", prompt("fix"), "\n\n".join(task), tools, FixOutput, llm, emit, check)
    return out


def reporter(record: dict, llm, emit) -> ReportOutput:
    task = "INVESTIGATION RECORD:\n" + json.dumps(record, indent=2, default=str)[:12000]
    out, _ = run_agent("reporter", prompt("reporter"), task, ToolSet(), ReportOutput, llm, emit, max_steps=3)
    return out


DEFAULT_PLAN = [
    ("investigator", "Find when the errors started and which requests fail"),
    ("fact_checker", "Try to disprove each possible cause with logs, metrics and code"),
    ("change_finder", "Check deploys and commits made just before the errors began"),
    ("fixer", "Write the smallest change that removes the confirmed cause"),
    ("test_lab", "Replay the failure without and with the fix"),
]


def planner(ws: Workspace, llm, emit, feedback: str = "", suspects: list[SuspectChange] | None = None) -> Plan:
    """The Commander writes the investigation plan up front, and a new one after guesses are ruled out."""
    task = [_alert_text(ws)]
    if suspects:
        task.append("Recent changes found so far:\n" + "\n".join(f"- {s.sha}: {s.summary}" for s in suspects))
    if feedback:
        task.append("EARLIER GUESSES THAT WERE RULED OUT:\n" + feedback)

    def check(out: Plan) -> list[str]:
        return [] if 3 <= len(out.steps) <= 7 else ["write between 4 and 6 steps"]

    try:
        out, _ = run_agent("planner", prompt("planner"), "\n\n".join(task), ToolSet(), Plan, llm, emit, check, max_steps=2)
        return out
    except AgentError:
        return Plan(steps=[PlanStep(agent=a, goal=g) for a, g in DEFAULT_PLAN],
                    rationale="Standard plan: find the onset, test each cause against evidence, then fix and test.")
