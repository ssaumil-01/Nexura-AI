"""V2-5.1 / 5.2 / 5.3 / 5.8: project profile, per-workspace allowlist, baseline and regression check."""
import json
import shutil
import sys

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import config
from core import baseline as bl
from core import validator
from core.project_profile import ProjectProfile, apply_overrides, detect_profile, needed_executables
from core.safety import SafetyError, parse_command, valid_extra_names
from core.schemas import Action, ProjectContext
from core.state_manager import StateManager
from tests.conftest import ALWAYS_OK, tool_call_msg


# ─── profile ────────────────────────────────────────────────

def write(root, rel, text):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def test_profile_fastapi_pytest_project(tmp_path):
    write(tmp_path, "requirements.txt", "fastapi==0.110\nuvicorn\npytest\n")
    write(tmp_path, "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write(tmp_path, "tests/test_a.py", "def test_a():\n    pass\n")
    write(tmp_path, ".env.example", "DATABASE_URL=\nAPI_KEY=\nexport DEBUG=1\n")
    p = detect_profile(tmp_path)
    assert p.languages == ["python"] and p.package_managers == ["pip"] and "FastAPI" in p.frameworks
    assert p.entry_points == ["main.py"] and p.run_commands == ["uvicorn main:app"]
    assert p.test_commands == ["python -m pytest"] and p.env_needs == ["DATABASE_URL", "API_KEY", "DEBUG"]
    assert needed_executables(p) == []


def test_profile_poetry_project_needs_poetry(tmp_path):
    write(tmp_path, "pyproject.toml", '[tool.poetry]\nname = "x"\n[tool.poetry.dependencies]\nflask = "*"\n[tool.ruff]\nline-length = 100\n')
    write(tmp_path, "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    write(tmp_path, "tests/test_x.py", "")
    p = detect_profile(tmp_path)
    assert p.package_managers == ["poetry"] and p.frameworks == ["Flask"]
    assert p.test_commands == ["poetry run pytest"] and p.lint_command == "poetry run ruff check ."
    assert p.run_commands == ["poetry run flask --app app run"]
    assert needed_executables(p) == ["poetry"]


def test_profile_node_project_with_yarn(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest", "dev": "vite", "lint": "eslint ."},
                                                "dependencies": {"react": "1"}, "devDependencies": {"vite": "1", "jest": "1"}}))
    write(tmp_path, "yarn.lock", "")
    write(tmp_path, "tsconfig.json", "{}")
    write(tmp_path, "src/main.tsx", "")
    p = detect_profile(tmp_path)
    assert p.languages == ["typescript"] and p.package_managers == ["yarn"] and {"React", "Vite", "Jest"} <= set(p.frameworks)
    assert p.test_commands == ["yarn test"] and p.run_commands == ["yarn dev"] and p.lint_command == "yarn lint"
    assert p.entry_points == ["src/main.tsx"] and needed_executables(p) == ["yarn"]


def test_profile_express_and_npm_defaults(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}, "dependencies": {"express": "4"}}))
    write(tmp_path, "index.js", "")
    p = detect_profile(tmp_path)
    assert p.test_commands == [] and p.run_commands == ["node index.js"] and p.package_managers == ["npm"]


def test_profile_makefile_and_readme_hints(tmp_path):
    write(tmp_path, "Makefile", "test:\n\tpytest\nlint:\n\truff .\n")
    p = detect_profile(tmp_path)
    assert p.test_commands == ["make test"] and p.lint_command == "make lint" and needed_executables(p) == ["make"]
    other = tmp_path / "o"
    write(other, "README.md", "# x\n\nRun the tests:\n\n    python -m pytest -q\n")
    assert detect_profile(other).test_commands == ["python -m pytest -q"]


def test_profile_plain_scripts_project_without_manifest(tmp_path):
    write(tmp_path, "todo.py", "def add(x):\n    return x\n")
    write(tmp_path, "app.py", "import streamlit as st\nfrom todo import add\n")
    write(tmp_path, "test_todo.py", "import pytest\nfrom todo import add\n\ndef test_add():\n    assert add(1) == 1\n")
    p = detect_profile(tmp_path)
    assert p.languages == ["python"] and p.test_commands == ["python -m pytest"]
    assert p.frameworks == ["Streamlit"] and p.run_commands == ["streamlit run app.py"]
    other = tmp_path / "u"
    write(other, "calc.py", "")
    write(other, "test_calc.py", "import unittest\n")
    assert detect_profile(other).test_commands == ["python -m unittest"]


def test_profile_unknown_project_is_empty_and_never_raises(tmp_path):
    write(tmp_path, "notes.txt", "hello")
    write(tmp_path, "package.json", "{not json")
    write(tmp_path, "pyproject.toml", "= broken")
    p = detect_profile(tmp_path)
    assert p.languages == ["python"] and not (p.test_commands or p.run_commands or p.frameworks or p.entry_points or p.lint_command)
    assert detect_profile(tmp_path / "missing-dir") == ProjectProfile()


def test_profile_never_records_secret_values(tmp_path):
    write(tmp_path, ".env.example", "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789\nPORT=3000\n")
    assert detect_profile(tmp_path).env_needs == ["PORT"]


def test_profile_monorepo_lists_subprojects_and_targets_one(tmp_path):
    write(tmp_path, "packages/web/package.json", json.dumps({"scripts": {"test": "vitest"}}))
    write(tmp_path, "packages/api/requirements.txt", "flask\n")
    write(tmp_path, "packages/api/app.py", "from flask import Flask\napp = Flask(__name__)\n")
    root = detect_profile(tmp_path)
    assert root.subprojects == ["packages/api", "packages/web"]
    api = detect_profile(tmp_path, "packages/api")
    assert api.frameworks == ["Flask"] and api.entry_points == ["packages/api/app.py"]
    with pytest.raises(SafetyError):
        detect_profile(tmp_path, "../outside")


def test_overrides_win_field_by_field_and_bad_values_are_ignored():
    p = ProjectProfile(test_commands=["python -m pytest"], run_commands=["python app.py"])
    o = apply_overrides(p, {"test_commands": ["make test"], "run_commands": "oops", "lint_command": "ruff check .", "bogus": 1})
    assert o.test_commands == ["make test"] and o.run_commands == ["python app.py"] and o.lint_command == "ruff check ."


def test_profile_storage_keeps_overrides_across_redetection(tmp_path):
    sm = StateManager(tmp_path / "n.db")
    sm.save_profile("w", {"languages": ["python"]})
    sm.save_profile("w", {"languages": ["python"]}, overrides={"test_commands": ["make test"]})
    sm.save_profile("w", {"languages": ["python", "javascript"]})            # re-detect
    got = sm.get_profile("w")
    assert got["detected"]["languages"] == ["python", "javascript"] and got["overrides"] == {"test_commands": ["make test"]}


# ─── per-workspace allowlist ────────────────────────────────

@pytest.fixture
def fake_exes(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/bin/{name}")


def test_default_allowlist_is_unchanged():
    assert config.ALLOWED_COMMANDS == ["python", "python3", "py", "pip", "pip3", "node", "npm", "npx", "uvicorn", "pytest", "streamlit", "flask"]


def test_extra_executable_needs_the_grant(tmp_path, fake_exes):
    with pytest.raises(SafetyError) as e:
        parse_command("poetry run pytest", tmp_path)
    assert e.value.code == "NOT_ALLOWED" and "approved for this workspace" in e.value.message
    assert parse_command("poetry run pytest", tmp_path, extra_allowed=["poetry"]).name == "poetry"
    with pytest.raises(SafetyError):
        parse_command("yarn test", tmp_path, extra_allowed=["poetry"])        # a grant is per executable


def test_grants_cannot_add_shells_paths_or_operators(tmp_path, fake_exes):
    assert valid_extra_names(["poetry", "bash", "/usr/bin/make", "make; rm", "curl", "YARN", " uv "]) == ["poetry", "yarn", "uv"]
    for cmd in ("bash -c ls", "curl http://x"):
        with pytest.raises(SafetyError):
            parse_command(cmd, tmp_path, extra_allowed=["bash", "curl"])
    with pytest.raises(SafetyError) as e:
        parse_command("make test | tee x", tmp_path, extra_allowed=["make"])
    assert e.value.code == "SHELL_OPERATOR"


def test_runners_cannot_reach_programs_outside_the_allowlist(tmp_path, fake_exes):
    ok = ["poetry run pytest -q", "uv run python -m pytest", "poetry run ruff check .", "pipenv run python app.py"]
    for cmd in ok:
        parse_command(cmd, tmp_path, extra_allowed=["poetry", "uv", "pipenv"])
    for cmd in ("poetry run bash", "uv run curl http://x", "poetry run rm -rf .", "uv run"):
        with pytest.raises(SafetyError) as e:
            parse_command(cmd, tmp_path, extra_allowed=["poetry", "uv"])
        assert e.value.code == "NOT_ALLOWED", cmd


def test_extra_executables_cannot_install_or_publish(tmp_path, fake_exes):
    for cmd in ("poetry install", "poetry add requests", "yarn add left-pad", "pnpm install", "uv pip install x", "poetry run pip install x",
                "yarn publish", "pnpm dlx create-x", "uv sync"):
        with pytest.raises(SafetyError) as e:
            parse_command(cmd, tmp_path, extra_allowed=["poetry", "yarn", "pnpm", "uv"])
        assert e.value.code in ("USE_INSTALL_DEPENDENCY", "NOT_ALLOWED"), cmd
    parse_command("yarn test", tmp_path, extra_allowed=["yarn"])
    parse_command("pnpm run build", tmp_path, extra_allowed=["pnpm"])
    parse_command("make install", tmp_path, extra_allowed=["make"])       # a Makefile target the user approved make for


def test_grants_are_per_workspace_and_revocable(tmp_path, fake_exes):
    sm = StateManager(tmp_path / "n.db")
    sm.grant_command("w1", "poetry")
    sm.grant_command("w1", "poetry")
    assert sm.list_commands("w1") == ["poetry"] and sm.list_commands("w2") == []
    ctx = ProjectContext(run_id="r", root=tmp_path, extra_commands=valid_extra_names(sm.list_commands("w1")))
    assert validator.validate(Action(action="run_command", command="poetry run pytest"), ctx).ok
    other = ProjectContext(run_id="r", root=tmp_path, extra_commands=valid_extra_names(sm.list_commands("w2")))
    assert validator.validate(Action(action="run_command", command="poetry run pytest"), other).code == "NOT_ALLOWED"
    sm.revoke_command("w1", "poetry")
    assert sm.list_commands("w1") == []


# ─── baseline / regression ──────────────────────────────────

PYTEST_BAD = "FAILED tests/test_b.py::test_two - assert 1 == 2\nFAILED tests/test_c.py::test_three\n1 failed, 2 passed in 0.1s\n"


def test_parse_pytest_output():
    r = bl.parse_results(PYTEST_BAD, 1)
    assert r["failing"] == ["tests/test_b.py::test_two", "tests/test_c.py::test_three"] and r["passed"] == 2 and r["failed"] == 2


def result(failing=(), exit_code=None, ran=True):
    return {"ran": ran, "failing": list(failing), "failed": len(failing), "passed": 3, "exit_code": exit_code if exit_code is not None else int(bool(failing))}


def test_compare_new_failure_is_a_regression_old_failure_is_not():
    assert bl.compare(result(), result(["a::t"]))["regressed"]
    same = bl.compare(result(["a::t"]), result(["a::t"]))
    assert not same["regressed"] and "No new test failures" in same["message"]
    mixed = bl.compare(result(["a::t"]), result(["a::t", "b::u"]))
    assert mixed["regressed"] and mixed["new_failures"] == ["b::u"] and "1 test(s) were already failing" in mixed["message"]
    fixed = bl.compare(result(["a::t"]), result())
    assert not fixed["regressed"] and fixed["fixed"] == ["a::t"]


def test_compare_without_test_ids_uses_exit_code_and_counts():
    nb = {"ran": True, "failing": [], "failed": 0, "passed": 5, "exit_code": 0}
    assert bl.compare(nb, {**nb, "exit_code": 1, "failed": 1})["regressed"]
    assert not bl.compare(nb, nb)["regressed"]
    assert not bl.compare({**nb, "exit_code": 1, "failed": 2}, {**nb, "exit_code": 1, "failed": 2})["regressed"]
    assert bl.compare({**nb, "exit_code": 1, "failed": 2}, {**nb, "exit_code": 1, "failed": 3})["regressed"]


def test_compare_skips_when_tests_did_not_run():
    v = bl.compare(result(), {"ran": False, "reason": "timed out after 300s"})
    assert not v["regressed"] and not v["checked"] and "timed out" in v["message"]


CHECK_PY = """import sys
state = open('flag.txt').read().strip()
if state == 'bad':
    print('FAILED tests/test_new.py::test_flag - AssertionError')
    print('1 failed, 2 passed')
    sys.exit(1)
if state == 'old':
    print('FAILED tests/test_old.py::test_legacy')
    print('1 failed, 2 passed')
    sys.exit(1)
print('3 passed')
"""


def run_graph(tmp_path, monkeypatch, fake_llm, git_repo, flag_after, flag_before="good", command="python check.py"):
    import agents.llm as llm_mod
    import agents.planner as planner_mod
    from core.orchestrator import build_graph
    from core.runner import start_run
    monkeypatch.setenv("CODER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("PLANNER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(llm_mod, "_override", fake_llm)
    plan = {"project_id": "p", "goal": "g", "steps": [
        {"step_id": 1, "title": "edit flag", "description": "d", "dependencies": [], "acceptance": ALWAYS_OK}]}
    monkeypatch.setattr(planner_mod.PlannerAgent, "call_llm", lambda self, prompt: json.dumps(plan))
    (git_repo / "check.py").write_text(CHECK_PY)
    (git_repo / "flag.txt").write_text(flag_before)
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=git_repo, check=True)
    fake_llm.script[:] = [tool_call_msg("read_file", {"target_file": "flag.txt"}, "r"),
                          tool_call_msg("write_file", {"target_file": "flag.txt", "content": flag_after}, "w"),
                          tool_call_msg("task_complete", {"summary": "ok"}, "t")]
    sm = StateManager(tmp_path / "data" / "n.db")
    ctx, state = start_run(sm, "ws", git_repo, "x", approval_level="auto_within_limits", baseline_command=command)
    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": ctx.run_id, "project": ctx}, "recursion_limit": 200}
    out = graph.invoke(state, cfg)
    while out.get("__interrupt__"):
        out = graph.invoke(Command(resume="yes" if isinstance(out["__interrupt__"][0].value, str) else {"decision": "approve"}), cfg)
    return sm, ctx, graph.get_state(cfg).values


@pytest.fixture(autouse=True)
def _python_is_this_python(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: sys.executable if name == "python" else real(name, *a, **k))


def events(sm, ctx, kind):
    return [e for e in sm.list_events(ctx.run_id) if e["type"] == kind]


def test_injected_regression_marks_the_run_and_undo_restores(tmp_path, monkeypatch, fake_llm, git_repo):
    from core import runner
    sm, ctx, values = run_graph(tmp_path, monkeypatch, fake_llm, git_repo, flag_after="bad")
    assert events(sm, ctx, "baseline_recorded") and sm.get_baseline(ctx.run_id)["exit_code"] == 0
    (e,) = events(sm, ctx, "regression_detected")
    assert e["data"]["new_failures"] == ["tests/test_new.py::test_flag"]
    assert values["run_status"] == "completed_with_failures" and "Regression" in sm.get_run(ctx.run_id).error
    assert runner.undo(sm, ctx.run_id).applied and (git_repo / "flag.txt").read_text() == "good"


def test_preexisting_failure_is_not_blamed_on_the_agent(tmp_path, monkeypatch, fake_llm, git_repo):
    sm, ctx, values = run_graph(tmp_path, monkeypatch, fake_llm, git_repo, flag_after="old", flag_before="old")
    assert sm.get_baseline(ctx.run_id)["failing"] == ["tests/test_old.py::test_legacy"]
    assert values["run_status"] == "completed" and not events(sm, ctx, "regression_detected") and events(sm, ctx, "regression_check")


def test_no_baseline_command_means_no_extra_test_runs(tmp_path, monkeypatch, fake_llm, git_repo):
    sm, ctx, values = run_graph(tmp_path, monkeypatch, fake_llm, git_repo, flag_after="bad", command=None)
    assert values["run_status"] == "completed" and not events(sm, ctx, "baseline_recorded") and sm.get_baseline(ctx.run_id) is None


def test_unapproved_executable_skips_the_baseline_with_a_notice(tmp_path, monkeypatch, fake_llm, git_repo):
    sm, ctx, values = run_graph(tmp_path, monkeypatch, fake_llm, git_repo, flag_after="bad", command="poetry run pytest")
    (e,) = events(sm, ctx, "baseline_skipped")
    assert "NOT_ALLOWED" in e["data"]["reason"] and values["run_status"] == "completed"
