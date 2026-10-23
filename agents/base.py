"""The shared agent loop: the model picks tools, our code runs them, caps steps and validates output.

The final answer is a call to the `submit_result` tool whose parameters are the agent's Pydantic
schema. A `check` function (e.g. the evidence checker) can reject a submission and send the errors
back to the model for another try.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from tools.workspace import ToolSet

T = TypeVar("T", bound=BaseModel)
PROMPTS = Path(__file__).parent / "prompts"
MAX_STEPS = 8
MAX_REJECTIONS = 2
REPAIR_STEPS = 2  # extra submit-only steps after a rejected final answer
MAX_TOOL_RESULT = 2500  # characters; free tiers cap tokens per minute

Emit = Callable[[str, str, dict], None]  # (actor, type, payload)


class AgentError(RuntimeError):
    pass


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def inline_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $refs inlined and titles dropped (some providers reject $defs)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            if "anyOf" in node:  # Optional[X] -> X
                options = [o for o in node["anyOf"] if o.get("type") != "null"]
                if len(options) == 1:
                    rest = {k: v for k, v in node.items() if k not in ("anyOf", "default")}
                    return walk({**options[0], **rest})
            return {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
        if isinstance(node, list):
            return [walk(n) for n in node]
        return node

    return walk(schema)


def _json_from_text(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def run_agent(
    name: str,
    system: str,
    task: str,
    tools: ToolSet,
    output_model: type[T],
    llm,
    emit: Emit,
    check: Callable[[T], list[str]] | None = None,
    max_steps: int = MAX_STEPS,
) -> tuple[T, list[str]]:
    """Run one agent to completion. Returns (output, unresolved check errors)."""
    submit = {"type": "function", "function": {
        "name": "submit_result",
        "description": "Submit your final answer. Call this exactly once when you are done.",
        "parameters": inline_schema(output_model),
    }}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    rejections = 0
    last_valid: T | None = None
    last_errors: list[str] = []
    # rate-limit waits and provider switches inside llm.chat() show up live, attributed to this agent
    llm.on_event = lambda type_, payload: emit(name, type_, payload)

    step, limit = 0, max_steps
    while step < limit:
        final_step = step >= max_steps - 1
        if step == max_steps - 1:
            messages.append({"role": "user", "content": "Step limit reached. Call submit_result now with your best answer."})
        emit(name, "thinking", {"step": step + 1, "max_steps": max_steps})
        reply = llm.chat(messages, tools=[submit] if final_step else tools.specs() + [submit])
        thought = (reply.get("reasoning") or reply["content"] or "").strip()
        emit(name, "llm_step", {"step": step + 1, "provider": reply.get("provider"), "model": reply.get("model"),
                                "tools": [c["name"] for c in reply["tool_calls"]], "text": (reply["content"] or "")[:300],
                                "thought": thought[-400:], "failovers": reply.get("failovers") or []})
        step += 1

        calls = reply["tool_calls"]
        if not calls:
            data = _json_from_text(reply["content"])
            messages.append({"role": "assistant", "content": reply["content"] or "(empty)"})
            if data is None:
                messages.append({"role": "user", "content": "Use the tools to investigate, then call submit_result with your final answer."})
                continue
            calls = [{"id": None, "name": "submit_result", "arguments": data}]
        else:
            messages.append({"role": "assistant", "content": reply["content"] or None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                 **({"extra_content": c["extra_content"]} if c.get("extra_content") else {})}
                for c in calls]})

        done = None
        for c in calls:
            if c["name"] == "submit_result":
                result, feedback = _try_submit(c["arguments"], output_model, check)
                if result is not None:
                    last_valid = result
                    last_errors = feedback
                if result is not None and not feedback:
                    done = result
                    feedback_text = "accepted"
                else:
                    rejections += 1
                    emit(name, "rejected", {"errors": feedback})
                    feedback_text = "REJECTED. Fix these problems and call submit_result again:\n- " + "\n- ".join(feedback)
                    if rejections > MAX_REJECTIONS and last_valid is not None:
                        return last_valid, last_errors
                    if step >= max_steps - 1:  # a rejected answer at the end still gets a chance to be repaired
                        limit = min(max_steps + REPAIR_STEPS, max(limit, step + 1))
                if c["id"] is None:
                    messages.append({"role": "user", "content": feedback_text})
                else:
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": feedback_text})
            else:
                out = tools.run(c["name"], c["arguments"])
                emit(name, "tool_call", {"tool": c["name"], "args": c["arguments"], "result_preview": out[:300]})
                if len(out) > MAX_TOOL_RESULT:
                    out = out[:MAX_TOOL_RESULT] + "\n... (truncated)"
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
        if done is not None:
            return done, []

    if last_valid is not None:
        return last_valid, last_errors
    raise AgentError(f"{name} did not return a valid result in {step} steps")


def _try_submit(args: dict, model: type[T], check) -> tuple[T | None, list[str]]:
    try:
        result = model.model_validate(args)
    except ValidationError as e:
        return None, [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:8]]
    errors = check(result) if check else []
    return result, errors
