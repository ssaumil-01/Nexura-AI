import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from core.schemas import ExecutionPlan
from core.task_manager import final_run_status, pick_next, run_summary, validate_plan


def step(i, deps=(), acc=True, doc=False):
    s = {"step_id": i, "title": f"t{i}", "description": "d", "dependencies": list(deps), "documentation_only": doc,
         "acceptance": [{"type": "file_exists", "path": "a.py"}] if acc else []}
    return s


def plan(*steps):
    return ExecutionPlan.model_validate({"project_id": "p", "goal": "g", "steps": list(steps)})


# ─── validation ─────────────────────────────────────────────

def test_valid_plan():
    assert validate_plan(plan(step(1), step(2, [1]))) == []


@pytest.mark.parametrize("steps,fragment", [
    ([], "no steps"),
    ([step(2)], "1.."),
    ([step(1), step(3)], "1.."),
    ([step(1, [2]), step(2)], "not earlier"),
    ([step(1), step(2, [9])], "unknown step 9"),
    ([step(1), step(2, acc=False)], "no acceptance"),
])
def test_invalid_plans(steps, fragment):
    assert fragment in "; ".join(validate_plan(plan(*steps)))


def test_doc_only_step_may_skip_acceptance():
    assert validate_plan(plan(step(1), step(2, acc=False, doc=True))) == []


def test_acceptance_required_fields():
    with pytest.raises(ValueError):
        ExecutionPlan.model_validate({"project_id": "p", "goal": "g", "steps": [
            {**step(1), "acceptance": [{"type": "http_status"}]}]})
    with pytest.raises(ValueError):
        ExecutionPlan.model_validate({"project_id": "p", "goal": "g", "steps": [
            {**step(1), "acceptance": [{"type": "output_contains", "command": "python x.py"}]}]})


# ─── pick_next / summary ────────────────────────────────────

TASKS = [{"step_id": 1, "dependencies": []}, {"step_id": 2, "dependencies": [1]}, {"step_id": 3, "dependencies": []}]


def test_pick_next_stop_policy_skips_everything_after_failure():
    idx, st = pick_next(TASKS, {"1": "failed", "2": "pending", "3": "pending"}, "stop")
    assert idx is None and st == {"1": "failed", "2": "skipped", "3": "skipped"}


def test_pick_next_continue_skips_only_dependents():
    idx, st = pick_next(TASKS, {"1": "failed", "2": "pending", "3": "pending"}, "continue")
    assert idx == 2 and st["2"] == "skipped"


def test_pick_next_in_order():
    assert pick_next(TASKS, {"1": "done", "2": "pending", "3": "pending"}, "stop")[0] == 1


def test_final_status_and_summary():
    assert final_run_status({"1": "done", "2": "done"}) == "completed"
    assert final_run_status({"1": "done", "2": "failed"}) == "completed_with_failures"
    assert final_run_status({"1": "failed", "2": "skipped"}) == "failed"
    s = run_summary({"tasks": [{"step_id": 1, "title": "a"}], "task_status": {"1": "failed"},
                     "task_errors": {"1": "TOOL_CAP"}, "run_status": "failed"})
    assert s["failed"] == [{"step_id": 1, "title": "a", "reason": "TOOL_CAP"}] and s["done"] == []


# ─── planner agent ──────────────────────────────────────────

@pytest.fixture
def planner(monkeypatch, tmp_path):
    monkeypatch.setenv("PLANNER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    from agents.planner import PlannerAgent
    p = PlannerAgent()
    p.replies, p.prompts = [], []

    def fake(prompt):
        p.prompts.append(prompt)
        r = p.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    p.call_llm = fake
    p.path = str(tmp_path / "ws")
    return p


GOOD = json.dumps({"project_id": "p", "goal": "g", "steps": [step(1), step(2, [1])]})


def test_planner_valid_first_try_and_saves_files(planner):
    planner.replies = [GOOD]
    out = planner.plan("make x", planner.path)
    assert len(out.steps) == 2
    import os
    assert os.path.exists(os.path.join(planner.path, "plan.json")) and os.path.exists(os.path.join(planner.path, "plan.md"))


def test_planner_strips_code_fences(planner):
    planner.replies = ["```json\n" + GOOD + "\n```"]
    assert len(planner.plan("x", planner.path).steps) == 2


def test_planner_retries_with_error_feedback_then_succeeds(planner):
    planner.replies = ["not json at all", json.dumps({"project_id": "p", "goal": "g", "steps": [step(1, acc=False)]}), GOOD]
    assert len(planner.plan("x", planner.path).steps) == 2
    assert "INVALID" in planner.prompts[1] and "no acceptance" in planner.prompts[2]


def test_planner_gives_up_after_three_invalid_answers(planner):
    from agents.planner import PlanError
    planner.replies = ["bad", "bad", "bad"]
    with pytest.raises(PlanError, match="3 attempts"):
        planner.plan("x", planner.path)


def test_planner_api_failure_is_not_retried(planner):
    from agents.planner import PlanError
    planner.replies = [RuntimeError("503 down")]
    with pytest.raises(PlanError, match="LLM call failed"):
        planner.plan("x", planner.path)
    assert len(planner.prompts) == 1


def test_planner_prompt_version_and_granularity():
    import config
    from agents.planner import PROMPT_TEMPLATE
    assert config.PROMPT_VERSIONS["planner"] == "2" and "3-8 STEPS" in PROMPT_TEMPLATE and "acceptance" in PROMPT_TEMPLATE


def test_planner_node_failure_ends_run_not_crash(monkeypatch, planner):
    import agents.planner as ap
    import core.graph  # noqa: F401  (needs env)
    monkeypatch.setattr(ap.PlannerAgent, "plan", lambda *a, **k: (_ for _ in ()).throw(ap.PlanError("boom")))
    from core.graph import planner_node, route_after_planner
    from langgraph.graph import END
    out = planner_node({"user_prompt": "x", "workspace_path": planner.path})
    assert out["run_status"] == "failed" and "boom" in out["run_error"]
    assert route_after_planner(out) == END


# ─── server does not block the event loop (2.13) ────────────

def test_server_stream_does_not_block_event_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("CODER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    import server

    class FakeGraph:
        def stream(self, *a, **k):
            time.sleep(0.6)  # a slow, blocking graph step
            yield {"planner": {"x": 1}}

        def get_state(self, c):
            return SimpleNamespace(next=(), tasks=(), values={"run_status": "completed", "task_status": {}, "tasks": []})

    monkeypatch.setattr(server, "build_graph", lambda **k: FakeGraph())

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1
        t = asyncio.create_task(ticker())
        events = [e async for e in server.event_generator("proj", prompt="hi")]
        t.cancel()
        return ticks, events
    ticks, events = asyncio.run(main())
    assert ticks > 10  # the loop kept running during the 0.6s blocking step
    assert events[-1]["event"] == "complete" and json.loads(events[-1]["data"])["run_status"] == "completed"
