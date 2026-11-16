import json
import threading

import pytest

from core.schemas import ProjectContext
from tools.directory_tools import create_directory, list_directory
from tools.file_tools import batch_file_operations, create_file, patch_file, read_file, write_file
from tools.verification_tools import verify_code_syntax


@pytest.fixture
def ctx(workspace):
    return ProjectContext(run_id="r", root=workspace)


def call(tool, ctx, **args):
    return tool.invoke(args, {"configurable": {"project": ctx}})


def test_tool_schema_hides_config():
    assert "config" not in create_file.args


def test_create_then_exists_fails(ctx):
    assert call(create_file, ctx, target_file="a.py", content="x=1\n").startswith("Successfully")
    assert "FILE_EXISTS" in call(create_file, ctx, target_file="a.py", content="y")


def test_write_requires_existing_and_read(ctx, workspace):
    assert "FILE_NOT_FOUND" in call(write_file, ctx, target_file="n.py", content="x")
    (workspace / "old.py").write_text("old")
    assert "READ_REQUIRED" in call(write_file, ctx, target_file="old.py", content="new")
    assert "READ_REQUIRED" in call(patch_file, ctx, target_file="old.py", search_text="old", replace_text="n")
    assert call(read_file, ctx, target_file="old.py") == "old"
    assert call(patch_file, ctx, target_file="old.py", search_text="old", replace_text="new").startswith("Successfully")
    assert call(write_file, ctx, target_file="old.py", content="newer").startswith("Successfully")
    assert (workspace / "old.py").read_text() == "newer"


def test_created_file_rewritable_same_task_not_after_reset(ctx):
    call(create_file, ctx, target_file="a.py", content="1")
    assert call(write_file, ctx, target_file="a.py", content="2").startswith("Successfully")
    ctx.reset_task()
    assert "READ_REQUIRED" in call(write_file, ctx, target_file="a.py", content="3")


def test_batch_structured_and_same_rules(ctx, workspace):
    (workspace / "old.py").write_text("old")
    out = json.loads(call(batch_file_operations, ctx, operations=[
        {"operation": "create", "target_file": "new.py", "content": "x"},
        {"operation": "create", "target_file": "new.py", "content": "x"},
        {"operation": "write", "target_file": "old.py", "content": "z"},
        {"operation": "bogus", "target_file": "q"},
        {"operation": "create", "target_file": "../esc.py", "content": "x"}]))
    assert [o["ok"] for o in out] == [True, False, False, False, False]
    assert [o["code"] for o in out[1:]] == ["FILE_EXISTS", "READ_REQUIRED", "BAD_OPERATION", "OUTSIDE_ROOT"]


def test_size_binary_protected_limits(ctx, workspace):
    ctx.limits.max_file_bytes = 10
    assert "TOO_LARGE" in call(create_file, ctx, target_file="big.txt", content="x" * 11)
    assert "BINARY_CONTENT" in call(create_file, ctx, target_file="b.bin", content="a\x00b")
    assert "NOT_WRITABLE" in call(create_file, ctx, target_file=".env", content="K=1")
    assert "NOT_WRITABLE" in call(create_file, ctx, target_file=".git/hooks/x", content="1")
    assert "APPROVAL_REQUIRED" in call(create_file, ctx, target_file="yarn.lock", content="1")
    assert "NOT_WRITABLE" in call(create_directory, ctx, target_dir=".git/x")
    assert not (workspace / ".env").exists()
    ctx.allow_protected = True
    assert call(create_file, ctx, target_file="yarn.lock", content="1").startswith("Successfully")
    assert "NOT_WRITABLE" in call(create_file, ctx, target_file=".env", content="1")


def test_traversal_in_all_tools(ctx):
    for tool, args in [(create_file, dict(target_file="../x", content="1")), (read_file, dict(target_file="../x")),
                       (list_directory, dict(target_dir="..")), (create_directory, dict(target_dir="../d")),
                       (verify_code_syntax, dict(file_path="../x.py")), (verify_code_syntax, dict(file_path="C:\\Windows\\x.py"))]:
        assert "OUTSIDE_ROOT" in call(tool, ctx, **args), tool.name


def test_two_projects_concurrently_no_cross_writes(tmp_path):
    ctxs = []
    for n in "ab":
        (tmp_path / n).mkdir()
        ctxs.append(ProjectContext(run_id=n, root=tmp_path / n))

    def work(c):
        for i in range(30):
            call(create_file, c, target_file=f"{c.run_id}{i}.txt", content=c.run_id)
    ts = [threading.Thread(target=work, args=(c,)) for c in ctxs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    for n in "ab":
        files = list((tmp_path / n).iterdir())
        assert len(files) == 30 and all(f.read_text() == n and f.name.startswith(n) for f in files)
