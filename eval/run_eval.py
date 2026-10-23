"""Score the pipeline across scenarios: did it catch the planted wrong hypothesis and fix the real cause?

python -m eval.run_eval [--provider gemini] [scenario ...]

Uses real model calls (costs free-tier quota). Approval is automated for the eval only and recorded as such.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from llm import LLM
from orchestrator.approval import make_approver
from orchestrator.pipeline import run_incident
from orchestrator.trace import Trace
from scenarios.build import ROOT as SCENARIOS, build
from tools.workspace import Workspace

OUT = Path(__file__).resolve().parent / "results.json"


def score(name: str, provider: str | None) -> dict:
    build(name)
    ws = Workspace(SCENARIOS / name)
    expected = json.loads((ws.root / "expected.json").read_text(encoding="utf-8"))
    trace = Trace()
    t0 = time.time()
    state = run_incident(ws, LLM(provider=provider), trace, make_approver("yes"))
    verdicts = {e["payload"]["claim_id"]: e["payload"]["verdict"] for e in trace.of_type("verdict")}
    claims = [e["payload"] for e in trace.of_type("claim")]
    supported = state.get("supported")
    culprit = expected["culprit_sha"]
    named_culprit = bool(supported) and (
        culprit in json.dumps(supported[0].model_dump()) + json.dumps(supported[1].model_dump())
        or any(s["sha"].startswith(culprit[:7]) or culprit.startswith(s["sha"][:7]) for s in state.get("suspects", [])[:1]))
    sb = state.get("sandbox")
    return {
        "scenario": name,
        "status": state["status"],
        "wrong_claims_refuted": sum(1 for v in verdicts.values() if v == "refuted"),
        "claims": len(claims),
        "right_category": bool(supported) and supported[0].category == expected["category"],
        "named_culprit_commit": named_culprit,
        "sandbox_passed": bool(sb and sb.passed),
        "attempts": state["attempts"],
        "fabrications_blocked": sum(1 for e in trace.of_type("rejected") if any("not found" in x or "out of range" in x for x in e["payload"]["errors"])),
        "seconds": round(time.time() - t0, 1),
        "models": sorted({f"{e['payload']['provider']}/{e['payload']['model']}" for e in trace.of_type("llm_step") if e["payload"].get("provider")}),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*")
    ap.add_argument("--provider")
    args = ap.parse_args(argv)
    names = args.scenarios or sorted(p.parent.name for p in SCENARIOS.glob("*/history.py"))
    rows = []
    for n in names:
        print(f"running {n} ...", flush=True)
        rows.append(score(n, args.provider))
        print(json.dumps(rows[-1]), flush=True)
    caught = sum(1 for r in rows if r["wrong_claims_refuted"] and r["right_category"] and r["sandbox_passed"])
    print(f"\nCaught and fixed: {caught} of {len(rows)} "
          "(a scenario counts only if a wrong claim was refuted, the right category was supported, and the fix passed the sandbox)")
    OUT.write_text(json.dumps({"rows": rows, "caught": caught, "total": len(rows)}, indent=2), encoding="utf-8")
    print(f"written to {OUT}")


if __name__ == "__main__":
    main()
