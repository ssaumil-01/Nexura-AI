"""V2 Phase 5 done-criterion: on a fixture existing repo a run indexes it, plans against real files, patches them with reviewable
diffs, detects an injected regression, and undo restores the exact pre-run state (fake LLM, FakeEmbedder, no network)."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

pytest.importorskip("chromadb")
import config  # noqa: E402
from core import runner  # noqa: E402
from core.state_manager import StateManager  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "evals" / "fixtures" / "buggy_shop"


def msg(*calls):
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"c{i}"} for i, (n, a) in enumerate(calls)])


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.is_file() and ".git" not in f.parts and "__pycache__" not in f.parts and ".pytest_cache" not in f.parts:
            h.update(f.relative_to(root).as_posix().encode() + f.read_bytes())
    return h.hexdigest()


@pytest.fixture(autouse=True)
def _python_is_this_python(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: sys.executable if name == "python" else real(name, *a, **k))


def test_existing_repo_end_to_end(tmp_path, monkeypatch, fake_llm, git_repo):
    import agents.llm as llm_mod
    import agents.planner as planner_mod
    from core.orchestrator import build_graph
    from rag.embedder import FakeEmbedder
    from rag.indexer import Indexer

    monkeypatch.setenv("CODER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("PLANNER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v2")
    monkeypatch.setattr(config, "PATCH_MODE", "v2")
    monkeypatch.setattr(llm_mod, "_override", fake_llm)

    shutil.copytree(FIXTURE, git_repo, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    (git_repo / "pytest.ini").write_text("[pytest]\n")        # keep the test run self-contained
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixture"], cwd=git_repo, check=True)
    before = tree_hash(git_repo)

    # the planner: sees the project, names real files, derives its check from the project
    seen = {}
    plan = {"project_id": "shop", "goal": "fix the discount", "steps": [{
        "step_id": 1, "title": "Fix apply_discount in shop/discounts.py", "description": "apply_discount subtracts the percent instead of taking it off proportionally.",
        "dependencies": [], "files": ["shop/discounts.py"], "new_files": [],
        "acceptance": [{"type": "file_exists", "path": "shop/discounts.py"}]}]}
    monkeypatch.setattr(planner_mod.PlannerAgent, "call_llm", lambda self, p: seen.setdefault("prompt", p) and json.dumps(plan))
    monkeypatch.setattr(planner_mod.PlannerAgent, "call_text", lambda self, p: "A tiny shop library with a cart, a catalog and discounts.")

    ix = Indexer(git_repo, "ws", FakeEmbedder(dim=16))
    ix.open()
    ix.build()

    sm = StateManager(tmp_path / "data" / "n.db")
    ctx, state = runner.start_run(sm, "ws", git_repo, "Fix the discount calculation", approval_level="ask_every_edit",
                                  baseline_command="python -m pytest -q")
    ctx.indexer = ix
    fix = ("    return round(amount - percent, 2)", "    return round(amount * (1 - percent / 100), 2)")
    break_bread = ('"bread": 2.25', '"bread": 99.0')                      # the injected regression: breaks test_subtotal
    fake_llm.script[:] = [
        msg(("read_file", {"target_file": "shop/discounts.py"}), ("read_file", {"target_file": "shop/catalog.py"})),
        msg(("apply_patch", {"target_file": "shop/discounts.py", "hunks": [{"search_text": fix[0], "replace_text": fix[1]}]}),
            ("apply_patch", {"target_file": "shop/catalog.py", "hunks": [{"search_text": break_bread[0], "replace_text": break_bread[1]}]})),
        msg(("task_complete", {"summary": "fixed"}))]

    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": ctx.run_id, "project": ctx}, "recursion_limit": 200}
    out = graph.invoke(state, cfg)
    approvals = []
    while out.get("__interrupt__"):
        payload = out["__interrupt__"][0].value
        if isinstance(payload, str):
            resume = "yes"
        else:
            approvals.append(payload)
            resume = {"decision": "approve"}
        out = graph.invoke(Command(resume=resume), cfg)
    values = graph.get_state(cfg).values

    # planned against real files
    assert "shop/discounts.py" in seen["prompt"] and "EXISTING PROJECT" in seen["prompt"] and "tiny shop library" in seen["prompt"]
    assert sm.get_repo_summary("ws") is not None and [v["version"] for v in sm.list_plan_versions(ctx.run_id)] == [1]

    # reviewable diffs: the approval shows what will be written
    diffs = {d["file"]: d for a in approvals for d in a["diffs"]}
    assert set(diffs) >= {"shop/discounts.py", "shop/catalog.py"}
    assert "-    return round(amount - percent, 2)" in diffs["shop/discounts.py"]["diff"]
    assert "+    return round(amount * (1 - percent / 100), 2)" in diffs["shop/discounts.py"]["diff"]

    # the index followed the edit and retrieval was used
    kinds = [e["type"] for e in sm.list_events(ctx.run_id)]
    assert "index_updated" in kinds and "retrieval" in kinds and "planner_context" in kinds
    assert any("amount * (1 - percent / 100)" in c.text for c in ix.lexical.chunks_of("shop/discounts.py"))

    # baseline: 1 failure was already there; the injected break is the regression
    base = sm.get_baseline(ctx.run_id)
    assert base["failing"] == ["tests/test_cart.py::test_total_with_discount"]
    (reg,) = [e for e in sm.list_events(ctx.run_id) if e["type"] == "regression_detected"]
    assert reg["data"]["new_failures"] == ["tests/test_cart.py::test_subtotal"]
    assert reg["data"]["fixed"] == ["tests/test_cart.py::test_total_with_discount"]
    assert values["run_status"] == "completed_with_failures" and "Regression" in sm.get_run(ctx.run_id).error

    # undo restores the exact pre-run state
    assert tree_hash(git_repo) != before
    res = runner.undo(sm, ctx.run_id)
    assert res.applied, res
    assert tree_hash(git_repo) == before
    ix.close()
