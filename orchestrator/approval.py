"""Human approval gate. Nothing is marked applied without a human decision.

mode 'ask' prompts in the terminal. 'yes'/'no' exist only for unattended eval runs and are
recorded in the trace as an automated decision, not a human one.
"""
from __future__ import annotations

from typing import Callable

from rich.prompt import Confirm, Prompt

Approver = Callable[[dict], dict]


def make_approver(mode: str = "ask") -> Approver:
    def approve(context: dict) -> dict:
        if mode == "yes":
            return {"approved": True, "approver": "eval-harness", "role": "automated", "reason": "auto-approved for eval run"}
        if mode == "no":
            return {"approved": False, "approver": "eval-harness", "role": "automated", "reason": "auto-rejected for eval run"}
        ok = Confirm.ask(f"Approve the {context['fix_type']} for {context['claim_id']}? (simulated apply)")
        reason = Prompt.ask("Reason (optional)", default="") if ok else Prompt.ask("Why reject?")
        return {"approved": ok, "approver": "on-call engineer", "role": "developer", "reason": reason}

    return approve
