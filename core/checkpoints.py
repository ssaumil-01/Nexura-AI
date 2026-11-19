"""Git-based checkpoints and undo. Uses the git CLI only (no GitPython).

Checkpoints are commit objects stored under refs/nexura/<run_id>/<task_id>, built from a TEMPORARY index, so HEAD, branches
and the user's index/staging area are never touched. Untracked files are included; .gitignore is respected."""
import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from core.safety import SafetyError, resolve_in_root

IDENTITY = {"GIT_AUTHOR_NAME": "Nexura", "GIT_AUTHOR_EMAIL": "nexura@localhost",
            "GIT_COMMITTER_NAME": "Nexura", "GIT_COMMITTER_EMAIL": "nexura@localhost"}


class GitError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _git(root: Path | str, *args: str, env: dict | None = None, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **IDENTITY, **(env or {})}
    try:
        r = subprocess.run(["git", *args], cwd=str(root), env=full_env, capture_output=True, text=text,
                           **({"encoding": "utf-8", "errors": "replace"} if text else {}))
    except FileNotFoundError:
        raise GitError("GIT_NOT_INSTALLED", "git is not installed or not on PATH.")
    if check and r.returncode != 0:
        err = r.stderr if text else r.stderr.decode("utf-8", "replace")
        raise GitError("GIT_FAILED", f"git {' '.join(args)}: {err.strip()[:300]}")
    return r


# ─── repo state ─────────────────────────────────────────────

def is_repo(root: Path | str) -> bool:
    """True only if `root` IS the top level of a git work tree (not merely inside a parent repo)."""
    try:
        r = _git(root, "rev-parse", "--show-toplevel", check=False)
    except GitError:
        return False
    return r.returncode == 0 and os.path.normcase(os.path.realpath(r.stdout.strip())) == os.path.normcase(os.path.realpath(str(root)))


def require_repo(root: Path | str) -> None:
    if not is_repo(root):
        raise GitError("NOT_A_REPO", f"{root} is not a git repository. Initialize git first so every change can be undone.")


def init_repo(root: Path | str) -> None:
    """User-approved bootstrap: git init + an initial commit of the current files."""
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "Initial commit (created by Nexura)")


def dirty_files(root: Path | str) -> list[str]:
    """Paths with uncommitted changes (modified, staged or untracked), relative with forward slashes."""
    r = _git(root, "status", "--porcelain", "-z", "--untracked-files=all", text=False)
    out = []
    for entry in r.stdout.split(b"\0"):
        if len(entry) > 3:
            out.append(entry[3:].decode("utf-8", "replace"))
    return out


# ─── checkpoints ────────────────────────────────────────────

def ref_name(run_id: str, task_id: int | str) -> str:
    return f"refs/nexura/{run_id}/{task_id}"


def snapshot(root: Path | str, run_id: str, task_id: int | str, message: str = "") -> str:
    """Snapshot the whole working tree (incl. untracked, minus ignored) as a commit under refs/nexura/... Returns the commit id."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        _git(root, "add", "-A", env=env)
        tree = _git(root, "write-tree", env=env).stdout.strip()
        head = _git(root, "rev-parse", "-q", "--verify", "HEAD", check=False)
        parent = ["-p", head.stdout.strip()] if head.returncode == 0 else []
        commit = _git(root, "commit-tree", tree, *parent, "-m", message or f"nexura checkpoint {run_id}/{task_id}").stdout.strip()
    _git(root, "update-ref", ref_name(run_id, task_id), commit)
    return commit


def has_checkpoint(root: Path | str, run_id: str, task_id: int | str) -> bool:
    return _git(root, "rev-parse", "-q", "--verify", ref_name(run_id, task_id), check=False).returncode == 0


def _blob(root, commit: str, path: str, filters: bool = True) -> bytes | None:
    # --filters applies the working-tree conversion (autocrlf / .gitattributes) so the restore is byte-exact
    args = ["cat-file", "--filters", f"--path={path}"] if filters else ["cat-file", "blob"]
    r = _git(root, *args, f"{commit}:{path}", check=False, text=False)
    return r.stdout if r.returncode == 0 else None


# ─── undo ───────────────────────────────────────────────────

@dataclass
class UndoResult:
    applied: bool
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)   # files edited since Nexura's last change
    error: str | None = None


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _plan_undo(changes: list[dict]) -> dict[str, dict]:
    """Per file: the EARLIEST task that touched it (its checkpoint is the state to restore) and the LAST recorded hash."""
    files: dict[str, dict] = {}
    for c in changes:  # ordered by id
        f = files.setdefault(c["file"], {"task_id": c["task_id"], "last_after": None, "first_before": c.get("sha256_before")})
        f["last_after"] = c["sha256_after"]
    return files


def undo(root: Path | str, run_id: str, changes: list[dict], confirm_conflicts: bool = False) -> UndoResult:
    """Restore exactly the files recorded in `changes` from their checkpoints; delete files the run created.
    If a file was edited after Nexura's last change, nothing is applied unless confirm_conflicts=True."""
    root = Path(root)
    if not changes:
        return UndoResult(applied=True)
    plan = _plan_undo(changes)
    try:
        for f, info in plan.items():
            resolve_in_root(root, f)
            if not has_checkpoint(root, run_id, info["task_id"]):
                return UndoResult(applied=False, error=f"No checkpoint for task {info['task_id']} of run {run_id}.")
    except SafetyError as e:
        return UndoResult(applied=False, error=str(e))

    conflicts = [f for f, info in plan.items() if _sha(root / f) != info["last_after"]]
    if conflicts and not confirm_conflicts:
        return UndoResult(applied=False, conflicts=conflicts)

    res = UndoResult(applied=True, conflicts=conflicts)
    for f, info in plan.items():
        commit = _git(root, "rev-parse", ref_name(run_id, info["task_id"])).stdout.strip()
        target = resolve_in_root(root, f)
        before = _blob(root, commit, f)
        want = info.get("first_before")
        if before is not None and want and hashlib.sha256(before).hexdigest() != want:
            # the working-tree conversion would change the bytes (e.g. LF files while core.autocrlf=true): use the stored bytes
            raw = _blob(root, commit, f, filters=False)
            if raw is not None and hashlib.sha256(raw).hexdigest() == want:
                before = raw
        if before is None:                      # did not exist before the task: remove it
            if target.exists():
                target.unlink()
            res.deleted.append(f)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(before)
            res.restored.append(f)
    return res
