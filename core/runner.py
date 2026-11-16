"""Shared run setup for the CLI and the API: create a run, or rebuild one for resume."""
import uuid
from pathlib import Path

import config
from core.runlog import RunLogger
from core.schemas import ProjectContext
from core.state_manager import StateManager


class ResumeError(RuntimeError):
    pass


def initial_state(prompt: str, root: str | Path, interactive: bool, **extra) -> dict:
    return {
        "messages": [], "user_prompt": prompt, "workspace_path": str(root), "plan": None, "tasks": [],
        "current_task_index": 0, "execution_history": [], "is_interactive": interactive, "plan_feedback": None,
        "tool_calls_count": 0, "task_status": {}, "task_errors": {}, "run_status": None, "run_error": None,
        "task_outcome": None, "task_reason": None, "replan_count": 0, "file_changes": [], "resuming": False,
        **extra,
    }


def _context(sm: StateManager, run_id: str, project_id: str, root: Path, approval_level: str | None = None, **kw) -> ProjectContext:
    run_dir = config.DATA_DIR / "runs" / run_id
    return ProjectContext(run_id=run_id, project_id=project_id, root=root, run_dir=run_dir, state_manager=sm,
                          logger=RunLogger(run_dir / "log.jsonl", project_id, run_id),
                          approval_level=approval_level or config.APPROVAL_LEVEL, **kw)


def start_run(sm: StateManager, project_id: str, root: str | Path, prompt: str, interactive: bool = False,
              approval_level: str | None = None, **ctx_kw) -> tuple[ProjectContext, dict]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    sm.upsert_project(project_id, str(root))
    sm.create_run(run_id, project_id, prompt, approval_level or config.APPROVAL_LEVEL)
    ctx = _context(sm, run_id, project_id, root, approval_level, **ctx_kw)
    ctx.emit("run_started", prompt=prompt[:200])
    return ctx, initial_state(prompt, root, interactive)


def prepare_resume(sm: StateManager, run_id: str, interactive: bool = False, **ctx_kw) -> tuple[ProjectContext, dict]:
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
    status = {}
    for s in steps:
        st = rows.get(s["step_id"], {}).get("status", "pending")
        status[str(s["step_id"])] = "done" if st == "done" else "pending"
        if st != "done":
            sm.mark_task(run_id, s["step_id"], "pending", attempts=0, reason=None)
    first = next((i for i, s in enumerate(steps) if status[str(s["step_id"])] != "done"), None)
    if first is None:
        raise ResumeError(f"Run {run_id} has no unfinished tasks.")

    sm.reopen_run(run_id)
    ctx = _context(sm, run_id, run.project_id, Path(root), run.approval_level, **ctx_kw)
    ctx.emit("run_resumed", first_task=steps[first]["step_id"])
    state = initial_state(run.prompt, root, interactive, plan=run.plan, tasks=steps, task_status=status,
                          current_task_index=first, run_status="running", resuming=True,
                          file_changes=[{"file": c["file"]} for c in sm.list_file_changes(run_id)])
    return ctx, state
