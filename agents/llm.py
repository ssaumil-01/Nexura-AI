"""LLM access shared by the coder and debugger: lazy construction (no import-time side effects) and rate-limit backoff."""
import random
import re
import time
from typing import Any

from langchain_core.messages import AIMessage

import config
from core import execution_manager as em

_override: Any = None  # tests inject a fake chat model here
_cache: dict[tuple, Any] = {}


def set_llm(llm: Any) -> None:
    global _override
    _override = llm


def get_llm(model_name: str):
    """Factory function to initialize the correct LLM provider."""
    if any(m in model_name.lower() for m in ["llama", "mixtral", "gemma"]):
        from langchain_groq import ChatGroq
        return ChatGroq(model=model_name, temperature=config.TEMPERATURE, request_timeout=config.LLM_TIMEOUT_S, max_retries=1)
    from langchain_google_genai import ChatGoogleGenerativeAI
    # the library defaults are NO timeout and 6 internal retries; we apply our own bounded backoff instead
    return ChatGoogleGenerativeAI(model=model_name, temperature=config.TEMPERATURE, timeout=config.LLM_TIMEOUT_S, max_retries=1)


def model_for(role: str) -> str:
    if _override is not None:
        return "fake"
    return config.model_name("coder")  # the debugger uses the coder's model


def llm_with_tools(role: str):
    if _override is not None:
        return _override
    name = model_for(role)
    key = (name, config.PATCH_MODE)
    if key not in _cache:
        _cache[key] = get_llm(name).bind_tools(em.tool_schemas())
    return _cache[key]


def _short(text: Any, n: int = 200) -> str:
    text = text if isinstance(text, str) else str(text)
    return text if len(text) <= n else text[:n] + "..."


# Errors worth retrying with backoff: rate limits, server overload (5xx / "high demand"), timeouts
TRANSIENT = re.compile(r"\b(429|500|502|503|504)\b|UNAVAILABLE|RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|timed? ?out|overloaded|"
                       r"ConnectionError|ReadError|RemoteProtocolError", re.IGNORECASE)


QUOTA = re.compile(r"PerDay|per day", re.IGNORECASE)    # per-MINUTE limits ("tokens per minute ... exceeded on Free Tier") are transient
RETRY_DELAY = re.compile(r"(?:retryDelay['\": ]+|retry in )(\d+(?:\.\d+)?)s", re.IGNORECASE)


def quota_message(error_text: str) -> str | None:
    """A DAILY/long quota exhaustion is not transient: waiting a few seconds will not help, so fail fast with a clear message."""
    m = RETRY_DELAY.search(error_text)
    delay = float(m.group(1)) if m else 0.0
    if QUOTA.search(error_text) or delay > 300:
        when = f" (resets in about {delay / 3600:.0f}h)" if delay > 3600 else ""
        return f"LLM_QUOTA: the API quota is exhausted{when}. Check your plan/billing or try again later."
    return None


def is_rate_limit(error_text: str) -> bool:
    return bool(TRANSIENT.search(error_text))


def backoff_delay(attempt: int, error_text: str) -> float:
    """Seconds to wait after a rate limit: server's retryDelay if present, else exponential; plus jitter."""
    m = RETRY_DELAY.search(error_text)
    base = float(m.group(1)) + 2 if m else min(5 * 2 ** attempt, 60)
    return min(base, 120) + random.uniform(0, base * 0.25)


def invoke(role: str, messages: list, prompt_version: str, on_log=None) -> tuple[AIMessage | None, str | None]:
    """Call the model. Returns (response, None) or (None, failure_reason). Rate limits retry with backoff."""
    model = model_for(role)
    detail = ""
    for attempt in range(config.MAX_RETRIES):
        try:
            response = llm_with_tools(role).invoke(messages)
            if on_log:
                calls = [c["name"] for c in (getattr(response, "tool_calls", None) or [])]
                usage = getattr(response, "usage_metadata", None) or {}
                on_log(role=role, prompt_version=prompt_version, model=model, result="ok",
                       input_summary=_short(getattr(messages[-1], "content", ""), 300),
                       output_summary=("tool_calls: " + ", ".join(calls)) if calls else _short(getattr(response, "content", ""), 300),
                       tokens=usage.get("total_tokens"))
            return response, None
        except config.ConfigError as e:
            return None, f"LLM_CONFIG: {e}"
        except Exception as e:
            err = str(e)
            quota = quota_message(err)
            if quota:
                if on_log:
                    on_log(role=role, prompt_version=prompt_version, model=model, result="quota_exhausted")
                return None, quota
            if not is_rate_limit(err):
                if on_log:
                    on_log(role=role, prompt_version=prompt_version, model=model, result="error")
                return None, f"LLM_ERROR: {_short(err, 150)}"
            detail = _short(err, 150)
            if attempt + 1 < config.MAX_RETRIES:
                wait = backoff_delay(attempt, err)
                print(f"  [!] Rate limited (attempt {attempt + 1}/{config.MAX_RETRIES}). Waiting {wait:.1f}s...")
                time.sleep(wait)
    if on_log:
        on_log(role=role, prompt_version=prompt_version, model=model, result="rate_limited")
    return None, f"LLM_UNAVAILABLE: {detail}"
