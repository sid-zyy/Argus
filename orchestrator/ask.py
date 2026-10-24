"""Ask Argus: answer a person's question about one incident, using only what is in its record.

The answer must cite where each fact comes from ([app.log:120], [commit a3f9c1d], [claim C1], [fix], [test]),
so people can check it the same way the agents' claims are checked.
"""
from __future__ import annotations

import json

from llm import LLM

MAX_QUESTION = 500
SYSTEM = """You are Argus, an AI incident commander. A person on the team is asking about one incident.
Answer ONLY from the incident record below. If the record does not contain the answer, say so plainly and suggest who
or what could answer it. Never invent log lines, commits, numbers or names.

Cite where each fact comes from, in square brackets, right after the fact:
- a log line: [app.log:123] (use the source and line exactly as they appear in the record)
- a code change: [commit abc1234]
- a possible cause: [claim C1]
- the suggested fix: [fix]   - the sandbox test: [test]   - the business estimate: [impact]

Write for a busy engineer or manager: plain language, at most 120 words, no headings. Short bullet points are fine."""


def _record(inc: dict) -> str:
    claims = [{"id": c["claim_key"], "category": c["category"], "statement": c["statement"],
               "verdict": c["verdict"] or "not checked yet", "fact_checker": c["reasoning"],
               "evidence": [f"{e['source']}:{e['line']} {e['quote']}" for e in c["evidence"]][:6]} for c in inc["claims"]]
    patch = inc["patches"][-1] if inc["patches"] else None
    sandbox = patch and patch["sandbox"]
    fix = patch and {"type": patch["fix_type"], "why": patch["rationale"], "rollback_to": patch["rollback_to"],
                     "diff": (patch["diff"] or "")[:3500]}
    run = lambda r: r and {"exit_code": r.get("exit_code"), "output": (r.get("output") or "")[-600:]}  # noqa: E731
    test = sandbox and {"passed": sandbox.get("passed"), "reproduced_before_fix": sandbox.get("reproduced"),
                        "before_fix": run(sandbox.get("before")), "after_fix": run(sandbox.get("after")),
                        "error": sandbox.get("error")}
    imp = inc.get("impact") or {}
    record = {
        "incident": f"INC-{inc['id']}", "title": inc["title"], "service": inc["service"], "severity": inc["severity"],
        "status": inc["status"], "opened": inc["created_at"], "resolved": inc["resolved_at"], "assigned_to": inc["assignee"],
        "alert": inc["alert"],
        "possible_causes": claims,
        "suspect_changes": inc["suspects"][:3],
        "fix": fix, "test": test,
        "approval": inc["approval"], "votes": [{k: v[k] for k in ("name", "role", "decision", "reason")} for v in inc["votes"]],
        "escalation": inc["escalation"],
        "impact": {k: imp.get(k) for k in ("service_label", "at_risk_per_minute", "minutes_so_far", "manual_minutes", "saved_value", "currency")} if imp else None,
        "notes": [f"{c['author']}: {c['text']}" for c in inc["comments"]][-5:],
        "report": (inc["report"] or "")[:3000] or None,
    }
    return json.dumps(record, default=str, ensure_ascii=False)


def answer(inc: dict, question: str, llm: LLM | None = None) -> dict:
    llm = llm or LLM(temperature=0.1)
    reply = llm.chat([{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": f"Incident record:\n{_record(inc)}\n\nQuestion: {question[:MAX_QUESTION]}"}])
    return {"answer": reply["content"].strip() or "I couldn't come up with an answer from this incident's record.",
            "provider": reply["provider"], "model": reply["model"], "tokens": reply.get("tokens", 0)}
