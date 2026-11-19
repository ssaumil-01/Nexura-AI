"""V2 security audit (§7 of the plan): the items that are not already covered by the indexer/retrieval/API test files."""
import os
import shutil
import sys

import pytest

import config
from core import approvals, baseline, validator
from core.project_profile import detect_profile
from core.schemas import Action, ProjectContext
from tests.test_execution_backend_contract import RecordingBackend


@pytest.fixture
def ctx(workspace, monkeypatch):
    monkeypatch.setattr(config, "PATCH_MODE", "v2")
    return ProjectContext(run_id="r", root=workspace)


def patch(path, s="a", r="b"):
    return Action(action="apply_patch", path=path, metadata={"hunks": [{"search_text": s, "replace_text": r}]})


# 1. path confinement for patching
@pytest.mark.parametrize("path", ["../outside.txt", "..\\outside.txt", "/etc/passwd", "C:/Windows/win.ini", "\\\\server\\share\\x", "sub/../../x"])
def test_apply_patch_rejects_escapes(ctx, path):
    r = validator.validate(patch(path), ctx)
    assert not r.ok and r.code in ("OUTSIDE_ROOT", "ABSOLUTE_PATH", "BAD_PATH", "FILE_NOT_FOUND") and r.code != "READ_REQUIRED"


def test_apply_patch_rejects_symlink_escape(ctx, workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("a")
    link = workspace / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    assert not validator.validate(patch("link.txt"), ctx).ok
    assert outside.read_text() == "a"


# 5/6. approvals stay as for write_file
@pytest.mark.parametrize("name", ["package.json", "requirements.txt", "pyproject.toml", "tsconfig.json"])
def test_patching_a_manifest_or_config_file_is_risky(ctx, workspace, name):
    (workspace / name).write_text("a\n")
    a = patch(name)
    reasons = approvals.assess(a, approvals.preview(a, ctx), ctx, set(), 0)
    assert any(kind == "risky" for kind, _ in reasons)
    assert approvals.requires_approval(reasons, "ask_risky")


def test_a_retrieved_file_still_needs_to_be_read_before_patching(ctx, workspace):
    (workspace / "a.py").write_text("a = 1\n")
    assert validator.validate(patch("a.py", "a = 1", "a = 2"), ctx).code == "READ_REQUIRED"


# 8. backend seam: the baseline test run is validated before the backend and goes through it
@pytest.fixture
def rec_ctx(workspace, monkeypatch):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: sys.executable if n == "python" else real(n, *a, **k))
    c = ProjectContext(run_id="r", root=workspace, backend=RecordingBackend())
    yield c
    c.backend.stop_all()


def test_baseline_uses_the_backend_only_after_validation(rec_ctx):
    (rec_ctx.root / "ok.py").write_text("print('3 passed')\n")
    res = baseline.run_tests(rec_ctx, "python ok.py")
    assert res["ran"] and res["passed"] == 3 and [c[0] for c in rec_ctx.backend.calls] == ["run"]
    rec_ctx.backend.calls.clear()
    for bad in ("curl http://example.com", "python ok.py | more", "poetry run pytest", "pip install requests"):
        res = baseline.run_tests(rec_ctx, bad)
        assert not res["ran"] and "NOT_ALLOWED" in res["reason"] or "SHELL_OPERATOR" in res["reason"] or "USE_INSTALL" in res["reason"], bad
    assert rec_ctx.backend.calls == []


def test_a_baseline_cannot_widen_the_allowlist(rec_ctx):
    rec_ctx.extra_commands = ["poetry"]
    for cmd in ("poetry install", "poetry run bash", "poetry add x"):
        assert not baseline.run_tests(rec_ctx, cmd)["ran"], cmd
    assert rec_ctx.backend.calls == []


def test_baseline_output_has_no_absolute_workspace_path(rec_ctx):
    (rec_ctx.root / "fail.py").write_text("import sys\nprint('FAILED tests/x.py::t')\nprint(__file__)\nsys.exit(1)\n")
    res = baseline.run_tests(rec_ctx, "python fail.py")
    assert res["ran"] and str(rec_ctx.root) not in res["output"] and res["failing"] == ["tests/x.py::t"]


def test_the_baseline_never_runs_without_a_command(rec_ctx):
    # asked for but nothing to run: recorded as skipped by the orchestrator, no process is started
    assert rec_ctx.baseline_command is None and rec_ctx.backend.calls == []


# profile: detection reads only inside the workspace and returns names, not values
def test_profile_detection_stays_inside_the_workspace(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "package.json").write_text('{"scripts": {"test": "jest"}}')
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "packages").mkdir()
    try:
        os.symlink(outside, ws / "packages" / "evil", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    p = detect_profile(ws)
    assert "packages/evil" not in p.subprojects
