"""LLM access shared by the coder and debugger: lazy construction (no import-time side effects) and rate-limit backoff."""
import random
import re
import time
from typing import Any

from langchain_core.messages import AIMessage

import config
from core import execution_manager as em

_override: Any = None  # tests inject a fake chat model here
_cache: dict[str, Any] = {}


def set_llm(llm: Any) -> None:
    global _override
    _override = llm


def get_llm(model_name: str):
    """Factory function to initialize the correct LLM provider."""
    if any(m in model_name.lower() for m in ["llama", "mixtral", "gemma"]):
        from langchain_groq import ChatGroq
        return ChatGroq(model=model_name, temperature=config.TEMPERATURE)
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(model=model_name, temperature=config.TEMPERATURE)


def model_for(role: str) -> str:
    if _override is not None:
        return "fake"
    return config.model_name("coder")  # the debugger uses the coder's model


def llm_with_tools(role: str):
    if _override is not None:
        return _override
    name = model_for(role)
    if name not in _cache:
        _cache[name] = get_llm(name).bind_tools(em.TOOL_SCHEMAS)
    return _cache[name]


def _short(text: Any, n: int = 200) -> str:
    text = text if isinstance(text, str) else str(text)
    return text if len(text) <= n else text[:n] + "..."


def is_rate_limit(error_text: str) -> bool:
    return "429" in error_text or "RESOURCE_EXHAUSTED" in error_text


def backoff_delay(attempt: int, error_text: str) -> float:
    """Seconds to wait after a rate limit: server's retryDelay if present, else exponential; plus jitter."""
    m = re.search(r"retryDelay['\": ]+(\d+(?:\.\d+)?)s", error_text)
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
                on_log(role=role, prompt_version=prompt_version, model=model, result="ok")
            return response, None
        except config.ConfigError as e:
            return None, f"LLM_CONFIG: {e}"
        except Exception as e:
            err = str(e)
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
