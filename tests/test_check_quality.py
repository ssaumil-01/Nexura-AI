"""Found by the live Streamlit to-do test: weak plan checks (syntax only), short retries on Google 503s, the plan printed twice."""
import json

import pytest

import config
from core import verification
from core.schemas import ExecResult, ProjectContext
from tests.test_planner_and_tasks import GOOD, planner  # noqa: F401  (fixture)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "app.py").write_text("import streamlit as st\nst.title('x')\n")
    (tmp_path / "todo.py").write_text("def add():\n    pass\n")
    (tmp_path / "test_app.py").write_text("from streamlit.testing.v1 import AppTest\n")
    return ProjectContext(run_id="r", root=tmp_path)


def fake_run(calls, ok=True):
    def run(action, ctx, **kw):
        calls.append((action, kw.get("command") or kw.get("path")))
        return ExecResult(ok=ok, stdout="Syntax check passed" if action == "verify_code_syntax" else "page rendered",
                            stderr="" if ok else "KeyError: 'id'")
    return run


def test_v2_adds_a_streamlit_render_check_for_changed_streamlit_pages(ws, monkeypatch):
    calls = []
    monkeypatch.setattr(verification, "_run", fake_run(calls))
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v2")
    task = {"acceptance": [{"type": "syntax_ok", "paths": ["app.py"]}]}
    res = verification.verify(task, ws, ["app.py", "todo.py", "test_app.py"], [])
    smoke = [c for c in res.checks if c.type == "streamlit_smoke"]
    assert res.ok and len(smoke) == 1 and "app.py" in smoke[0].detail   # not for todo.py (no streamlit) nor test files
    assert any(a == "run_command" and "AppTest.from_file('app.py'" in c for a, c in calls)


def test_a_streamlit_page_that_crashes_fails_verification(ws, monkeypatch):
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v2")
    monkeypatch.setattr(verification, "_run", fake_run([], ok=False))
    res = verification.verify({"acceptance": []}, ws, ["app.py"], [])
    assert not res.ok and "crashed" in res.failure_text() and "KeyError" in res.failure_text()


def test_v1_verification_is_unchanged(ws, monkeypatch):
    calls = []
    monkeypatch.setattr(verification, "_run", fake_run(calls))
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v1")
    res = verification.verify({"acceptance": [{"type": "syntax_ok", "paths": ["app.py"]}]}, ws, ["app.py"], [])
    assert [c.type for c in res.checks] == ["syntax_ok"] and [a for a, _ in calls] == ["verify_code_syntax"]


def test_streamlit_smoke_command_passes_the_command_rules(ws):
    from core.safety import parse_command
    calls = []
    verification._run = verification._run   # real command text, built the same way
    verification_run, verification._run = verification._run, fake_run(calls)
    try:
        verification._streamlit_smoke(ws, "pages\\home.py")
    finally:
        verification._run = verification_run
    argv = parse_command(calls[0][1], ws.root).argv
    assert argv[1] == "-c" and "AppTest.from_file('pages/home.py'" in argv[2]


def test_check_quality_section_only_in_v2(planner, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v1")
    planner.replies = [GOOD]
    planner.plan("x", planner.path)
    assert "CHECK QUALITY" not in planner.prompts[0]
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v2")
    planner.replies = [GOOD]
    planner.plan("x", planner.path)
    assert "CHECK QUALITY" in planner.prompts[1] and "syntax_ok and file_exists alone are NOT enough" in planner.prompts[1]


def test_model_calls_get_more_attempts_than_task_retries():
    assert config.LLM_MAX_ATTEMPTS == 6 and config.MAX_RETRIES == 3


def test_plan_is_printed_once_per_plan(capsys):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from core.orchestrator import review_plan_node, _print_plan
    plan = json.loads(GOOD)
    _print_plan(plan, plan["steps"])
    assert capsys.readouterr().out.count("GENERATED PLAN") == 1
    import inspect
    assert "GENERATED PLAN" not in inspect.getsource(review_plan_node)   # it re-runs on resume: it must not print the plan
    assert InMemorySaver and Command
