"""Phase 3: validator, execution manager, verification, retry/debug loop, tool isolation, context budget."""
import ast
import socket
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

import config
from core import execution_manager as em
from core import validator, verification
from core.context_builder import build_context
from core.error_classifier import classify
from core.retry_manager import decide
from core.schemas import Action, ProjectContext
from tests.conftest import tool_call_msg
from tests.test_bug_repros import done, g, values  # noqa: F401  (fixtures/helpers)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ctx(workspace):
    c = ProjectContext(run_id="r", root=workspace)
    yield c
    c.processes.stop_all()


def A(name, **kw):
    return Action(action=name, **kw)


# ─── validator ──────────────────────────────────────────────

@pytest.mark.parametrize("action,code", [
    (A("create_file", path="a.py"), "BAD_SCHEMA"),                                  # missing content
    (A("rm_rf", path="x"), "UNKNOWN_ACTION"),
    (A("create_file", path="../x", content="1"), "OUTSIDE_ROOT"),
    (A("create_file", path="plan.json", content="{}"), "PLAN_IMMUTABLE"),
    (A("create_file", path=".env", content="K=1"), "NOT_WRITABLE"),
    (A("create_file", path="yarn.lock", content="1"), "APPROVAL_REQUIRED"),
    (A("run_command", command="python a.py | more"), "SHELL_OPERATOR"),
    (A("run_command", command="curl http://x"), "NOT_ALLOWED"),
    (A("write_file", path="nope.py", content="1"), "FILE_NOT_FOUND"),
    (A("batch_file_operations", metadata={"operations": [{"operation": "create", "target_file": f"f{i}", "content": ""} for i in range(11)]}), "BATCH_TOO_LARGE"),
])
def test_validator_rejects(ctx, action, code):
    r = validator.validate(action, ctx)
    assert not r.ok and r.code == code, r


def test_validator_read_before_write_and_batch_simulation(ctx, workspace):
    (workspace / "old.py").write_text("x")
    assert validator.validate(A("write_file", path="old.py", content="y"), ctx).code == "READ_REQUIRED"
    ctx.files_read.add(ctx.key(workspace / "old.py"))
    assert validator.validate(A("write_file", path="old.py", content="y"), ctx).ok
    assert validator.validate(A("create_file", path="old.py", content="y"), ctx).code == "FILE_EXISTS"
    batch = A("batch_file_operations", metadata={"operations": [
        {"operation": "create", "target_file": "n.py", "content": "1"},
        {"operation": "patch", "target_file": "n.py", "search_text": "1", "replace_text": "2"}]})
    assert validator.validate(batch, ctx).ok  # patch of a file created earlier in the same batch


def test_validator_debug_mode_restrictions(ctx, workspace):
    (workspace / "a.py").write_text("x")
    ctx.files_read.add(ctx.key(workspace / "a.py"))
    assert validator.validate(A("create_file", path="n.py", content="1"), ctx, "debug").code == "NOT_ALLOWED_IN_DEBUG"
    assert validator.validate(A("install_dependency", command="pip install x"), ctx, "debug").code == "NOT_ALLOWED_IN_DEBUG"
    assert validator.validate(A("task_complete", metadata={"summary": "x"}), ctx, "debug").code == "NOT_ALLOWED_IN_DEBUG"
    assert validator.validate(A("write_file", path="missing.py", content="1"), ctx, "debug").code in ("FILE_NOT_FOUND", "DEBUG_NEW_FILE")
    assert validator.validate(A("patch_file", path="a.py", search_text="x", replace_text="y"), ctx, "debug").ok


# ─── execution manager ──────────────────────────────────────

def test_execution_manager_records_file_changes_with_hashes(ctx, workspace):
    r = em.run(A("create_file", path="a.py", content="x=1\n"), ctx, task_id=3)
    assert r.ok and len(r.file_changes) == 1
    fc = r.file_changes[0]
    assert (fc.file, fc.change_type, fc.task_id, fc.sha256_before) == ("a.py", "created", 3, None) and len(fc.sha256_after) == 64
    r2 = em.run(A("patch_file", path="a.py", search_text="1", replace_text="2"), ctx, task_id=3)
    assert r2.ok and r2.file_changes[0].change_type == "modified" and r2.file_changes[0].sha256_before == fc.sha256_after


def test_execution_manager_never_raises_and_reports_errors(ctx):
    r = em.run(A("create_file", path="../x", content="1"), ctx)
    assert not r.ok and r.code == "OUTSIDE_ROOT" and r.text().startswith("ERROR: OUTSIDE_ROOT")
    assert em.run(A("bogus"), ctx).code == "UNKNOWN_ACTION"
    assert not em.run(A("create_file"), ctx).ok  # missing fields: tool raises -> structured error
    r = em.run(A("run_command", command="python -c \"raise SystemExit(4)\""), ctx)
    assert not r.ok and r.exit_code == 4 and r.code == "COMMAND_FAILED"


def test_failed_write_records_no_change(ctx, workspace):
    (workspace / "a.py").write_text("x")
    r = em.run(A("write_file", path="a.py", content="y"), ctx)  # READ_REQUIRED
    assert not r.ok and r.file_changes == []


def test_tools_only_imported_by_execution_manager():
    """No code path may call a tool outside core/execution_manager.py."""
    offenders = []
    for folder in ("core", "agents"):
        for f in (ROOT / folder).glob("*.py"):
            if f.name == "execution_manager.py":
                continue
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                mods = [node.module] if isinstance(node, ast.ImportFrom) and node.module else \
                       [a.name for a in node.names] if isinstance(node, ast.Import) else []
                offenders += [f"{f.name}: {m}" for m in mods if m == "tools" or m.startswith("tools.")]
    for f in (ROOT / "main.py", ROOT / "server.py"):
        assert "tools." not in f.read_text(encoding="utf-8").replace("# ", ""), f.name
    assert offenders == []


# ─── verification ───────────────────────────────────────────

def task(*acc):
    return {"step_id": 1, "title": "t", "description": "d", "acceptance": list(acc)}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_verify_each_acceptance_type(ctx, workspace):
    (workspace / "ok.py").write_text("print('hello 42')\n")
    (workspace / "bad.py").write_text("def (:\n")
    v = lambda *acc: verification.verify(task(*acc), ctx, [], [])  # noqa: E731
    assert v({"type": "file_exists", "path": "ok.py"}).ok
    assert not v({"type": "file_exists", "path": "nope.py"}).ok
    assert v({"type": "syntax_ok", "paths": ["ok.py"]}).ok
    r = v({"type": "syntax_ok", "paths": ["ok.py", "bad.py"]})
    assert not r.ok and "bad.py" in r.failure_text()
    assert v({"type": "command_ok", "command": "python ok.py"}).ok
    assert not v({"type": "command_ok", "command": "python bad.py"}).ok
    assert v({"type": "output_contains", "command": "python ok.py", "text": "42"}).ok
    assert not v({"type": "output_contains", "command": "python ok.py", "text": "999"}).ok
    assert not v({"type": "command_ok", "command": "rm -rf ."}).ok       # acceptance commands are allowlisted too
    assert not v({"type": "file_exists", "path": "../x"}).ok


def test_verify_http_status_starts_and_cleans_up(ctx):
    port = free_port()
    r = verification.verify(task({"type": "http_status", "start_command": f"python -m http.server {port} --bind 127.0.0.1",
                                  "url": f"http://127.0.0.1:{port}/", "expect": 200, "wait_s": 15}), ctx, [], [])
    assert r.ok, r.failure_text()
    assert len(ctx.processes) == 0
    bad = verification.verify(task({"type": "http_status", "url": "http://example.com/", "expect": 200}), ctx, [], [])
    assert not bad.ok and "localhost" in bad.failure_text()
    down = verification.verify(task({"type": "http_status", "url": f"http://127.0.0.1:{free_port()}/", "wait_s": 1}), ctx, [], [])
    assert not down.ok


def test_verify_default_checks_syntax_and_last_command_result(ctx, workspace):
    (workspace / "bad.py").write_text("def (:\n")
    (workspace / "ok.py").write_text("x = 1\n")
    assert not verification.verify(task(), ctx, ["bad.py"], []).ok
    assert verification.verify(task(), ctx, ["ok.py"], []).ok
    cmds = [{"command": "python t.py", "exit_code": 1}, {"command": "python t.py", "exit_code": 0}]
    assert verification.verify(task(), ctx, [], cmds).ok           # fixed and re-run: last result counts
    assert not verification.verify(task(), ctx, [], cmds[::-1]).ok


# ─── retry manager / classifier ─────────────────────────────

def test_retry_decisions():
    assert [decide({}, n, "boom").action for n in (1, 2, 3, 4)] == ["retry", "retry_with_error", "switch_strategy", "fail"]
    assert "boom" in decide({}, 2, "boom").hint and "from scratch" in decide({}, 3, "boom").hint


@pytest.mark.parametrize("text,cat", [
    ("ModuleNotFoundError: No module named 'flask'", "missing_dependency"), ("ImportError: cannot import name x", "import_error"),
    ("  File x.py\nSyntaxError: invalid syntax", "syntax_error"), ("OSError: [Errno 98] Address already in use", "port_in_use"),
    ("AssertionError: 1 != 2", "test_failure"), ("something odd", "unknown")])
def test_error_classifier(text, cat):
    assert classify(text) == cat


# ─── orchestrated flows (fake LLM) ──────────────────────────

def one_step(acceptance):
    return {"project_id": "p", "goal": "g", "steps": [
        {"step_id": 1, "title": "hello", "description": "make hello.py", "dependencies": [], "acceptance": acceptance}]}


CMD_OK = [{"type": "command_ok", "command": "python hello.py"}]


def create_buggy_and_complete():
    return AIMessage(content="", tool_calls=[
        {"name": "create_file", "args": {"target_file": "hello.py", "content": "print(undefined_name)\n"}, "id": "a"},
        {"name": "task_complete", "args": {"summary": "done"}, "id": "b"}])


def test_invalid_action_rejected_then_retry_succeeds(g, fake_llm, workspace):  # noqa: F811
    g.holder["plan"] = one_step([])
    fake_llm.script[:] = [tool_call_msg("create_file", {"target_file": "../evil.py", "content": "1"}, "a"), *done(1)]
    v = values(g())
    assert v["task_status"] == {"1": "done"} and not (workspace.parent / "evil.py").exists()
    assert "OUTSIDE_ROOT" in str(fake_llm.calls[1][-1].content)   # rejection was fed back to the agent


def test_repeated_rejections_fail_the_task(g, fake_llm):  # noqa: F811
    g.holder["plan"] = one_step([])
    fake_llm.script[:] = [tool_call_msg("create_file", {"target_file": "../e.py", "content": "1"}, f"c{i}") for i in range(6)]
    v = values(g())
    assert v["task_errors"]["1"].startswith("VALIDATION_REJECTED") and v["run_status"] == "failed"
    assert len(fake_llm.calls) == config.MAX_RETRIES + 1


def test_command_failure_goes_to_debugger_then_verifies_and_completes(g, fake_llm, workspace):  # noqa: F811
    g.holder["plan"] = one_step(CMD_OK)
    fix = tool_call_msg("patch_file", {"target_file": "hello.py", "search_text": "undefined_name", "replace_text": "'hi'"}, "f")
    fake_llm.script[:] = [create_buggy_and_complete(), fix]
    v = values(g())
    assert v["task_status"] == {"1": "done"} and v["run_status"] == "completed"
    assert "'hi'" in (workspace / "hello.py").read_text()
    assert "Debugger Agent" in str(fake_llm.calls[1][0].content) and "NameError" in str(fake_llm.calls[1][0].content)
    assert [h["status"] for h in v["execution_history"] if h["tool"] == "verification"] == ["error", "ok"]
    assert any(h.get("error_type") for h in v["execution_history"])


def test_task_complete_is_only_a_request(g, fake_llm):  # noqa: F811
    g.holder["plan"] = one_step([{"type": "file_exists", "path": "never_created.py"}])
    fake_llm.script[:] = done(1) + [tool_call_msg("list_directory", {"target_dir": "."}, f"x{i}") for i in range(10)]
    v = values(g())
    assert v["task_status"]["1"] == "failed" and v["run_status"] == "failed"


def test_repeated_failure_marks_failed_and_run_completed_with_failures(g, fake_llm, workspace, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "FAILURE_POLICY", "continue")
    plan = one_step(CMD_OK)
    plan["steps"].append({"step_id": 2, "title": "other", "description": "d", "dependencies": [], "acceptance": []})
    g.holder["plan"] = plan
    useless = [tool_call_msg("run_command", {"command": "python hello.py"}, f"r{i}") for i in range(3)]
    fake_llm.script[:] = [create_buggy_and_complete(), *useless, *done(1)]
    v = values(g())
    assert v["task_status"] == {"1": "failed", "2": "done"}
    assert v["task_errors"]["1"].startswith("VERIFICATION_FAILED")
    assert v["run_status"] == "completed_with_failures"
    import main
    assert main.print_summary(v) == 1


def test_debugger_cannot_create_new_files(g, fake_llm, workspace):  # noqa: F811
    g.holder["plan"] = one_step(CMD_OK)
    fake_llm.script[:] = [create_buggy_and_complete(),
                          tool_call_msg("create_file", {"target_file": "new.py", "content": "1"}, "n"),
                          tool_call_msg("patch_file", {"target_file": "hello.py", "search_text": "undefined_name", "replace_text": "1"}, "p")]
    v = values(g())
    assert not (workspace / "new.py").exists() and v["task_status"]["1"] == "done"
    assert "NOT_ALLOWED_IN_DEBUG" in str(fake_llm.calls[2][-1].content)


# ─── context builder ────────────────────────────────────────

def test_context_priorities_budget_and_ignores(workspace):
    (workspace / ".gitignore").write_text("secret_dir/\n*.log\n")
    (workspace / "secret_dir").mkdir()
    (workspace / "secret_dir" / "a.py").write_text("HIDDEN")
    (workspace / "x.log").write_text("HIDDEN")
    (workspace / ".env").write_text("API_KEY=HIDDEN")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "m.js").write_text("HIDDEN")
    (workspace / "target.py").write_text("TARGET_BODY\n")
    (workspace / "changed.py").write_text("CHANGED_BODY\n")
    (workspace / "big.py").write_text("\n".join(f"line{i}" for i in range(500)))
    text, b = build_context({"title": "edit target.py", "description": ""}, workspace, ["changed.py"])
    assert "HIDDEN" not in text and "API_KEY" not in text
    assert "TARGET_BODY" in text and "CHANGED_BODY" in text
    assert "line10" in text and "line400" not in text and "more lines" in text   # big.py: head only
    assert b["named"] == 1 and b["changed"] == 1 and text.index("TARGET_BODY") < text.index("CHANGED_BODY")
    small, b2 = build_context({"title": "edit target.py", "description": ""}, workspace, ["changed.py"], budget=400)
    assert len(small) <= 400 + 200 and b2["omitted_for_budget"] > 0 and "TARGET_BODY" in small
