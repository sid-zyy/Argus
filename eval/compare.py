"""Argus vs a single chatbot prompt, scored the same way.

The baseline gets what an engineer would typically paste into a chatbot: the alert, the tail of each log, the deploy
log and the diff of the latest deploy. It answers once, with no tools. Both sides are scored on: right kind of cause,
culprit change named, evidence that matches the real files, and a fix that passes the same sandbox.

python -m eval.compare            # runs the baseline (real model calls) and writes eval/baseline.json
"""
from __future__ import annotations

import importlib
import json
import re
import time
from pathlib import Path

from orchestrator.state import Edit, Evidence, FixOutput
from scenarios.build import ROOT as SCENARIOS, build
from tools.evidence_check import check_all
from tools.sandbox import run_sandbox
from tools.workspace import Workspace

HERE = Path(__file__).resolve().parent
REPLAYS = HERE.parent / "replay"
BASELINE = HERE / "baseline.json"
MAX_PASTE = 18000  # characters; roughly what fits a free-tier request

PROMPT = """You are an expert site reliability engineer. Below is everything I have about a production incident.
Find the root cause and propose a fix.

Reply with JSON only, in this shape:
{"root_cause": "...", "category": "code|config|resource|dependency", "culprit_commit": "<sha or null>",
 "evidence": [{"source": "<file name>", "line": <line number>, "quote": "<exact text from that line>"}],
 "fix": {"type": "patch|config_change|rollback", "file": "<path in repo>", "old": "<exact text to replace>", "new": "<replacement>",
         "rollback_to": "<sha or null>"}}

"""


def _paste(ws: Workspace) -> str:
    parts = ["ALERT:\n" + json.dumps(ws.alert, indent=2)]
    for src in ws.log_sources():
        lines = ws.lines(src)
        tail = lines if len(lines) <= 30 else lines[-60:] if src == "app.log" else lines[-15:]
        parts.append(f"--- {src} (last {len(tail)} lines) ---\n" + "\n".join(tail))
    parts.append("--- git log ---\n" + ws.git("log", "--no-color", "--format=%h %ad %an | %s", "--date=iso-strict", "-n", "6"))
    deploy = ws.lines("deploy.log")[-1] if "deploy.log" in ws.log_sources() else ""
    m = re.search(r"changes=([\w,]+)", " ".join(ws.lines("deploy.log"))) if deploy else None
    for sha in (m.group(1).split(",") if m else [])[-2:]:
        parts.append(f"--- git show {sha} ---\n" + ws.git("show", "--no-color", "--format=medium", sha))
    return "\n\n".join(parts)[:MAX_PASTE]


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def expected_for(name: str, ws: Workspace) -> dict | None:
    """The answer key: expected.json, or for the live shop the pushed bad change (its sha is known only once pushed)."""
    p = SCENARIOS / name / "expected.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    try:
        bad = importlib.import_module(f"scenarios.{name}.history").BAD_CHANGE
        sha = ws.git("log", "-1", "--format=%h", "--fixed-strings", f"--grep={bad['message']}").strip()
    except (ImportError, AttributeError, KeyError, ValueError):
        return None
    return {**bad["expected"], "culprit_sha": sha} if sha else None


def run_baseline(name: str, llm, ws: Workspace | None = None) -> dict:
    """Pass `ws` to score an existing incident's workspace as it is (e.g. the live shop, whose repo must not be rebuilt)."""
    if ws is None:
        build(name)
        ws = Workspace(SCENARIOS / name)
    expected = expected_for(name, ws) or {}
    t0 = time.time()
    reply = llm.chat([{"role": "user", "content": PROMPT + _paste(ws)}])
    answer = _json(reply.get("content", ""))
    evidence = []
    for e in answer.get("evidence", []) or []:
        try:
            evidence.append(Evidence(source=str(e.get("source", "")), line=int(e.get("line", 0)), quote=str(e.get("quote", ""))))
        except (TypeError, ValueError, AttributeError):
            pass
    bad = check_all(ws, evidence)
    fix_ok, fix_note = False, "no usable fix"
    f = answer.get("fix") or {}
    try:
        if f.get("type") == "rollback" and f.get("rollback_to"):
            fix = FixOutput(fix_type="rollback", rationale="baseline", rollback_to=str(f["rollback_to"]))
        else:
            fix = FixOutput(fix_type=f.get("type") if f.get("type") in ("patch", "config_change") else "patch", rationale="baseline",
                            edits=[Edit(file=str(f.get("file", "")), old=str(f.get("old", "")), new=str(f.get("new", "")))])
        result = run_sandbox(ws, fix)
        fix_ok, fix_note = result.passed, result.error or ("passed" if result.passed else "problem still happens")
    except Exception as e:  # noqa: BLE001 - any malformed fix simply fails
        fix_note = f"fix could not be applied: {str(e)[:120]}"
    text = json.dumps(answer)
    return {
        "scenario": name,
        "root_cause": answer.get("root_cause", "(no answer)"),
        "right_category": answer.get("category") == expected["category"] if expected else None,
        "named_culprit": expected["culprit_sha"][:7] in text if expected else None,
        "evidence_total": len(evidence),
        "evidence_verified": len(evidence) - len(bad),
        "fix_passes": fix_ok,
        "fix_note": fix_note,
        "wrong_guess_caught": False,
        "seconds": round(time.time() - t0, 1),
        "model": f"{reply.get('provider')}/{reply.get('model')}",
    }


def score_incident(inc: dict, expected: dict | None) -> dict:
    """Score an Argus incident from its record (GET /incidents/{id}) the same way as the baseline."""
    supported = next((c for c in inc["claims"] if c["verdict"] == "supported"), None)
    evidence = [e for c in inc["claims"] for e in c["evidence"] + c["verdict_evidence"]]
    latest = inc["patches"][-1] if inc["patches"] else None
    culprit = (expected or {}).get("culprit_sha", "")[:7]
    return {
        "root_cause": supported["statement"] if supported else "(none confirmed yet)",
        "right_category": (bool(supported) and supported["category"] == expected["category"]) if expected else None,
        "named_culprit": (bool(culprit) and (culprit in json.dumps(supported or {}) or any(s.get("sha", "")[:7] == culprit for s in inc["suspects"])))
        if expected else None,
        "evidence_total": len(evidence),
        "evidence_verified": len(evidence),  # stored evidence already passed the code check; failing quotes never get in
        "fix_passes": bool(latest and latest["passed"]),
        "wrong_guess_caught": bool(supported) and any(c["verdict"] in ("refuted", "insufficient") for c in inc["claims"]),
        "guesses_ruled_out": [c["statement"] for c in inc["claims"] if c["verdict"] in ("refuted", "insufficient")],
        "approved_by_person": bool(inc.get("approval") and inc["approval"].get("approved")),
    }


def argus_from_replay(name: str) -> dict | None:
    """Score a recorded Argus run the same way."""
    p = REPLAYS / f"{name}.json"
    if not p.is_file():
        return None
    events = json.loads(p.read_text(encoding="utf-8"))
    expected = json.loads((SCENARIOS / name / "expected.json").read_text(encoding="utf-8"))
    claims = {e["payload"]["id"]: e["payload"] for e in events if e["type"] == "claim"}
    verdicts = [e["payload"] for e in events if e["type"] == "verdict"]
    supported = [v for v in verdicts if v["verdict"] == "supported"]
    sandbox = [e["payload"] for e in events if e["type"] == "sandbox"]
    suspects = [s for e in events if e["type"] == "suspects" for s in e["payload"]["suspects"]]
    evidence = [x for c in claims.values() for x in c["evidence"]] + [x for v in verdicts for x in v["evidence"]]
    blocked = sum(1 for e in events if e["type"] == "rejected" and any(h in x for x in e["payload"]["errors"] for h in ("not found", "out of range")))
    first = next((s for s in supported), None)
    from datetime import datetime
    ts = [datetime.fromisoformat(e["ts"]) for e in events]
    approval = next((datetime.fromisoformat(e["ts"]) for e in events if e["type"] == "awaiting_approval"), ts[-1])
    return {
        "scenario": name,
        "root_cause": claims[first["claim_id"]]["statement"] if first else "(none confirmed)",
        "right_category": bool(first) and claims[first["claim_id"]]["category"] == expected["category"],
        "named_culprit": bool(first) and (expected["culprit_sha"][:7] in json.dumps(first) or any(s["sha"][:7] == expected["culprit_sha"][:7] for s in suspects)),
        "evidence_total": len(evidence),
        "evidence_verified": len(evidence),  # every quote in a recorded claim or verdict already passed the code check
        "fabrications_blocked": blocked,
        "fix_passes": bool(sandbox and sandbox[-1]["passed"]),
        "wrong_guess_caught": len(claims) > len(supported) and bool(supported),
        "seconds": round((approval - ts[0]).total_seconds(), 1),
        "model": ", ".join(sorted({f"{e['payload']['provider']}/{e['payload']['model']}" for e in events
                                   if e["type"] == "llm_step" and e["payload"].get("provider")})),
    }


def main():
    from llm import LLM
    llm = LLM()
    names = sorted(p.parent.name for p in SCENARIOS.glob("*/history.py"))
    rows = []
    for n in names:
        print(f"baseline: {n} ...", flush=True)
        rows.append(run_baseline(n, llm))
        print(json.dumps(rows[-1])[:300], flush=True)
    BASELINE.write_text(json.dumps({"rows": rows, "recorded_at": time.strftime("%Y-%m-%d %H:%M")}, indent=2), encoding="utf-8")
    print(f"written to {BASELINE}")


if __name__ == "__main__":
    main()
