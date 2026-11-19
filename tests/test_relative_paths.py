"""V2-1.7 / plan section 7.11: logs, events and tool output use workspace-relative paths only (no absolute workspace root)."""
import json
import os

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import config
from core.state_manager import StateManager
from tests.conftest import tool_call_msg

PLAN = {"project_id": "p", "goal": "g", "steps": [{"step_id": 1, "title": "make bad.py", "description": "d", "dependencies": [],
                                                  "acceptance": [{"type": "file_exists", "path": "bad.py"}]}]}


def variants(root) -> set[str]:
    out = set()
    for p in {str(root), os.path.realpath(root)}:
        for v in (p, p.replace("\\", "/"), p.replace("\\", "\\\\"), p.lower(), p.lower().replace("\\", "\\\\")):
            out.add(v)
    return out


@pytest.fixture
def finished_run(tmp_path, monkeypatch, fake_llm, git_repo):
    import agents.llm as llm_mod
    import agents.planner as planner_mod
    from core.orchestrator import build_graph
    from core.runner import start_run
    monkeypatch.setenv("CODER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("PLANNER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(llm_mod, "_override", fake_llm)
    monkeypatch.setattr(planner_mod.PlannerAgent, "call_llm", lambda self, prompt: json.dumps(PLAN))
    fake_llm.script[:] = [
        tool_call_msg("read_file", {"target_file": "missing.txt"}, "a"),
        tool_call_msg("read_file", {"target_file": "../outside.txt"}, "b"),
        tool_call_msg("create_file", {"target_file": "bad.py", "content": "raise ValueError('boom')\n"}, "c"),
        tool_call_msg("run_command", {"command": "python bad.py"}, "d"),
        tool_call_msg("verify_code_syntax", {"file_path": "bad.py"}, "e"),
        tool_call_msg("task_complete", {"summary": "done"}, "f"),
    ]
    sm = StateManager(tmp_path / "data" / "n.db")
    ctx, state = start_run(sm, "relpaths", git_repo, "make bad.py", approval_level="auto_within_limits")
    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": ctx.run_id, "project": ctx}, "recursion_limit": 200}
    out = graph.invoke(state, cfg)
    while out.get("__interrupt__"):
        out = graph.invoke(Command(resume="yes" if isinstance(out["__interrupt__"][0].value, str) else {"decision": "approve"}), cfg)
    ctx.processes.stop_all()
    return git_repo, sm, ctx, graph.get_state(cfg).values


def test_the_scripted_run_really_exercised_errors_and_tracebacks(finished_run):
    root, sm, ctx, values = finished_run
    tool_text = "\n".join(str(m.content) for m in values["messages"] if m.type == "tool")
    assert "Traceback" in tool_text and "boom" in tool_text      # the crash output (which normally has absolute paths) is there
    assert values["run_status"] == "completed"


def test_no_absolute_workspace_path_in_events_logs_or_tool_output(finished_run):
    root, sm, ctx, values = finished_run
    texts = {"events": json.dumps(sm.list_events(ctx.run_id)),
             "log.jsonl": (ctx.run_dir / "log.jsonl").read_text(encoding="utf-8"),
             "tool output": "\n".join(str(m.content) for m in values["messages"] if m.type == "tool"),
             "history": json.dumps(values.get("execution_history"), default=str)}
    leaks = {name: v for name, t in texts.items() for v in variants(root) if v in t}
    assert not leaks, leaks


@pytest.mark.parametrize("root,text,expected", [
    (r"C:\Work\proj", r'File "C:\Work\proj\pkg\a.py", line 1', 'File "pkg/a.py", line 1'.replace("/", "\\")),
    (r"C:\Work\proj", "see C:/Work/proj/b.txt", "see b.txt"),
    (r"C:\Work\proj", r"cwd is C:\Work\proj", "cwd is ."),
    (r"C:\Work\proj", r"other C:\Work\proj2\x.py stays", r"other C:\Work\proj2\x.py stays"),
    ("/home/u/proj", "/home/u/proj/src/m.py:3: error", "src/m.py:3: error"),
])
def test_relativize(root, text, expected):
    from core.safety import relativize
    if os.name == "nt" and root.startswith("/") or os.name != "nt" and root.startswith("C:"):
        pytest.skip("path style of the other OS")
    assert relativize(text, root) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_relativize_ignores_case_on_windows():
    from core.safety import relativize
    assert relativize(r"c:\work\PROJ\a.py", r"C:\Work\proj") == "a.py"
