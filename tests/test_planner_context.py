"""V2-5.4 / 5.5: repo summary (cached by source hash), the context-aware planner prompt, path validation, V1 equivalence."""
import json

import pytest

import config
from agents.planner import PlannerAgent
from core.planner_context import build_planner_context, effective_profile
from core.schemas import ProjectContext
from core.state_manager import StateManager
from tests.conftest import ALWAYS_OK


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANNER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v2")
    ws = tmp_path / "ws"
    (ws / "shop").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "shop" / "cart.py").write_text("def total(items):\n    return sum(items)\n")
    (ws / "tests" / "test_cart.py").write_text("def test_total():\n    pass\n")
    (ws / "README.md").write_text("# Shop\nA small shop library.\n")
    (ws / "requirements.txt").write_text("pytest\n")
    (ws / ".env").write_text("API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789\n")
    sm = StateManager(tmp_path / "n.db")
    ctx = ProjectContext(run_id="r", root=ws, project_id="w1", state_manager=sm)
    calls = []

    def call_text(prompt):
        calls.append(prompt)
        return "Purpose: a small shop library. Run tests with python -m pytest."
    return ws, sm, ctx, calls, call_text


def test_v1_mode_and_empty_workspaces_get_no_context(env, tmp_path, monkeypatch):
    ws, sm, ctx, calls, call_text = env
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v1")
    assert build_planner_context(ctx, "add discount", call_text) is None
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v2")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert build_planner_context(ProjectContext(run_id="r", root=empty, project_id="e", state_manager=sm), "x", call_text) is None
    assert calls == []


def test_context_has_summary_profile_files_and_no_secret_files(env):
    ws, sm, ctx, calls, call_text = env
    c = build_planner_context(ctx, "add discount to the cart", call_text)
    assert "shop/cart.py" in c["files"] and ".env" not in c["files"]
    assert c["profile"]["test_commands"] == ["python -m pytest"] and "small shop library" in c["summary"]
    assert "sk-abcdefghijkl" not in calls[0]                       # the summary input never contains secrets
    assert sm.get_profile("w1")["detected"]["languages"] == ["python"]
    assert sm.get_repo_summary("w1")["version"] == 1


def test_summary_is_cached_until_the_source_changes_meaningfully(env):
    ws, sm, ctx, calls, call_text = env
    build_planner_context(ctx, "x", call_text)
    build_planner_context(ctx, "y", call_text)
    assert len(calls) == 1                                          # same hash -> cached
    (ws / "shop" / "extra.py").write_text("x = 1\n")                # one new file in a known folder: not meaningful
    build_planner_context(ctx, "z", call_text)
    assert len(calls) == 1
    (ws / "README.md").write_text("# Shop\nNow with a CLI and a web UI.\n")
    build_planner_context(ctx, "z", call_text)
    assert len(calls) == 2 and sm.get_repo_summary("w1")["version"] == 2
    (ws / "billing").mkdir()
    (ws / "billing" / "invoice.py").write_text("y = 2\n")           # a new folder: meaningful
    build_planner_context(ctx, "z", call_text)
    assert len(calls) == 3


def test_summary_failure_never_fails_planning(env):
    ws, sm, ctx, calls, call_text = env

    def boom(prompt):
        raise RuntimeError("quota")
    c = build_planner_context(ctx, "x", boom)
    assert c["summary"] == "" and any(e["type"] == "summary_failed" for e in sm.list_events("r"))
    build_planner_context(ctx, "x", call_text)                      # recovers on the next call
    assert sm.get_repo_summary("w1") is not None


def test_summary_secret_in_model_output_is_redacted(env):
    ws, sm, ctx, calls, call_text = env
    c = build_planner_context(ctx, "x", lambda p: "key is sk-abcdefghijklmnopqrstuvwxyz0123456789 ok")
    assert "sk-abcdefghijkl" not in c["summary"] and "sk-abcdefghijkl" not in sm.get_repo_summary("w1")["summary"]


def test_user_overrides_reach_the_planner_context(env):
    ws, sm, ctx, calls, call_text = env
    effective_profile(ctx)
    sm.save_profile("w1", sm.get_profile("w1")["detected"], overrides={"test_commands": ["make test"]})
    assert build_planner_context(ctx, "x", call_text)["profile"]["test_commands"] == ["make test"]


# ─── the prompt and plan validation ─────────────────────────

def plan_json(files=(), new_files=(), command="python -m pytest -q"):
    return json.dumps({"project_id": "p", "goal": "g", "steps": [{
        "step_id": 1, "title": "t", "description": "d", "dependencies": [], "files": list(files), "new_files": list(new_files),
        "acceptance": [{"type": "command_ok", "command": command}]}]})


def test_planner_prompt_contains_the_project_knowledge(env, monkeypatch):
    ws, sm, ctx, calls, call_text = env
    prompts = []
    monkeypatch.setattr(PlannerAgent, "call_llm", lambda self, p: prompts.append(p) or plan_json(files=["shop/cart.py"]))
    ctx.extra_commands = ["poetry"]
    pctx = build_planner_context(ctx, "add a discount to cart total", call_text)
    plan = PlannerAgent().plan("add a discount", str(ws.parent / "plan"), context=pctx)
    p = prompts[0]
    assert "EXISTING PROJECT" in p and "shop/cart.py" in p and "small shop library" in p and "python -m pytest" in p
    assert "poetry" in p and "PREFER EDITING" in p
    assert plan.steps[0].files == ["shop/cart.py"]


def test_plan_naming_a_missing_file_is_sent_back_then_accepted(env, monkeypatch):
    ws, sm, ctx, calls, call_text = env
    answers = [plan_json(files=["shop/nope.py"]), plan_json(files=["shop/cart.py"], new_files=["shop/discount.py"])]
    prompts = []

    def llm(self, prompt):
        prompts.append(prompt)
        return answers[len(prompts) - 1]
    monkeypatch.setattr(PlannerAgent, "call_llm", llm)
    plan = PlannerAgent().plan("x", str(ws.parent / "plan"), context=build_planner_context(ctx, "x", call_text))
    assert len(prompts) == 2 and "shop/nope.py" in prompts[1] and "does not exist" in prompts[1]
    assert plan.steps[0].new_files == ["shop/discount.py"]


def test_plan_marking_an_existing_file_as_new_is_rejected(env, monkeypatch):
    ws, sm, ctx, calls, call_text = env
    from agents.planner import PlanError
    monkeypatch.setattr(PlannerAgent, "call_llm", lambda self, p: plan_json(new_files=["shop/cart.py"]))
    with pytest.raises(PlanError) as e:
        PlannerAgent().plan("x", str(ws.parent / "plan"), context=build_planner_context(ctx, "x", call_text))
    assert "already exists" in str(e.value)


def test_path_check_cannot_be_escaped_with_traversal(env):
    ws, sm, ctx, calls, call_text = env
    exists = build_planner_context(ctx, "x", call_text)["exists"]
    assert exists("shop/cart.py") and not exists("../ws/shop/cart.py".replace("ws", "other")) and not exists("../../etc/passwd")


def test_workspace_approved_executable_is_valid_in_plan_checks(env, monkeypatch):
    ws, sm, ctx, calls, call_text = env
    ctx.extra_commands = ["poetry"]
    monkeypatch.setattr(PlannerAgent, "call_llm", lambda self, p: plan_json(files=["shop/cart.py"], command="poetry run pytest"))
    plan = PlannerAgent().plan("x", str(ws.parent / "plan"), context=build_planner_context(ctx, "x", call_text))
    assert plan.steps[0].acceptance[0].command == "poetry run pytest"


def test_without_context_the_prompt_is_v1(env, monkeypatch):
    ws, sm, ctx, calls, call_text = env
    prompts = []
    monkeypatch.setattr(PlannerAgent, "call_llm", lambda self, p: prompts.append(p) or json.dumps({
        "project_id": "p", "goal": "g", "steps": [{"step_id": 1, "title": "t", "description": "d", "acceptance": ALWAYS_OK}]}))
    PlannerAgent().plan("x", str(ws.parent / "plan"))
    assert "EXISTING PROJECT" not in prompts[0]
