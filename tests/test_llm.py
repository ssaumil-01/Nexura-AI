"""LLM access: every model client has a timeout, and transient errors (rate limit, overload, timeouts) are retried with backoff."""
import time

import pytest

import agents.llm as llm
import config
from tests.conftest import FakeChatModel, tool_call_msg


def test_google_and_groq_clients_get_an_explicit_timeout(monkeypatch):
    """The library default is no timeout plus 6 internal retries: a stalled connection hung a whole eval run."""
    seen = {}

    class Rec:
        def __init__(self, **kw):
            seen.update(kw)
    import langchain_google_genai
    import langchain_groq
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", Rec)
    monkeypatch.setattr(langchain_groq, "ChatGroq", Rec)
    llm.get_llm("gemini-test")
    assert seen["timeout"] == config.LLM_TIMEOUT_S and seen["max_retries"] <= 2
    seen.clear()
    llm.get_llm("llama-3-test")
    assert seen["request_timeout"] == config.LLM_TIMEOUT_S


@pytest.mark.parametrize("text,transient", [
    ("503 UNAVAILABLE. This model is currently experiencing high demand.", True), ("Request timed out.", True),
    ("429 RESOURCE_EXHAUSTED", True), ("httpx.ReadError: connection closed", True), ("ConnectionError: reset", True),
    ("401 API key not valid", False), ("400 invalid argument", False)])
def test_transient_classification(text, transient):
    assert llm.is_rate_limit(text) is transient


def test_invoke_retries_a_timeout_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    fake = FakeChatModel(script=[TimeoutError("Request timed out."), tool_call_msg("task_complete", {"summary": "x"})], calls=[])
    monkeypatch.setattr(llm, "_override", fake)
    resp, failure = llm.invoke("coder", ["hi"], "1")
    assert failure is None and resp.tool_calls[0]["name"] == "task_complete" and len(sleeps) == 1


def test_invoke_reports_persistent_overload_and_hard_errors_differently(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "_override", FakeChatModel(script=[Exception("503 UNAVAILABLE")] * 3, calls=[]))
    assert llm.invoke("coder", ["hi"], "1")[1].startswith("LLM_UNAVAILABLE")
    monkeypatch.setattr(llm, "_override", FakeChatModel(script=[Exception("401 API key not valid")], calls=[]))
    assert llm.invoke("coder", ["hi"], "1")[1].startswith("LLM_ERROR")
