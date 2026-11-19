"""Undo restores the exact bytes even when git's line-ending conversion would change them (LF files with core.autocrlf=true)."""
import hashlib
import subprocess

from core import checkpoints


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git(root, *a):
    subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)


def test_lf_file_under_autocrlf_is_restored_byte_for_byte(git_repo):
    git(git_repo, "config", "core.autocrlf", "true")
    f = git_repo / "a.txt"
    f.write_bytes(b"one\ntwo\n")                                  # LF on disk although autocrlf=true would write CRLF
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "lf")
    checkpoints.snapshot(git_repo, "r1", 1)
    f.write_bytes(b"one\nCHANGED\n")
    change = {"file": "a.txt", "task_id": 1, "sha256_before": sha(b"one\ntwo\n"), "sha256_after": sha(b"one\nCHANGED\n")}
    res = checkpoints.undo(git_repo, "r1", [change])
    assert res.applied and f.read_bytes() == b"one\ntwo\n"


def test_crlf_file_under_autocrlf_still_restores_with_the_filter(git_repo):
    git(git_repo, "config", "core.autocrlf", "true")
    f = git_repo / "b.txt"
    f.write_bytes(b"one\r\ntwo\r\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "crlf")
    checkpoints.snapshot(git_repo, "r2", 1)
    f.write_bytes(b"one\r\nX\r\n")
    change = {"file": "b.txt", "task_id": 1, "sha256_before": sha(b"one\r\ntwo\r\n"), "sha256_after": sha(b"one\r\nX\r\n")}
    assert checkpoints.undo(git_repo, "r2", [change]).applied and f.read_bytes() == b"one\r\ntwo\r\n"
