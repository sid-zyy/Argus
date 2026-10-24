"""The incident state machine: triage -> verify -> (code history) -> fix -> sandbox -> approval -> report.

Refutations are fed back to Triage; sandbox failures are fed back to Fix. After MAX_ATTEMPTS
without a supported claim (or a passing fix) the incident is escalated to a human.
"""
from __future__ import annotations

from agents import investigators as ag
from agents.base import AgentError
from llm import LLMError
from orchestrator import policy
from orchestrator.approval import Approver
from orchestrator.state import Claim, SuspectChange, Verdict
from orchestrator.trace import Trace
from tools.evidence_check import cause_evidence_missing, check_all
from tools.sandbox import run_sandbox
from tools.workspace import Workspace

MAX_ATTEMPTS = 3
# Shown live in the UI so people always know what is happening and why.
PHASES = {
    "plan": ("Making a plan", "The commander decides which checks to run, in what order, and why."),
    "investigate": ("Looking into it", "The investigator reads the logs and metrics and writes down possible causes."),
    "verify": ("Double-checking", "The fact checker tries to prove each possible cause wrong, using only real evidence."),
    "history": ("Checking recent changes", "The change finder looks for a recent code or settings change that lines up with the problem."),
    "fix": ("Writing a fix", "The fixer writes the smallest change that removes the confirmed cause."),
    "test": ("Testing the fix", "The fix is tried on a safe copy: the problem should happen without it and go away with it."),
    "approval": ("Waiting for approval", "A person reviews the tested fix. Nothing changes until they approve."),
}


def _phase(trace: Trace, key: str, **extra) -> None:
    label, detail = PHASES[key]
    trace.emit("orchestrator", "phase", {"key": key, "label": label, "detail": detail, **extra})


def _emit(trace: Trace):
    return lambda actor, type_, payload: trace.emit(actor, type_, payload)


def _clean_verdicts(ws: Workspace, claims: list[Claim], verdicts: list[Verdict]) -> list[Verdict]:
    """Downgrade any verdict whose evidence still fails the code check; fill in missing verdicts."""
    by_id = {}
    category = {c.id: c.category for c in claims}
    for v in verdicts:
        bad = check_all(ws, v.evidence)
        if bad or (v.verdict != "insufficient" and not v.evidence):
            v.evidence = [e for e in v.evidence if not check_all(ws, [e])]
            v.reasoning += f" [Downgraded from '{v.verdict}': evidence failed the code check: {'; '.join(bad) or 'no evidence'}]"
            v.verdict = "insufficient"
        if v.verdict == "supported" and (why := cause_evidence_missing(category.get(v.claim_id, "code"), v.evidence)):
            v.reasoning += f" [Downgraded from 'supported': {why}]"
            v.verdict = "insufficient"
        by_id.setdefault(v.claim_id, v)
    for c in claims:
        if c.id not in by_id:
            by_id[c.id] = Verdict(claim_id=c.id, verdict="insufficient", reasoning="Verifier returned no verdict for this claim.", evidence=[])
    return [by_id[c.id] for c in claims]


def _feedback(claims: list[Claim], verdicts: list[Verdict]) -> str:
    out = []
    for c, v in zip(claims, verdicts):
        ev = "; ".join(f"{e.source}:{e.line} '{e.quote[:120]}'" for e in v.evidence)
        out.append(f"- {c.id} \"{c.statement}\" was {v.verdict.upper()}: {v.reasoning} Evidence: {ev or 'none'}")
    return "\n".join(out)


def run_incident(ws: Workspace, llm, trace: Trace, approve: Approver, claim_offset: int = 0) -> dict:
    """claim_offset keeps claim ids unique when an incident is re-run (C4, C5 ... after C1-C3)."""
    emit = _emit(trace)
    trace.emit("orchestrator", "incident_opened", {"alert": ws.alert, "providers": llm.available() if hasattr(llm, "available") else []})
    state = {"status": "investigating", "claims": [], "verdicts": [], "suspects": [], "fix": None, "sandbox": None,
             "approval": None, "attempts": 0, "supported": None}
    feedback, suspects, counter = "", None, claim_offset

    try:
        _plan(ws, llm, emit, trace, 1)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            state["attempts"] = attempt
            if over := _over_budget(trace):
                return _escalate(trace, state, over)
            if attempt > 1:
                _plan(ws, llm, emit, trace, attempt, feedback, suspects)
            trace.emit("orchestrator", "attempt", {"n": attempt, "max": MAX_ATTEMPTS, "stage": "investigate"})
            _phase(trace, "investigate", attempt=attempt)
            t = ag.triage(ws, llm, emit, feedback, suspects)
            for c in t.claims:
                counter += 1
                c.id = f"C{counter}"
                trace.emit("triage", "claim", c.model_dump())
            state["claims"] += t.claims
            if not t.claims:
                feedback = "Your previous claims had no evidence that passed the citation check. Cite real lines."
                continue
            _phase(trace, "verify", attempt=attempt, claims=[c.id for c in t.claims])
            v, _ = ag.verifier(ws, t.claims, llm, emit)
            verdicts = _clean_verdicts(ws, t.claims, v.verdicts)
            for verdict in verdicts:
                trace.emit("verifier", "verdict", verdict.model_dump())
            state["verdicts"] += verdicts
            supported = next(((c, vd) for c, vd in zip(t.claims, verdicts) if vd.verdict == "supported"), None)
            if suspects is None:
                suspects = _code_history(ws, llm, emit, trace)
                state["suspects"] = [s.model_dump() for s in suspects]
            if supported:
                state["supported"] = supported
                break
            feedback = _feedback(t.claims, verdicts)

        if not state["supported"]:
            return _escalate(trace, state, f"No claim was supported after {MAX_ATTEMPTS} attempts.")

        claim, verdict = state["supported"]
        failure = ""
        for fix_attempt in range(1, MAX_ATTEMPTS + 1):
            if over := _over_budget(trace):
                return _escalate(trace, state, over)
            trace.emit("orchestrator", "attempt", {"n": fix_attempt, "max": MAX_ATTEMPTS, "stage": "fix"})
            _phase(trace, "fix", attempt=fix_attempt)
            f = ag.fix(ws, claim, verdict.reasoning, suspects or [], llm, emit, failure)
            _phase(trace, "test", attempt=fix_attempt, fix_type=f.fix_type)
            result = run_sandbox(ws, f)
            trace.emit("fix", "patch", {**f.model_dump(), "diff": result.diff})
            trace.emit("sandbox", "sandbox", result.model_dump())
            state["fix"], state["sandbox"] = f, result
            blocked = policy.check_fix(f.fix_type, result.diff) if result.passed else []
            if blocked:  # works, but not allowed: the fixer gets the rule and tries again
                trace.emit("orchestrator", "policy_blocked", {"violations": blocked, **policy.ref(blocked[0]["rule"])})
                failure = ("The fix passed testing but breaks the team's policy, so it can't be offered for approval: "
                           + "; ".join(f"{b['reason']} (rule {b['rule']})" for b in blocked) + ". Write a fix that stays within it.")
                continue
            if result.passed:
                break
            failure = (f"error: {result.error}\n" if result.error else "") + \
                      f"after-fix replay: {result.after.output if result.after else 'not run'}\n" + \
                      f"tests: {result.tests.output if result.tests else 'not run'}"
        else:
            why = "within the policy" if state["sandbox"] and state["sandbox"].passed else "the sandbox"
            return _escalate(trace, state, f"No fix passed {why} after {MAX_ATTEMPTS} attempts.")

        state["status"] = "awaiting_approval"
        _phase(trace, "approval")
        trace.emit("orchestrator", "awaiting_approval", {"claim_id": claim.id, "fix_type": state["fix"].fix_type})
        decision = approve({"claim_id": claim.id, "fix_type": state["fix"].fix_type, "diff": state["sandbox"].diff})
        trace.emit("human", "approval", decision)
        state["approval"] = decision
        state["status"] = "applied" if decision["approved"] else "rejected"
        return state
    except (AgentError, LLMError) as e:
        trace.emit("orchestrator", "error", {"message": str(e)})
        return _escalate(trace, state, f"Pipeline error: {e}")


def _plan(ws, llm, emit, trace, version: int, feedback: str = "", suspects=None) -> None:
    _phase(trace, "plan", attempt=version)
    plan = ag.planner(ws, llm, emit, feedback, suspects)
    trace.emit("planner", "plan", {"version": version, "steps": [s.model_dump() for s in plan.steps], "rationale": plan.rationale})


def _code_history(ws, llm, emit, trace) -> list[SuspectChange]:
    """Helpful but not essential: if it fails, the investigation continues without suspects."""
    _phase(trace, "history")
    try:
        suspects = ag.code_history(ws, llm, emit).suspects
    except AgentError as e:
        trace.emit("code_history", "error", {"message": f"Could not finish: {e}. Continuing without it."})
        suspects = []
    trace.emit("code_history", "suspects", {"suspects": [s.model_dump() for s in suspects]})
    return suspects


def _over_budget(trace: Trace) -> str | None:
    """The policy's AI budget for one run, checked before each attempt so a run never starts work it can't pay for."""
    budget = policy.ai_budget()
    if not budget:
        return None
    spent = policy.ai_cost(sum(e["payload"].get("tokens", 0) or 0 for e in trace.of_type("llm_step")))
    if spent < budget:
        return None
    trace.emit("orchestrator", "policy_blocked", {"violations": [{"rule": "ai.budget_per_incident",
               "reason": f"AI spend reached {spent:g}; the budget is {budget:g}"}], **policy.ref("ai.budget_per_incident")})
    return f"The AI budget for this run ({budget:g}) is used up (spent {spent:g}), so a person takes over."


def _escalate(trace: Trace, state: dict, reason: str) -> dict:
    state["status"] = "escalated"
    state["escalation"] = reason
    trace.emit("orchestrator", "escalated", {"reason": reason})
    return state
