"""The governance policy (policy.yaml): approvals, what a fix may touch, the AI budget, the approval SLA.

The file is re-read when it changes. Every decision made under it carries `ref()`, the rule and the file's fingerprint,
so the audit log shows which version of the rules applied.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import threading
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {
    "version": 1,
    "approvals": {"two_person": {"enabled": False, "severities": ["sev1", "sev2"], "lead": True}},
    "separation_of_duties": True,
    "fixes": {"allowed_types": ["patch", "config_change", "rollback"], "protected_paths": [], "max_changed_lines": 0},
    "ai": {"budget_per_incident": 0, "cost_per_1k_tokens": 0.05},
    "approval_sla_minutes": 10,
}
_cache: dict = {}
_lock = threading.Lock()


def path() -> Path:
    return Path(os.getenv("ARGUS_POLICY", ROOT / "policy.yaml"))


def load() -> dict:
    """The policy as a dict, plus 'sha' (first 12 hex of the file's sha256) and 'path'. Missing file: safe defaults."""
    p = path()
    try:
        stamp = (str(p), p.stat().st_mtime_ns)
    except OSError:
        stamp = (str(p), None)
    with _lock:
        if _cache.get("stamp") == stamp:
            return _cache["policy"]
        raw = p.read_bytes() if stamp[1] is not None else b""
        data = yaml.safe_load(raw) or {} if raw else {}
        pol = _merge(DEFAULTS, data)
        pol["sha"] = hashlib.sha256(raw).hexdigest()[:12] if raw else "defaults"
        pol["path"] = p.name if raw else "(built-in defaults)"
        _cache.update(stamp=stamp, policy=pol)
        return pol


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def ref(rule: str) -> dict:
    p = load()
    return {"rule": rule, "policy_sha": p["sha"], "policy_file": p["path"]}


def _two_person_on(p: dict) -> bool:
    env = os.getenv("TWO_PERSON_APPROVAL")
    if env is not None and env.strip():
        return env.strip().lower() in ("1", "true", "yes")
    return bool(p["approvals"]["two_person"].get("enabled"))


def approval_rule(severity: str) -> dict:
    """How many people must approve a fix for this severity, and whether one must be a lead."""
    p = load()
    two = p["approvals"]["two_person"]
    if _two_person_on(p) and severity in two.get("severities", []):
        lead = bool(two.get("lead", True))
        return {"count": 2, "lead": lead, "label": "Developer + Lead (two people)" if lead else "Two people",
                **ref("approvals.two_person")}
    n = int((p["approvals"].get(severity) or {}).get("people", 1))
    return {"count": n, "lead": False, "label": "One developer or lead" if n == 1 else f"{n} people", **ref(f"approvals.{severity}")}


def separation_of_duties() -> bool:
    return bool(load().get("separation_of_duties", True))


def sla_minutes() -> float:
    env = os.getenv("APPROVAL_SLA_MINUTES")
    return float(env) if env not in (None, "") else float(load().get("approval_sla_minutes") or 0)


def cost_per_1k_tokens() -> float:
    return float(load()["ai"].get("cost_per_1k_tokens") or 0)


def ai_cost(tokens: int) -> float:
    return round(tokens / 1000 * cost_per_1k_tokens(), 2)


def ai_budget() -> float:
    return float(load()["ai"].get("budget_per_incident") or 0)


def changed_files(diff: str) -> list[str]:
    files = []
    for m in re.finditer(r"^(?:\+\+\+|---) (?:[ab]/)?(\S+)", diff or "", re.MULTILINE):
        f = m.group(1)
        if f != "/dev/null" and f not in files:
            files.append(f)
    return files


def changed_lines(diff: str) -> int:
    return sum(1 for line in (diff or "").splitlines()
               if line[:1] in "+-" and not line.startswith(("+++", "---")))


def check_fix(fix_type: str, diff: str) -> list[dict]:
    """Ways a tested fix breaks the policy, each with the rule it breaks. Empty means it may go to approval."""
    f = load()["fixes"]
    out = []
    if fix_type not in f.get("allowed_types", []):
        out.append({"rule": "fixes.allowed_types", "reason": f"a {fix_type} is not an allowed kind of fix ({', '.join(f['allowed_types'])})"})
    for name in changed_files(diff):
        pattern = next((g for g in f.get("protected_paths", []) if fnmatch.fnmatch(name, g) or fnmatch.fnmatch("/" + name, g)
                        or fnmatch.fnmatch(name.split("/")[-1], g)), None)
        if pattern:
            out.append({"rule": "fixes.protected_paths", "reason": f"it changes {name}, which matches the protected path {pattern}"})
    limit = int(f.get("max_changed_lines") or 0)
    if limit and (n := changed_lines(diff)) > limit:
        out.append({"rule": "fixes.max_changed_lines", "reason": f"it changes {n} lines; the limit is {limit}"})
    return out


def summary() -> dict:
    """The policy in plain words, for the UI."""
    p = load()
    rules = [{"rule": f"approvals.{s}", "text": f"{s}: {approval_rule(s)['label']}"} for s in ("sev1", "sev2", "sev3", "sev4")]
    if p["separation_of_duties"]:
        rules.append({"rule": "separation_of_duties", "text": "When two people are needed, the reporter can't be one of them"})
    f = p["fixes"]
    rules.append({"rule": "fixes.allowed_types", "text": f"Allowed fixes: {', '.join(f['allowed_types'])}"})
    if f.get("protected_paths"):
        rules.append({"rule": "fixes.protected_paths", "text": f"Never changes: {', '.join(f['protected_paths'])}"})
    if f.get("max_changed_lines"):
        rules.append({"rule": "fixes.max_changed_lines", "text": f"At most {f['max_changed_lines']} changed lines per fix"})
    if ai_budget():
        rules.append({"rule": "ai.budget_per_incident", "text": f"AI budget per run: {ai_budget():g} (at {cost_per_1k_tokens():g} per 1,000 tokens)"})
    if sla_minutes():
        rules.append({"rule": "approval_sla_minutes", "text": f"Lead is pinged after {sla_minutes():g} minutes waiting for approval"})
    return {"sha": p["sha"], "file": p["path"], "version": p.get("version"), "two_person": _two_person_on(p), "rules": rules}
