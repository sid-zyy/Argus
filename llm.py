"""Provider-agnostic chat client: Groq, Gemini, OpenRouter and Ollama through their OpenAI-compatible APIs.

chat(messages, tools=None, provider=None) -> dict(content, tool_calls, provider, model)
Fails over to the next configured provider on rate limits, connection errors and 5xx responses.
"""
from __future__ import annotations

import json
import os
import re
import time

import openai
from dotenv import load_dotenv

load_dotenv()

PROVIDERS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-3.5-flash-lite",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_model": "openai/gpt-oss-120b",
    },
    # free models on OpenRouter (ids end in ':free'); they change often, so several are tried in turn
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "qwen/qwen3-coder:free",
    },
    "ollama": {
        "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "key_env": None,
        "model_env": "OLLAMA_MODEL",
        "default_model": "gemma4:e2b",
    },
}

OPENROUTER_BACKUPS = "meta-llama/llama-3.3-70b-instruct:free,openai/gpt-oss-20b:free"

MAX_RATE_LIMIT_WAIT = 65.0  # seconds; per-minute token windows are worth waiting out, daily quotas are not
COOLDOWN = 20.0  # seconds a failed provider is skipped (longer if it asks for longer)
MAX_TOTAL_WAIT = 180.0  # seconds one call may spend waiting for any provider before giving up


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, provider: str | None = None, fallbacks: list[str] | None = None, temperature: float = 0.2):
        primary = provider or os.getenv("LLM_PROVIDER", "groq")
        if fallbacks is None:
            fallbacks = [p.strip() for p in os.getenv("LLM_FALLBACKS", "gemini,openrouter").split(",") if p.strip()]
        self.order = [primary] + [p for p in fallbacks if p != primary]
        self.temperature = temperature
        self._clients: dict[str, openai.OpenAI] = {}
        # A provider that just failed is skipped for a while, so one run does not bounce between models every step.
        self._cooldown_until: dict[str, float] = {}
        self.on_event = None  # optional callback(type, payload) for live progress: waits and provider switches

    def _notify(self, type_: str, payload: dict) -> None:
        if self.on_event:
            try:
                self.on_event(type_, payload)
            except Exception:
                pass

    def model_for(self, provider: str) -> str:
        cfg = PROVIDERS[provider]
        if provider == self.order[0] and os.getenv("LLM_MODEL"):
            return os.environ["LLM_MODEL"]
        return os.getenv(cfg["model_env"]) or cfg["default_model"]

    def available(self) -> list[str]:
        out = []
        for p in self.order:
            cfg = PROVIDERS.get(p)
            if cfg and (cfg["key_env"] is None or os.getenv(cfg["key_env"])):
                out.append(p)
        return out

    def _client(self, provider: str) -> openai.OpenAI:
        if provider not in self._clients:
            cfg = PROVIDERS[provider]
            key = os.getenv(cfg["key_env"]) if cfg["key_env"] else "ollama"
            self._clients[provider] = openai.OpenAI(base_url=cfg["base_url"], api_key=key, max_retries=0, timeout=90)
        return self._clients[provider]

    def chat(self, messages: list[dict], tools: list[dict] | None = None, provider: str | None = None) -> dict:
        providers = [provider] if provider else self.available()
        if not providers:
            raise LLMError("No LLM provider configured. Set GEMINI_API_KEY or GROQ_API_KEY in .env, or run Ollama.")
        errors = []
        deadline = time.time() + MAX_TOTAL_WAIT
        while True:
            now = time.time()
            ready = [p for p in providers if self._cooldown_until.get(p, 0) <= now]
            for p in ready:
                try:
                    reply = self._call(p, messages, tools)
                    reply["failovers"] = errors
                    return reply
                except LLMError as e:
                    errors.append(f"{p}: {str(e)[:160]}")
                    self._cooldown_until[p] = time.time() + max(COOLDOWN, getattr(e, "retry_after", 0))
                    self._notify("failover", {"provider": p, "reason": _plain_reason(str(e))})
            # Every provider is briefly unavailable: wait for the soonest one, unless that is hours away (a daily quota).
            soonest = min(providers, key=lambda p: self._cooldown_until.get(p, 0))
            wait = max(1.0, self._cooldown_until.get(soonest, 0) - time.time())
            if time.time() + wait > deadline:
                raise LLMError("All providers failed: " + " | ".join(errors[-4:]))
            self._notify("waiting", {"provider": soonest, "seconds": round(wait, 1), "reason": "all_busy"})
            time.sleep(wait)

    def _call(self, provider: str, messages: list[dict], tools: list[dict] | None) -> dict:
        model = self.model_for(provider)
        kwargs = {"model": model, "messages": _for_provider(provider, messages), "temperature": self.temperature}
        if provider == "openrouter":
            # OpenRouter retries the next model in this list when one is rate-limited, down or retired
            backups = [m.strip() for m in os.getenv("OPENROUTER_FALLBACK_MODELS", OPENROUTER_BACKUPS).split(",") if m.strip()]
            kwargs["extra_body"] = {"models": [model] + [m for m in backups if m != model][:2]}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        for attempt in range(2):
            try:
                resp = self._client(provider).chat.completions.create(**kwargs)
                break
            except openai.RateLimitError as e:
                wait = _retry_after(e)
                if attempt == 0 and wait <= MAX_RATE_LIMIT_WAIT:
                    self._notify("waiting", {"provider": provider, "seconds": round(wait, 1), "reason": "rate_limit"})
                    time.sleep(wait)
                    continue
                err = LLMError(f"rate limited (retry-after {wait:.0f}s)")
                err.retry_after = wait
                raise err from e
            except (openai.APIConnectionError, openai.APITimeoutError) as e:
                raise LLMError(f"connection failed: {e}") from e
            except openai.APIStatusError as e:
                if attempt == 0 and e.status_code in (500, 502, 503):  # "high demand" spikes are usually brief
                    self._notify("waiting", {"provider": provider, "seconds": 3, "reason": "busy"})
                    time.sleep(3)
                    continue
                raise LLMError(f"HTTP {e.status_code}: {str(e)[:300]}") from e
        msg = resp.choices[0].message
        tool_calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_json__": tc.function.arguments}
            call = {"id": tc.id, "name": tc.function.name, "arguments": args}
            extra = (tc.model_extra or {}).get("extra_content")
            if extra:  # Gemini 3 thought signature; must be sent back with this call on later turns
                call["extra_content"] = extra
            tool_calls.append(call)
        extra = msg.model_extra or {}
        reasoning = extra.get("reasoning") or extra.get("reasoning_content") or ""
        tokens = getattr(resp.usage, "total_tokens", 0) if getattr(resp, "usage", None) else 0
        return {"content": msg.content or "", "tool_calls": tool_calls, "provider": provider, "model": model,
                "reasoning": reasoning, "tokens": tokens or 0}


def _plain_reason(err: str) -> str:
    if "rate limited" in err or "429" in err:
        return "hit its free-tier limit"
    if "503" in err or "high demand" in err:
        return "is busy right now"
    if "400" in err:
        return "rejected the request"
    return "didn't respond"


SKIP_SIGNATURE = {"google": {"thought_signature": "skip_thought_signature_validator"}}


def _for_provider(provider: str, messages: list[dict]) -> list[dict]:
    """Gemini needs a thought signature on every earlier tool call (a placeholder for calls another provider made);
    other providers reject the unknown field, so it is stripped for them."""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                c = dict(c)
                if provider == "gemini":
                    c.setdefault("extra_content", SKIP_SIGNATURE)
                else:
                    c.pop("extra_content", None)
                calls.append(c)
            m = {**m, "tool_calls": calls}
        out.append(m)
    return out


def _retry_after(e: openai.RateLimitError) -> float:
    try:
        if e.response.headers.get("retry-after"):
            return float(e.response.headers["retry-after"])
        m = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+)s", str(e)) or re.search(r"retry in (\d+(?:\.\d+)?)s", str(e))
        return float(m.group(1)) if m else 5.0
    except (AttributeError, ValueError):
        return 5.0
