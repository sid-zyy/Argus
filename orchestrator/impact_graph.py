"""Impact graph: what an incident touches beyond the alerting service, and what each way out would cost.

From the service map (scenarios/service_maps/<service>.json) and the incident's record it works out:
- which parts are failing, and whether that is OBSERVED (the confirmed evidence mentions them) or INFERRED (they depend
  on something that is failing, according to the map);
- when each part would fail if nothing is done (the map's cascade delays);
- the blast radius of the proposed fix and of rolling back the suspect change (which parts the change touches, and
  everything downstream of them);
- the estimated cost of fixing, rolling back, or waiting for a manual fix.
Every number is an estimate built from the scenario's business assumptions, and the response says so.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from db.business import assumptions

ROOT = Path(__file__).resolve().parent.parent
MAPS = ROOT / "scenarios" / "service_maps"
APPROVAL_MINUTES = 5  # typical wait for a person to review and approve
DEPLOY_MINUTES = 1


@lru_cache(maxsize=8)
def service_map(service: str) -> dict | None:
    p = MAPS / f"{service}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _texts(inc: dict) -> tuple[list[str], str]:
    """The evidence text the observed links are judged on: confirmed causes if there are any, else everything so far."""
    a = inc["alert"]
    alert = [f"{a.get('title', '')} {a.get('metric') or ''}"]
    confirmed = [c for c in inc["claims"] if c["verdict"] == "supported"]
    claims = confirmed or [c for c in inc["claims"] if c["verdict"] != "refuted"]
    quotes = [e["quote"] for c in claims for e in c["evidence"] + c["verdict_evidence"]]
    quotes += [e.get("quote", "") for s in inc["suspects"][:1] for e in s.get("evidence", [])]
    return alert + quotes, "confirmed evidence" if confirmed else "evidence so far"


def _owner(nodes: list[dict], path: str) -> str | None:
    path = path.removeprefix("repo/").removeprefix("a/").removeprefix("b/")
    return next((n["id"] for n in nodes if path in n.get("code", [])), None)


def _diff_stats(diff: str) -> tuple[list[str], int]:
    files = re.findall(r"^\+\+\+ b/(\S+)", diff or "", flags=re.M) or re.findall(r"^diff --git a/\S+ b/(\S+)", diff or "", flags=re.M)
    lines = sum(1 for l in (diff or "").splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))
    return sorted(set(files)), lines


def _downstream(edges: list[list[str]], start: set[str]) -> set[str]:
    seen, todo = set(start), list(start)
    while todo:
        n = todo.pop()
        for a, b in edges:
            if a == n and b not in seen:
                seen.add(b)
                todo.append(b)
    return seen


def _blast(m: dict, files: list[str], lines: int, tested: bool) -> dict:
    touched = {o for o in (_owner(m["nodes"], f) for f in files) if o}
    reach = _downstream(m["edges"], touched)
    total = len(m["nodes"])
    score = 100 * (0.55 * len(reach) / total + 0.3 * min(lines, 60) / 60 + 0.15 * min(len(files), 4) / 4)
    if tested:
        score *= 0.6  # it already passed the reproduction and the tests in the sandbox
    score = round(min(100, score))
    return {"score": score, "level": "Low" if score < 34 else "Medium" if score < 67 else "High",
            "files": files, "lines_changed": lines, "touches": sorted(touched), "reaches": sorted(reach - touched), "tested": tested}


def analyse(inc: dict, workspace=None) -> dict | None:
    m = service_map(inc["service"])
    if not m:
        return None
    nodes = {n["id"]: n for n in m["nodes"]}
    texts, basis = _texts(inc)
    observed = {nid for nid, n in nodes.items() if any(re.search(s, t, re.I) for s in n.get("signals", []) for t in texts)}

    patch = inc["patches"][-1] if inc["patches"] else None
    fix_files, fix_lines = _diff_stats(patch["diff"]) if patch else ([], 0)
    origin = {o for o in (_owner(m["nodes"], f) for f in fix_files) if o} if patch and patch["passed"] else set()

    # when each part fails if nothing is done: the seeds now, then along the edges with each part's own delay
    fail_at: dict[str, float] = {n: 0.0 for n in observed | origin}
    changed = True
    while changed:
        changed = False
        for a, b in m["edges"]:
            if a in fail_at:
                t = fail_at[a] + nodes[b].get("cascade_min", 0)
                if t < fail_at.get(b, float("inf")):
                    fail_at[b] = t
                    changed = True

    imp = inc.get("impact") or {}
    out_nodes = []
    for nid, n in nodes.items():
        status = "observed" if nid in observed else "inferred" if nid in fail_at else "healthy"
        value = None
        if n.get("metric") == "at_risk" and imp:
            value = f"₹{imp['at_risk_per_minute']:,}/min at risk"
        elif n.get("metric") == "affected" and imp:
            value = f"{imp['affected_per_minute']:g} {imp['unit']}/min failing"
        out_nodes.append({"id": nid, "label": n["label"], "kind": n["kind"], "col": n["col"], "note": n.get("note", ""),
                          "status": status, "origin": nid in origin, "fails_at_min": fail_at.get(nid), "value": value,
                          "code": n.get("code", [])})
    out_edges = [{"from": a, "to": b, "active": a in fail_at and b in fail_at,
                  "observed": a in observed and b in observed} for a, b in m["edges"]]

    # ways out, and what each costs
    b = assumptions(inc["scenario"]) or {}
    per_min = imp.get("at_risk_per_minute", 0)
    fixed = inc["status"] in ("resolved", "closed", "awaiting_qa")
    hit_by = lambda minutes: [nodes[n]["label"] for n, t in fail_at.items() if 0 < t <= minutes]  # noqa: E731
    options = []
    if patch:
        mins = APPROVAL_MINUTES + DEPLOY_MINUTES
        options.append({"key": "fix", "title": "Apply the tested fix", "minutes": mins, "loss": round(per_min * mins),
                        "blast": _blast(m, fix_files, fix_lines, bool(patch["passed"])), "spreads_to": hit_by(mins),
                        "note": "Smallest change that passed the reproduction and the tests." if patch["passed"] else "Did not pass the sandbox."})
    suspect = inc["suspects"][0] if inc["suspects"] else None
    if suspect and workspace is not None:
        try:
            stat = workspace.git("show", "--numstat", "--format=", suspect["sha"])
            rows = [r.split("\t") for r in stat.splitlines() if r.count("\t") == 2]
            files = [r[2] for r in rows]
            lines = sum(int(r[0]) + int(r[1]) for r in rows if r[0].isdigit() and r[1].isdigit())
            subject = workspace.git("log", "-1", "--format=%s", suspect["sha"]).strip()
            mins = APPROVAL_MINUTES + DEPLOY_MINUTES
            options.append({"key": "rollback", "title": f"Roll back {suspect['sha'][:7]}", "minutes": mins, "loss": round(per_min * mins),
                            "blast": _blast(m, files, lines, False), "spreads_to": hit_by(mins),
                            "note": f"Undoes the whole change “{subject}”, not just the bug. Not tested in the sandbox."})
        except (ValueError, RuntimeError, OSError):
            pass  # the commit is gone (the live shop was reset)
    manual = b.get("manual_mttr_minutes")
    if manual:
        options.append({"key": "wait", "title": "Wait for a manual fix", "minutes": manual, "loss": round(per_min * manual),
                        "blast": None, "spreads_to": hit_by(manual),
                        "note": f"About {manual} minutes for engineers to find and fix it by hand, by the scenario's assumptions."})
    if options:
        best = min(options, key=lambda o: (o["loss"], (o["blast"] or {}).get("score", 100)))
        best["recommended"] = True

    # the critical path: from where the fault starts, along failing links, to the money
    failing_now = {n for n, t in fail_at.items() if t == 0}
    starts = [n for n in (origin or failing_now) if not any(b2 == n and a2 in failing_now for a2, b2 in m["edges"])] or sorted(origin or failing_now)
    best: list[str] = []
    for s0 in starts:
        stack = [[s0]]
        while stack:
            path = stack.pop()
            nxt = [b2 for a2, b2 in m["edges"] if a2 == path[-1] and b2 in failing_now and b2 not in path]
            for b2 in nxt:
                stack.append(path + [b2])
            if not nxt:
                ends_in_money = nodes[path[-1]].get("metric") == "at_risk"
                if (ends_in_money, len(path)) > (bool(best) and nodes[best[-1]].get("metric") == "at_risk", len(best)):
                    best = path

    # how sure Argus is about this picture, and why
    reasons, conf = [], 40
    if any(c["verdict"] == "supported" for c in inc["claims"]):
        conf += 25; reasons.append("A cause was confirmed by the fact checker")
    if any(c["verdict"] == "refuted" for c in inc["claims"]):
        conf += 5; reasons.append("A wrong guess was ruled out")
    if inc["suspects"]:
        conf += 10; reasons.append("The change that caused it was found")
    if patch and patch["passed"]:
        conf += 15; reasons.append("The fix reproduced the bug and then passed in the sandbox")
    if len(observed) >= 2:
        conf += 5; reasons.append(f"{len(observed)} parts are seen failing in the evidence")

    unit_total = round((imp.get("affected_per_minute") or 0) * (imp.get("minutes_so_far") or 0))
    business_hit = [n for n in nodes.values() if n["kind"] == "business" and n["id"] in failing_now]
    level = "High" if per_min >= 3000 or len(business_hit) >= 3 else "Medium" if per_min >= 500 else "Low"
    layers = {
        "code": {"files": fix_files, "lines": fix_lines, "tested": bool(patch and patch["passed"]),
                 "commit": (inc["suspects"][0]["sha"][:7] if inc["suspects"] else None)},
        "infra": {"observed": len(observed), "inferred": len([n for n in fail_at if n not in observed]),
                  "healthy": len(nodes) - len(fail_at), "total": len(nodes)},
        "business": {"per_minute": per_min, "so_far": imp.get("at_risk_so_far", 0), "affected": unit_total, "unit": imp.get("unit", ""),
                     "manual_minutes": b.get("manual_mttr_minutes")},
    }
    return {"nodes": out_nodes, "edges": out_edges, "basis": basis, "options": options, "fixed": fixed,
            "critical_path": best, "confidence": {"value": min(conf, 99), "reasons": reasons},
            "owner": {"team": m.get("team", "On-call team"), "person": inc.get("assignee")},
            "potential_impact": {"level": level, "business_parts": [n["label"] for n in business_hit]},
            "layers": layers,
            "minutes_so_far": imp.get("minutes_so_far", 0), "at_risk_per_minute": per_min, "currency": imp.get("currency", "INR"),
            "assumptions": [f"Service map: {m['about']}", f"Approval about {APPROVAL_MINUTES} min, deploy about {DEPLOY_MINUTES} min.",
                            *imp.get("assumptions", [])]}
