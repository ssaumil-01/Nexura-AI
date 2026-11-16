"""Shared run setup for the CLI and the API: start a run, rebuild one for resume, undo."""
import uuid
from pathlib import Path

import config
from core import checkpoints
from core.runlog import RunLogger
from core.safety import load_ignore_patterns
from core.schemas import ProjectContext
from core.state_manager import StateManager
from core.checkpoints import GitError, UndoResult  # noqa: F401  (re-exported for callers)


class ResumeError(RuntimeError):
    pass


class DirtyWorkspace(RuntimeError):
    def __init__(self, files: list[str]):
        super().__init__(f"The workspace has uncommitted changes ({len(files)} files) and dirty_policy is 'abort'.")
        self.files = files


def initial_state(prompt: str, root: str | Path, interactive: bool, **extra) -> dict:
    return {
        "messages": [], "user_prompt": prompt, "workspace_path": str(root), "plan": None, "tasks": [],
        "current_task_index": 0, "execution_history": [], "is_interactive": interactive, "plan_feedback": None,
        "tool_calls_count": 0, "task_status": {}, "task_errors": {}, "run_status": None, "run_error": None,
        "task_outcome": None, "task_reason": None, "replan_count": 0, "file_changes": [], "resuming": False,
        **extra,
    }


def _context(sm: StateManager, run_id: str, project_id: str, root: Path, approval_level: str | None, trusted: bool,
             editor_dirty: list[str], git: bool) -> ProjectContext:
    run_dir = config.DATA_DIR / "runs" / run_id
    return ProjectContext(
        run_id=run_id, project_id=project_id, root=root, run_dir=run_dir, state_manager=sm,
        logger=RunLogger(run_dir / "log.jsonl", project_id, run_id),
        approval_level=approval_level or config.APPROVAL_LEVEL, trusted=trusted,
        dirty_files={d.replace("\\", "/").lower() for d in editor_dirty},
        ignore_patterns=load_ignore_patterns(root, include_defaults=False), git_checkpoints=git)


def _prepare_git(root: Path, run_id: str, dirty_policy: str, git_init: bool) -> None:
    if not checkpoints.is_repo(root):
        if not git_init:
            raise GitError("NOT_A_REPO", f"{root} is not a git repository. Initialize git so every change can be undone.")
        checkpoints.init_repo(root)
    dirty = checkpoints.dirty_files(root)
    if dirty and dirty_policy == "abort":
        raise DirtyWorkspace(dirty)
    if dirty:   # dirty_policy == "checkpoint": remember the user's uncommitted state before anything changes
        checkpoints.snapshot(root, run_id, "base", "nexura base checkpoint (uncommitted changes at run start)")


def start_run(sm: StateManager, project_id: str, root: str | Path, prompt: str, interactive: bool = False,
              approval_level: str | None = None, *, require_git: bool = True, git_init: bool = False,
              dirty_policy: str = "checkpoint", dirty_files: list[str] | None = None,
              trusted: bool = True) -> tuple[ProjectContext, dict]:
    """Raises GitError(NOT_A_REPO), DirtyWorkspace, or WorkspaceBusy. `require_git=False` exists for unit tests only."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    sm.upsert_project(project_id, str(root))
    sm.acquire_lock(root, run_id)   # one active run per workspace
    try:
        if require_git:
            _prepare_git(root, run_id, dirty_policy, git_init)
        sm.create_run(run_id, project_id, prompt, approval_level or config.APPROVAL_LEVEL)
    except BaseException:
        sm.release_lock(run_id)
        raise
    ctx = _context(sm, run_id, project_id, root, approval_level, trusted, dirty_files or [], require_git)
    ctx.emit("run_started", prompt=prompt[:200])
    return ctx, initial_state(prompt, root, interactive)


def prepare_resume(sm: StateManager, run_id: str, interactive: bool = False, *, require_git: bool = True,
                   dirty_files: list[str] | None = None, trusted: bool = True) -> tuple[ProjectContext, dict]:
    """Reload a run: keep `done` tasks, restart everything else with a fresh attempt count."""
    run = sm.get_run(run_id)
    if run is None:
        raise ResumeError(f"No such run: {run_id}")
    if not run.plan:
        raise ResumeError(f"Run {run_id} has no saved plan; start a new run instead.")
    root = sm.project_path(run.project_id)
    if not root or not Path(root).is_dir():
        raise ResumeError(f"Workspace for run {run_id} no longer exists: {root}")

    rows = {r["task_id"]: r for r in sm.list_tasks(run_id)}
    steps = run.plan["steps"]
    status = {str(s["step_id"]): "done" if rows.get(s["step_id"], {}).get("status") == "done" else "pending" for s in steps}
    first = next((i for i, s in enumerate(steps) if status[str(s["step_id"])] != "done"), None)
    if first is None:
        raise ResumeError(f"Run {run_id} has no unfinished tasks.")

    sm.acquire_lock(root, run_id)
    if require_git and not checkpoints.is_repo(Path(root)):
        sm.release_lock(run_id)
        raise GitError("NOT_A_REPO", f"{root} is no longer a git repository.")
    for s in steps:
        if status[str(s["step_id"])] != "done":
            sm.mark_task(run_id, s["step_id"], "pending", attempts=0, reason=None)
    sm.reopen_run(run_id)
    ctx = _context(sm, run_id, run.project_id, Path(root), run.approval_level, trusted, dirty_files or [], require_git)
    ctx.emit("run_resumed", first_task=steps[first]["step_id"])
    state = initial_state(run.prompt, root, interactive, plan=run.plan, tasks=steps, task_status=status,
                          current_task_index=first, run_status="running", resuming=True,
                          file_changes=[{"file": c["file"]} for c in sm.list_file_changes(run_id)])
    return ctx, state


def undo(sm: StateManager, run_id: str, task_id: int | None = None, confirm_conflicts: bool = False) -> UndoResult:
    """Undo one task (task_id) or the whole run (None): restore exactly the files the run changed from its checkpoints."""
    run = sm.get_run(run_id)
    if run is None:
        return UndoResult(applied=False, error=f"No such run: {run_id}")
    root = sm.project_path(run.project_id)
    changes = sm.list_file_changes(run_id, task_id)
    res = checkpoints.undo(Path(root), run_id, changes, confirm_conflicts)
    sm.add_event(run_id, "undo", task_id, applied=res.applied, restored=res.restored, deleted=res.deleted,
                 conflicts=res.conflicts, error=res.error)
    return res
