"""V2-5.6/5.7: apply_patch through the validator, execution manager and approval preview (PATCH_MODE=v2); V1 mode unchanged."""
import pytest

import config
from core import approvals, validator
from core import execution_manager as em
from core.schemas import Action, ProjectContext


@pytest.fixture
def ctx(workspace, monkeypatch):
    monkeypatch.setattr(config, "PATCH_MODE", "v2")
    return ProjectContext(run_id="r", root=workspace)


def patch_action(path, *pairs):
    return Action(action="apply_patch", path=path, metadata={"hunks": [{"search_text": s, "replace_text": r} for s, r in pairs]})


def read(ctx, path):
    assert em.run(Action(action="read_file", path=path), ctx).ok


def test_v1_mode_does_not_know_apply_patch(workspace, monkeypatch):
    monkeypatch.setattr(config, "PATCH_MODE", "v1")
    (workspace / "a.py").write_text("x = 1\n")
    r = validator.validate(patch_action("a.py", ("x = 1", "x = 2")), ProjectContext(run_id="r", root=workspace))
    assert not r.ok and r.code == "NOT_ALLOWED"
    assert "apply_patch" not in [t.name for t in em.tool_schemas()]
    monkeypatch.setattr(config, "PATCH_MODE", "v2")
    assert "apply_patch" in [t.name for t in em.tool_schemas()]


def test_apply_patch_end_to_end_keeps_crlf(ctx, workspace):
    (workspace / "a.py").write_bytes(b"def f():\r\n    return 1\r\n\r\ndef g():\r\n    return 2\r\n")
    a = patch_action("a.py", ("return 1", "return 10"), ("return 2", "return 20"))
    assert validator.validate(a, ctx).code == "READ_REQUIRED"          # read-before-write holds
    read(ctx, "a.py")
    assert validator.validate(a, ctx).ok
    res = em.run(a, ctx)
    assert res.ok and [c.change_type for c in res.file_changes] == ["modified"]
    assert (workspace / "a.py").read_bytes() == b"def f():\r\n    return 10\r\n\r\ndef g():\r\n    return 20\r\n"


def test_unappliable_patch_is_rejected_by_the_validator_not_the_user(ctx, workspace):
    (workspace / "a.py").write_text("print(1)\nprint(1)\n")
    read(ctx, "a.py")
    r = validator.validate(patch_action("a.py", ("print(1)", "print(2)")), ctx)
    assert r.code == "PATCH_AMBIGUOUS" and "1, 2" in r.message
    assert validator.validate(patch_action("a.py", ("nope", "x")), ctx).code == "PATCH_NO_MATCH"
    assert validator.validate(Action(action="apply_patch", path="a.py", metadata={"hunks": "x"}), ctx).code == "BAD_HUNK"
    assert (workspace / "a.py").read_text() == "print(1)\nprint(1)\n"


def test_multi_hunk_failure_leaves_the_file_untouched(ctx, workspace):
    (workspace / "a.py").write_text("a = 1\nb = 2\n")
    read(ctx, "a.py")
    res = em.run(patch_action("a.py", ("a = 1", "a = 9"), ("zzz", "q")), ctx)
    assert not res.ok and res.code == "PATCH_NO_MATCH"
    assert (workspace / "a.py").read_text() == "a = 1\nb = 2\n" and res.file_changes == []


def test_protected_path_needs_approval_like_write_file(ctx, workspace):
    (workspace / ".github" / "workflows").mkdir(parents=True)
    (workspace / ".github" / "workflows" / "ci.yml").write_text("a: 1\n")
    read(ctx, ".github/workflows/ci.yml")
    a = patch_action(".github/workflows/ci.yml", ("a: 1", "a: 2"))
    w = Action(action="write_file", path=".github/workflows/ci.yml", content="a: 2\n")
    assert validator.validate(a, ctx).code == validator.validate(w, ctx).code == "APPROVAL_REQUIRED"
    reasons = approvals.assess(a, approvals.preview(a, ctx), ctx, set(), 0)
    assert ("hard", "protected path: .github/workflows/ci.yml") in reasons


def test_preview_is_the_real_multi_hunk_diff(ctx, workspace):
    body = "".join(f"line{i}\n" for i in range(30))
    (workspace / "m.txt").write_text(body)
    a = patch_action("m.txt", ("line2\n", "LINE2\n"), ("line25\n", "LINE25\n"))
    (p,) = approvals.preview(a, ctx)
    assert p["change_type"] == "modified" and p["diff"].count("\n@@") == 2 and p["lines"] == 4
    read(ctx, "m.txt")
    em.run(a, ctx)
    assert (workspace / "m.txt").read_text() == p["after"]            # what was shown is what was written


def test_dirty_file_is_refused(ctx, workspace):
    (workspace / "a.py").write_text("x = 1\n")
    ctx.dirty_files = {"a.py"}
    read(ctx, "a.py")
    assert validator.validate(patch_action("a.py", ("x = 1", "x = 2")), ctx).code == "DIRTY_FILE"


def test_patch_file_uses_the_same_engine_in_v2(ctx, workspace):
    (workspace / "a.py").write_text("print(1)\nprint(1)\n")
    read(ctx, "a.py")
    r = validator.validate(Action(action="patch_file", path="a.py", search_text="print(1)", replace_text="x"), ctx)
    assert r.code == "PATCH_AMBIGUOUS"
