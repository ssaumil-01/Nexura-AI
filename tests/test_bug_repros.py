"""Failing reproductions for V1 plan §3 bugs 1-7 and 12. xfail(strict) -> remove marker when fixed."""
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

from tests.conftest import tool_call_msg

ROOT = Path(__file__).resolve().parent.parent
PLAN = {"project_id": "p", "goal": "g", "steps": [
    {"step_id": 1, "title": "t1", "description": "d1", "dependencies": []},
    {"step_id": 2, "title": "t2", "description": "d2", "dependencies": []},
]}


@pytest.fixture
def g(monkeypatch, fake_llm, workspace):
    monkeypatch.setenv("CODER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    import core.graph as cg
    import tools.directory_tools as dt
    import tools.file_tools as ft
    import tools.system_tools as st
    for m in (ft, dt, st):
        monkeypatch.setattr(m, "WORKSPACE_PATH", str(workspace))
    monkeypatch.setattr(cg, "llm_with_tools", fake_llm)
    monkeypatch.setattr(cg, "planner_node", lambda s: {
        "plan": PLAN, "tasks": PLAN["steps"], "current_task_index": 0, "plan_feedback": None})
    monkeypatch.setattr(cg.time, "sleep", lambda s: None)

    def run(interactive=False):
        graph = cg.build_graph(interactive=interactive)
        cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 100}
        init = {"messages": [], "user_prompt": "x", "workspace_path": str(workspace), "plan": None,
                "tasks": [], "current_task_index": 0, "execution_history": [],
                "is_interactive": interactive, "plan_feedback": None, "tool_calls_count": 0}
        graph.invoke(init, cfg)
        graph.invoke(Command(resume="yes"), cfg)
        return graph, cfg
    return run


def done(n=1):
    return [tool_call_msg("task_complete", {"summary": "ok"}, f"d{i}") for i in range(n)]


def spin(n):
    return [tool_call_msg("list_directory", {"target_dir": "."}, f"s{i}") for i in range(n)]


@pytest.mark.xfail(strict=True, reason="bug 1: tool-call cap never triggers")
def test_bug1_tool_cap_triggers(g, fake_llm):
    fake_llm.script[:] = spin(40) + done(2)
    g()
    assert len(fake_llm.calls) <= 30  # 15 per task x 2 tasks


@pytest.mark.xfail(strict=True, reason="bug 2: cap-forced advance recorded as completed")
def test_bug2_forced_advance_not_completed(g, fake_llm):
    fake_llm.script[:] = spin(40) + done(2)
    graph, cfg = g()
    hist = graph.get_state(cfg).values["execution_history"]
    assert all(h["result"] != "completed" for h in hist[:1])


@pytest.mark.xfail(strict=True, reason="bug 3: exhausted LLM retries end the run silently")
def test_bug3_llm_exhaustion_is_failure(g, fake_llm):
    fake_llm.script[:] = [Exception("429 RESOURCE_EXHAUSTED")] * 3
    graph, cfg = g()
    assert graph.get_state(cfg).values.get("run_status") == "failed"


@pytest.mark.xfail(strict=True, reason="bug 4: messages=[] does not clear history")
def test_bug4_messages_cleared_between_tasks(g, fake_llm):
    fake_llm.script[:] = [tool_call_msg("list_directory", {"target_dir": "."}, "a")] + done(1) + done(1)
    g()
    assert len(fake_llm.calls[-1]) == 1  # first call of task 2 sees only its own prompt


@pytest.mark.xfail(strict=True, reason="bug 5: only last ToolMessage inspected")
def test_bug5_task_complete_not_last(g, fake_llm):
    both = AIMessage(content="", tool_calls=[
        {"name": "task_complete", "args": {"summary": "x"}, "id": "a"},
        {"name": "list_directory", "args": {"target_dir": "."}, "id": "b"}])
    fake_llm.script[:] = [both, *done(1)]
    g()  # exhausts script (AssertionError) if task 1 doesn't advance after the batch


@pytest.mark.xfail(strict=True, reason="bug 6: execution_history only logs task_complete")
def test_bug6_history_logs_every_call(g, fake_llm):
    fake_llm.script[:] = [tool_call_msg("list_directory", {"target_dir": "."}, "a")] + done(2)
    graph, cfg = g()
    assert any(h["tool"] == "list_directory" for h in graph.get_state(cfg).values["execution_history"])


@pytest.mark.xfail(strict=True, reason="bug 7: interrupt_before=tools + run_graph exits silently")
def test_bug7_interactive_resumes(monkeypatch, g, fake_llm, workspace):
    import main
    import core.graph as cg
    fake_llm.script[:] = [tool_call_msg("create_file", {"target_file": "a.txt", "content": "x"}, "a")] + done(2)
    monkeypatch.setattr("builtins.input", lambda *_: "yes")
    graph = cg.build_graph(interactive=True)
    cfg = {"configurable": {"thread_id": "t7"}, "recursion_limit": 100}
    init = {"messages": [], "user_prompt": "x", "workspace_path": str(workspace), "plan": None, "tasks": [],
            "current_task_index": 0, "execution_history": [], "is_interactive": True,
            "plan_feedback": None, "tool_calls_count": 0}
    main.run_graph(graph, init, cfg)
    assert (workspace / "a.txt").exists()


@pytest.mark.xfail(strict=True, reason="bug 12: core.graph crashes at import without CODER_MODEL_NAME")
def test_bug12_import_without_model_env():
    env = {k: v for k, v in __import__("os").environ.items() if k != "CODER_MODEL_NAME"}
    env["NEXURA_NO_DOTENV"] = "1"
    r = subprocess.run([sys.executable, "-c", "import core.graph"], cwd=ROOT, env=env, capture_output=True)
    assert r.returncode == 0, r.stderr.decode()[-300:]
