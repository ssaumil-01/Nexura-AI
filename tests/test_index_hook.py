"""V2-2.10 / invariant 5: files changed by a task are re-indexed BEFORE the next task starts; an index failure never fails a run."""
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

pytest.importorskip("chromadb")
import config  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from tests.conftest import tool_call_msg  # noqa: E402

PLAN = {"project_id": "p", "goal": "g", "steps": [
    {"step_id": 1, "title": "write mod", "description": "d", "dependencies": [], "acceptance": [{"type": "file_exists", "path": "mod.py"}]},
    {"step_id": 2, "title": "use mod", "description": "d", "dependencies": [1], "acceptance": [{"type": "file_exists", "path": "mod.py"}]}]}


def run_with(tmp_path, monkeypatch, fake_llm, git_repo, indexer):
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
    sm = StateManager(tmp_path / "data" / "n.db")
    ctx, state = start_run(sm, "hook", git_repo, "x", approval_level="auto_within_limits")
    ctx.indexer = indexer
    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": ctx.run_id, "project": ctx}, "recursion_limit": 200}
    out = graph.invoke(state, cfg)
    while out.get("__interrupt__"):
        out = graph.invoke(Command(resume="yes" if isinstance(out["__interrupt__"][0].value, str) else {"decision": "approve"}), cfg)
    return sm, ctx, graph.get_state(cfg).values


def test_changed_files_are_indexed_before_the_next_task(tmp_path, monkeypatch, fake_llm, git_repo):
    from rag.embedder import FakeEmbedder
    from rag.indexer import Indexer
    monkeypatch.setattr(config, "INDEX_DIR", tmp_path / "index")
    ix = Indexer(git_repo, "ws", FakeEmbedder(dim=16))
    ix.open()
    ix.build()
    seen = {}

    def second_task_starts():
        seen["symbols"] = {c.symbol for c in ix.lexical.chunks_of("mod.py")}
        return tool_call_msg("task_complete", {"summary": "ok"}, "t2")

    fake_llm.script[:] = [tool_call_msg("create_file", {"target_file": "mod.py", "content": "def special_fn():\n    return 1\n"}, "c"),
                          tool_call_msg("task_complete", {"summary": "ok"}, "t1"), second_task_starts]
    sm, ctx, values = run_with(tmp_path, monkeypatch, fake_llm, git_repo, ix)
    assert values["run_status"] == "completed"
    assert seen["symbols"] == {"special_fn"}                     # already in the index when task 2's agent ran
    assert any(e["type"] == "index_updated" for e in sm.list_events(ctx.run_id))
    ix.close()


def test_an_index_failure_does_not_fail_the_run(tmp_path, monkeypatch, fake_llm, git_repo):
    class Broken:
        def update(self, paths):
            raise RuntimeError("index exploded")

    fake_llm.script[:] = [tool_call_msg("create_file", {"target_file": "mod.py", "content": "x = 1\n"}, "c"),
                          tool_call_msg("task_complete", {"summary": "ok"}, "t1"), tool_call_msg("task_complete", {"summary": "ok"}, "t2")]
    sm, ctx, values = run_with(tmp_path, monkeypatch, fake_llm, git_repo, Broken())
    assert values["run_status"] == "completed"
    failed = [e for e in sm.list_events(ctx.run_id) if e["type"] == "index_update_failed"]
    assert failed and "index exploded" in failed[0]["data"]["error"]


def test_without_an_indexer_nothing_changes(tmp_path, monkeypatch, fake_llm, git_repo):
    fake_llm.script[:] = [tool_call_msg("create_file", {"target_file": "mod.py", "content": "x = 1\n"}, "c"),
                          tool_call_msg("task_complete", {"summary": "ok"}, "t1"), tool_call_msg("task_complete", {"summary": "ok"}, "t2")]
    sm, ctx, values = run_with(tmp_path, monkeypatch, fake_llm, git_repo, None)
    assert values["run_status"] == "completed"
    assert not [e for e in sm.list_events(ctx.run_id) if e["type"].startswith("index_")]
