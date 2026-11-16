"""Regression tests for V1 plan §3 bugs 1-7 and 12, plus pipeline behaviour (fake LLM, no network)."""
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

import config as app_config
from core.schemas import ProjectContext
from tests.conftest import tool_call_msg

ROOT = Path(__file__).resolve().parent.parent
PLAN = {"project_id": "p", "goal": "g", "steps": [
    {"step_id": 1, "title": "t1", "description": "d1", "dependencies": []},
    {"step_id": 2, "title": "t2", "description": "d2", "dependencies": []},
]}


def initial_state(workspace, interactive=False):
    return {"messages": [], "user_prompt": "x", "workspace_path": str(workspace), "plan": None, "tasks": [],
            "current_task_index": 0, "execution_history": [], "is_interactive": interactive, "plan_feedback": None,
            "tool_calls_count": 0, "task_status": {}, "task_errors": {}, "run_status": None, "run_error": None,
            "task_outcome": None, "task_reason": None, "replan_count": 0}


@pytest.fixture
def g(monkeypatch, fake_llm, workspace):
    monkeypatch.setenv("CODER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    import core.graph as cg
    monkeypatch.setattr(cg, "llm_with_tools", fake_llm)
    monkeypatch.setattr(cg, "planner_node", lambda s: {
        "plan": PLAN, "tasks": PLAN["steps"], "current_task_index": 0, "plan_feedback": None,
        "task_status": {"1": "pending", "2": "pending"}, "task_errors": {}, "run_status": "running"})
    sleeps = []
    monkeypatch.setattr(cg.time, "sleep", lambda s: sleeps.append(s))

    def cfg(thread="t"):
        return {"configurable": {"thread_id": thread, "project": ProjectContext(run_id=thread, root=workspace)},
                "recursion_limit": 200}

    def run(interactive=False, plan_reply="yes"):
        graph = cg.build_graph(interactive=interactive)
        c = cfg()
        graph.invoke(initial_state(workspace, interactive), c)
        graph.invoke(Command(resume=plan_reply), c)
        return graph, c

    run.sleeps = sleeps
    run.cfg = cfg
    run.cg = cg
    return run


def values(run_result):
    graph, c = run_result
    return graph.get_state(c).values


def done(n=1):
    return [tool_call_msg("task_complete", {"summary": "ok"}, f"d{i}") for i in range(n)]


def spin(n):
    return [tool_call_msg("list_directory", {"target_dir": "."}, f"s{i}") for i in range(n)]


# ─── bugs 1 & 2: tool cap ───────────────────────────────────

def test_bug1_tool_cap_triggers_and_fails_task(g, fake_llm):
    fake_llm.script[:] = spin(40) + done(2)
    v = values(g())
    assert len(fake_llm.calls) == app_config.MAX_TOOL_CALLS_PER_TASK
    assert v["task_status"] == {"1": "failed", "2": "skipped"}  # default policy: stop
    assert v["task_errors"]["1"].startswith("TOOL_CAP")
    assert v["run_status"] == "failed"


def test_bug2_forced_advance_is_not_recorded_as_completed(g, fake_llm):
    fake_llm.script[:] = spin(40) + done(2)
    v = values(g())
    outcomes = [h for h in v["execution_history"] if h["tool"] == "task_outcome"]
    assert len(outcomes) == 1 and outcomes[0]["result"].startswith("failed: TOOL_CAP")
    assert all(h["result"] != "completed" for h in v["execution_history"])


def test_failure_with_continue_policy_ends_completed_with_failures(g, fake_llm, monkeypatch):
    monkeypatch.setattr(app_config, "FAILURE_POLICY", "continue")
    fake_llm.script[:] = spin(15) + done(1)
    v = values(g())
    assert v["task_status"] == {"1": "failed", "2": "done"}
    assert v["run_status"] == "completed_with_failures"
    import main
    assert main.print_summary(v) == 1


def test_all_done_is_completed_and_exit_zero(g, fake_llm):
    fake_llm.script[:] = done(2)
    v = values(g())
    assert v["run_status"] == "completed" and v["task_status"] == {"1": "done", "2": "done"}
    import main
    assert main.print_summary(v) == 0


# ─── bug 3: LLM failure ─────────────────────────────────────

def test_bug3_llm_exhaustion_fails_task_and_skips_rest(g, fake_llm):
    fake_llm.script[:] = [Exception("429 RESOURCE_EXHAUSTED")] * 3
    v = values(g())
    assert v["run_status"] == "failed"
    assert v["task_errors"]["1"].startswith("LLM_UNAVAILABLE")
    assert v["task_status"]["2"] == "skipped"
    assert len(g.sleeps) == app_config.MAX_RETRIES - 1  # no sleep after the last attempt


def test_backoff_honors_retry_delay_and_grows():
    import core.graph as cg
    assert 12 <= cg._backoff_delay(0, "... retryDelay': '10s' ...") <= 15
    assert cg._backoff_delay(2, "429") > cg._backoff_delay(0, "429") - 5  # exponential base (jitter aside)


def test_other_llm_error_fails_task_with_reason(g, fake_llm):
    fake_llm.script[:] = [RuntimeError("invalid api key")]
    v = values(g())
    assert v["task_errors"]["1"].startswith("LLM_ERROR") and v["run_status"] == "failed"


def test_prose_only_answers_cannot_loop_forever(g, fake_llm):
    fake_llm.script[:] = [AIMessage(content="thinking...") for _ in range(30)]
    v = values(g())
    assert v["task_errors"]["1"].startswith("NO_TOOL_CALLS")


# ─── bug 4: message clearing ────────────────────────────────

def test_bug4_messages_cleared_between_tasks(g, fake_llm):
    fake_llm.script[:] = [tool_call_msg("list_directory", {"target_dir": "."}, "a")] + done(1) + done(1)
    g()
    assert len(fake_llm.calls[-1]) == 1  # first call of task 2 sees only its own prompt


# ─── bug 5: task_complete inside a batch ────────────────────

def test_bug5_task_complete_in_batch_ignores_later_calls(g, fake_llm):
    both = AIMessage(content="", tool_calls=[
        {"name": "task_complete", "args": {"summary": "x"}, "id": "a"},
        {"name": "list_directory", "args": {"target_dir": "."}, "id": "b"}])
    fake_llm.script[:] = [both, *done(1)]
    v = values(g())
    assert v["task_status"] == {"1": "done", "2": "done"}
    assert not any(h["tool"] == "list_directory" for h in v["execution_history"])


def test_task_complete_after_other_calls_in_batch_is_found(g, fake_llm):
    both = AIMessage(content="", tool_calls=[
        {"name": "list_directory", "args": {"target_dir": "."}, "id": "a"},
        {"name": "task_complete", "args": {"summary": "x"}, "id": "b"}])
    fake_llm.script[:] = [both, *done(1)]
    assert values(g())["task_status"] == {"1": "done", "2": "done"}


# ─── bug 6: execution history ───────────────────────────────

def test_bug6_history_logs_every_call(g, fake_llm, workspace):
    fake_llm.script[:] = [tool_call_msg("create_file", {"target_file": "../x", "content": "1"}, "a"),
                          tool_call_msg("list_directory", {"target_dir": "."}, "b")] + done(2)
    h = values(g())["execution_history"]
    tools = [(e["step_id"], e["tool"], e["status"]) for e in h]
    assert tools[:3] == [(1, "create_file", "error"), (1, "list_directory", "ok"), (1, "task_complete", "ok")]
    assert "args" in h[0] and "result" in h[0]


# ─── bug 7: interactive / HITL ──────────────────────────────

def _interactive(g, monkeypatch, workspace, replies):
    import main
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda *_: next(it))
    graph = g.cg.build_graph(interactive=True)
    c = g.cfg("t7")
    return main.run_graph(graph, initial_state(workspace, True), c)


def test_bug7_interactive_resumes_and_executes(g, fake_llm, monkeypatch, workspace):
    fake_llm.script[:] = [tool_call_msg("create_file", {"target_file": "a.txt", "content": "x"}, "a")] + done(2)
    v = _interactive(g, monkeypatch, workspace, ["yes"] * 10)
    assert (workspace / "a.txt").read_text() == "x"
    assert v["run_status"] == "completed"


def test_interactive_reject_is_fed_back_and_nothing_runs(g, fake_llm, monkeypatch, workspace):
    fake_llm.script[:] = [tool_call_msg("create_file", {"target_file": "a.txt", "content": "x"}, "a")] + done(2)
    # plan ok, reject create_file, approve the rest
    v = _interactive(g, monkeypatch, workspace, ["yes", "no, use b.txt", "yes", "yes", "yes"])
    assert not (workspace / "a.txt").exists()
    assert any(h["status"] == "rejected" for h in v["execution_history"])
    assert "REJECTED by user: no, use b.txt" in str(fake_llm.calls[1][-1].content)


# ─── re-plan cap ────────────────────────────────────────────

def test_replan_cap_stops_run(g, fake_llm):
    graph = g.cg.build_graph()
    c = g.cfg("rp")
    graph.invoke(initial_state(Path(c["configurable"]["project"].root)), c)
    for _ in range(app_config.MAX_REPLANS):
        graph.invoke(Command(resume="please change it"), c)
        assert graph.get_state(c).values["run_status"] == "running"
    graph.invoke(Command(resume="again!"), c)
    v = graph.get_state(c).values
    assert v["run_status"] == "failed" and "Re-plan limit" in v["run_error"]
    assert not graph.get_state(c).next


# ─── bug 12 (fixed in Phase 3 when the coder moves out of graph.py) ──

@pytest.mark.xfail(strict=True, reason="bug 12: core.graph crashes at import without CODER_MODEL_NAME")
def test_bug12_import_without_model_env():
    env = {k: v for k, v in __import__("os").environ.items() if k != "CODER_MODEL_NAME"}
    r = subprocess.run([sys.executable, "-c", "import core.graph"], cwd=ROOT, env=env, capture_output=True)
    assert r.returncode == 0, r.stderr.decode()[-300:]
