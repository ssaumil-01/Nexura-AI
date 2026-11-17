import os

import pytest

from core.safety import SafetyError, check_writable, is_protected, is_within, resolve_in_root, truncate


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "..\\x", "/etc/passwd", "C:\\Windows\\x", "C:x", "D:/x", "\\\\srv\\share\\x", "\\x"])
def test_rejects_escapes(workspace, bad):
    with pytest.raises(SafetyError) as e:
        resolve_in_root(workspace, bad)
    assert e.value.code == "OUTSIDE_ROOT"


def test_accepts_inside(workspace):
    assert resolve_in_root(workspace, "a/b.txt") == (workspace.resolve() / "a" / "b.txt")
    assert resolve_in_root(workspace, ".") == workspace.resolve()


def test_prefix_sibling_is_not_inside(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj_evil").mkdir()
    assert is_within(str(tmp_path / "proj"), str(tmp_path / "proj" / "a"))
    assert not is_within(str(tmp_path / "proj"), str(tmp_path / "proj_evil" / "a"))


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitivity")
def test_windows_case_insensitive(workspace):
    assert is_within(str(workspace).upper(), str(workspace / "a"))


def test_symlink_escape(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, workspace / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges")
    with pytest.raises(SafetyError):
        resolve_in_root(workspace, "link/x.txt")


@pytest.mark.parametrize("rel,prot", [(".env", True), (".env.local", True), ("a/.env", True), (".git/config", True),
                                      (".github/workflows/ci.yml", True), ("package-lock.json", True), ("k/x.pem", True),
                                      ("src/env.py", False), (".github/ISSUE.md", False), ("app.py", False)])
def test_protected(rel, prot):
    import config
    assert is_protected(rel, config.PROTECTED_PATHS) is prot


def test_env_and_git_never_writable_even_if_approved():
    import config
    assert check_writable(".env", config.PROTECTED_PATHS, allow_protected=True).code == "NOT_WRITABLE"
    assert check_writable(".git/HEAD", config.PROTECTED_PATHS, allow_protected=True).code == "NOT_WRITABLE"
    assert check_writable("yarn.lock", config.PROTECTED_PATHS).code == "APPROVAL_REQUIRED"
    assert check_writable("yarn.lock", config.PROTECTED_PATHS, allow_protected=True) is None


def test_truncate_head_tail():
    out, cut = truncate("a" * 100 + "b" * 100, 40)
    assert cut and out.startswith("a") and out.endswith("b") and "[truncated]" in out
    assert truncate("short", 40) == ("short", False)
