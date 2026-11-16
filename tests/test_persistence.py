"""Phase 4: state manager, logs, durable checkpoints, resume, metrics, rendered plan."""
import json
import sqlite3

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

import config
from core import metrics
from core.runner import ResumeError, prepare_resume, start_run
from core.safety import redact_secrets
from core.schemas import FileChange
from core.state_manager import StateManager, make_checkpointer
from core.task_manager import load_tasks, render_plan_md
from tests.conftest import tool_call_msg

PLAN = {"project_id": "p", "goal": "g", "steps": [
    {"step_id": 1, "title": "first", "description": "d1", "dependencies": [], "acceptance": [{"type": "file_exists", "path": "a.txt"}]},
    {"step_id": 2, "title": "second", "description": "d2", "dependencies": [1], "acceptance": []}]}


@pytest.fixture
def env(tmp_path, monkeypatch, fake_llm, workspace):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    import agents.llm as llm_mod
    import core.orchestrator as orch
    monkeypatch.setattr(llm_mod, "_override", fake_llm)

    def stub_planner(state, config):  # same persistence as the real planner node, without calling Gemini
        ctx = config["configurable"]["project"]
        ctx.state_manager.save_plan(ctx.run_id, PLAN, load_tasks(PLAN))
        return {"plan": PLAN, "tasks": PLAN["steps"], "current_task_index": 0, "plan_feedback": None,
                "task_status": {"1": "pending", "2": "pending"}, "task_errors": {}, "run_status": "running"}
    monkeypatch.setattr(orch, "planner_node", stub_planner)

    class E:
        sm = StateManager(tmp_path / "data" / "nexura.db")
        ckpt_path = tmp_path / "data" / "ckpt.db"
        ws = workspace
        llm = fake_llm
        build = staticmethod(orch.build_graph)
    return E


def run_new(env, script):
    from core.runner import initial_state  # noqa: F401
    env.llm.script[:] = script
    ctx, state = start_run(env.sm, "proj", env.ws, "make a.txt")
    graph = env.build(checkpointer=make_checkpointer(env.ckpt_path))
    cfg = {"configurable": {"thread_id": ctx.run_id, "project": ctx}, "recursion_limit": 200}
    graph.invoke(state, cfg)
    return ctx, graph, cfg


FIRST = [AIMessage(content="", tool_calls=[
    {"name": "create_file", "args": {"target_file": "a.txt", "content": "hello"}, "id": "c1"},
    {"name": "task_complete", "args": {"summary": "ok"}, "id": "c2"}])]
SECOND = [tool_call_msg("task_complete", {"summary": "ok"}, "c3")]


# ─── state manager ──────────────────────────────────────────

def test_schema_version_and_round_trip(tmp_path):
    sm = StateManager(tmp_path / "x.db")
    assert sm.schema_version() == 1
    assert StateManager(tmp_path / "x.db").schema_version() == 1  # re-open: no re-migration
    sm.upsert_project("p", "/w")
    sm.create_run("r1", "p", "prompt", "ask_risky")
    sm.save_plan("r1", PLAN, load_tasks(PLAN))
    sm.mark_task("r1", 1, "running")
    sm.mark_task("r1", 1, "done", attempts=2)
    sm.add_file_change("r1", FileChange(file="a.txt", change_type="created", task_id=1, sha256_after="ab"), "user")
    sm.add_event("r1", "custom", 1, k="v")
    assert sm.get_run("r1").plan["goal"] == "g" and sm.project_path("p") == "/w"
    t = sm.list_tasks("r1")
    assert [(x["task_id"], x["status"], x["attempts"]) for x in t] == [(1, "done", 2), (2, "pending", 0)]
    assert t[0]["started_at"] and t[0]["finished_at"]
    fc = sm.list_file_changes("r1")[0]
    assert (fc["file"], fc["approval_source"]) == ("a.txt", "user")
    assert sm.list_events("r1")[0]["data"] == {"k": "v"}
    with pytest.raises(ValueError):
        sm.update_run("r1", "bogus")
    sm.update_run("r1", "failed", "boom")
    r = sm.get_run("r1")
    assert (r.status, r.error) == ("failed", "boom") and r.finished_at


def test_only_state_manager_writes_the_db_and_agents_cannot_import_it():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for f in (root / "agents").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            assert not any(m and "state_manager" in m for m in mods), f.name
    sqlite_users = [f.name for f in (root / "core").glob("*.py") if "sqlite3.connect" in f.read_text(encoding="utf-8")]
    assert sqlite_users == ["state_manager.py"]


# ─── full run persisted ─────────────────────────────────────

def test_run_is_persisted_logged_and_rendered(env):
    ctx, graph, cfg = run_new(env, FIRST + SECOND)
    graph.invoke(Command(resume="yes"), cfg)
    run = env.sm.get_run(ctx.run_id)
    assert run.status == "completed" and run.finished_at
    assert [t["status"] for t in env.sm.list_tasks(ctx.run_id)] == ["done", "done"]
    fc = env.sm.list_file_changes(ctx.run_id)
    assert len(fc) == 1 and fc[0]["file"] == "a.txt" and fc[0]["sha256_before"] is None and len(fc[0]["sha256_after"]) == 64
    types = [e["type"] for e in env.sm.list_events(ctx.run_id)]
    for t in ("run_started", "task_started", "llm_call", "tool_call", "verification_passed", "task_done", "run_finished"):
        assert t in types, t
    log = ctx.logger.read()
    llm = [r for r in log if r["agent"] == "coder"]
    assert llm and all(r["prompt_version"] == "1" and r["model"] == "fake" and r["run_id"] == ctx.run_id for r in llm)
    assert {"timestamp", "project_id", "run_id", "task_id", "agent", "prompt_version", "model", "input_summary", "output_summary", "result"} <= set(llm[0])
    md = (ctx.run_dir / "plan.md").read_text(encoding="utf-8")
    assert "- [x] **Step 1: first**" in md and "- [x] **Step 2: second**" in md and "completed" in md
    assert not (env.ws / "plan.md").exists() and not (env.ws / "plan.json").exists()   # nothing written into the user's tree


def test_plan_md_marks_failed_and_pending():
    md = render_plan_md(PLAN, {"1": "failed", "2": "skipped"}, {"1": "TOOL_CAP"}, "failed")
    assert "[!] **Step 1: first** (FAILED: TOOL_CAP)" in md and "[-] **Step 2: second** (SKIPPED)" in md
    assert "- [ ] **Step 1" in render_plan_md(PLAN, {})


def test_logs_redact_secrets(tmp_path):
    from core.runlog import RunLogger
    lg = RunLogger(tmp_path / "l.jsonl", "p", "r")
    lg.log(agent="coder", input_summary="key AIzaSyA12345678901234567890123456789012345 password: hunter2hunter2", result="x" * 5000)
    rec = lg.read()[0]
    assert "AIza" not in rec["input_summary"] and "hunter2" not in rec["input_summary"] and len(rec["result"]) < 700
    assert "[REDACTED]" in redact_secrets("ghp_" + "a" * 36)


# ─── durable checkpoints + resume ───────────────────────────

def test_checkpoints_survive_a_new_connection(env):
    ctx, graph, cfg = run_new(env, FIRST + SECOND)
    graph.invoke(Command(resume="yes"), cfg)
    graph2 = env.build(checkpointer=make_checkpointer(env.ckpt_path))  # brand-new connection, same file
    assert graph2.get_state(cfg).values["run_status"] == "completed"
    assert sqlite3.connect(env.ckpt_path).execute("select count(*) from checkpoints").fetchone()[0] > 0


def test_kill_mid_run_then_resume_continues_from_first_unfinished_task(env):
    ctx, graph, cfg = run_new(env, FIRST + [KeyboardInterrupt()])   # task 1 done, process "dies" in task 2
    with pytest.raises(KeyboardInterrupt):
        graph.invoke(Command(resume="yes"), cfg)
    assert [t["status"] for t in env.sm.list_tasks(ctx.run_id)] == ["done", "running"]
    assert env.sm.get_run(ctx.run_id).status == "running"

    env.llm.script[:] = list(SECOND)
    env.llm.calls.clear()
    ctx2, state = prepare_resume(env.sm, ctx.run_id)
    assert state["current_task_index"] == 1 and state["task_status"] == {"1": "done", "2": "pending"} and state["resuming"]
    graph2 = env.build(checkpointer=make_checkpointer(env.ckpt_path))
    cfg2 = {"configurable": {"thread_id": "resume-1", "project": ctx2}, "recursion_limit": 200}
    graph2.invoke(state, cfg2)

    assert len(env.llm.calls) == 1                                  # task 1 was NOT redone; no new plan call either
    assert "Task 2: second" in str(env.llm.calls[0][0].content)
    run = env.sm.get_run(ctx.run_id)
    assert run.status == "completed"
    assert [t["status"] for t in env.sm.list_tasks(ctx.run_id)] == ["done", "done"]
    assert "run_resumed" in [e["type"] for e in env.sm.list_events(ctx.run_id)]


def test_resume_errors(env, tmp_path):
    with pytest.raises(ResumeError, match="No such run"):
        prepare_resume(env.sm, "nope")
    ctx, graph, cfg = run_new(env, FIRST + SECOND)
    graph.invoke(Command(resume="yes"), cfg)
    with pytest.raises(ResumeError, match="no unfinished"):
        prepare_resume(env.sm, ctx.run_id)
    env.sm.create_run("noplan", "proj", "x")
    with pytest.raises(ResumeError, match="no saved plan"):
        prepare_resume(env.sm, "noplan")


def test_failed_task_is_retried_on_resume_with_fresh_attempts(env):
    spin = [tool_call_msg("list_directory", {"target_dir": "."}, f"s{i}") for i in range(config.MAX_TOOL_CALLS_PER_TASK)]
    ctx, graph, cfg = run_new(env, spin)
    graph.invoke(Command(resume="yes"), cfg)
    assert env.sm.get_run(ctx.run_id).status == "failed"
    assert env.sm.list_tasks(ctx.run_id)[0]["reason"].startswith("TOOL_CAP")
    ctx2, state = prepare_resume(env.sm, ctx.run_id)
    assert state["task_status"] == {"1": "pending", "2": "pending"} and env.sm.list_tasks(ctx.run_id)[0]["attempts"] == 0


# ─── metrics ────────────────────────────────────────────────

def test_metrics_reflect_the_run(env):
    bad = tool_call_msg("create_file", {"target_file": "../x", "content": "1"}, "bad")
    ctx, graph, cfg = run_new(env, [bad] + FIRST + SECOND)
    graph.invoke(Command(resume="yes"), cfg)
    m = metrics.compute(env.sm, ctx.run_id)
    assert m["status"] == "completed" and m["tasks_total"] == 2 and m["tasks_done"] == 2 and m["task_success_rate"] == 1.0
    assert m["retries_per_task"]["1"] == 1 and m["error_types"] == {"rejected:OUTSIDE_ROOT": 1}
    assert m["wall_time_s"] >= 0 and m["tokens"] is None
    assert metrics.compute(env.sm, "missing") is None


def test_cli_metrics_and_resume_flags(env, capsys, monkeypatch):
    import main
    ctx, graph, cfg = run_new(env, FIRST + SECOND)
    graph.invoke(Command(resume="yes"), cfg)
    monkeypatch.setattr(config, "DB_PATH", env.sm.db_path)
    monkeypatch.setattr("sys.argv", ["main.py", "--metrics", ctx.run_id])
    main.main()
    assert json.loads(capsys.readouterr().out.split("\n[Config]")[-1].split("DISABLED (no LANGCHAIN_API_KEY found)")[-1])["tasks_done"] == 2
    monkeypatch.setattr("sys.argv", ["main.py", "--resume", "nope"])
    with pytest.raises(SystemExit) as e:
        main.main()
    assert e.value.code == 2
