"""Plan validation, next-task selection and run summaries (pure functions over plain dicts)."""
import config


def validate_plan(plan) -> list[str]:
    """Return a list of problems with an ExecutionPlan (empty list = valid)."""
    errs: list[str] = []
    steps = plan.steps
    if not steps:
        return ["plan has no steps"]
    ids = [s.step_id for s in steps]
    if ids != list(range(1, len(steps) + 1)):
        errs.append(f"step_ids must be 1..{len(steps)} in order, got {ids}")
    for s in steps:
        for d in s.dependencies:
            if d not in ids:
                errs.append(f"step {s.step_id} depends on unknown step {d}")
            elif d >= s.step_id:
                errs.append(f"step {s.step_id} depends on step {d}, which is not earlier")
        if not s.acceptance and not s.documentation_only:
            errs.append(f"step {s.step_id} has no acceptance checks (set documentation_only only for pure docs)")
    return errs


def pick_next(tasks: list[dict], status: dict[str, str], policy: str | None = None) -> tuple[int | None, dict[str, str]]:
    """Next runnable task index (or None when the run is over) and the updated status map.
    policy 'stop': any failure skips everything pending. 'continue': skip only tasks depending on a failed/skipped one."""
    policy = policy or config.FAILURE_POLICY
    status = dict(status)
    if policy == "stop" and any(v == "failed" for v in status.values()):
        return None, {k: ("skipped" if v == "pending" else v) for k, v in status.items()}
    for i, t in enumerate(tasks):
        sid = str(t["step_id"])
        if status.get(sid) != "pending":
            continue
        deps = [str(d) for d in t.get("dependencies", [])]
        if any(status.get(d) in ("failed", "skipped") for d in deps):
            status[sid] = "skipped"
            continue
        if all(status.get(d) == "done" for d in deps):
            return i, status
    return None, status


def final_run_status(status: dict[str, str]) -> str:
    vals = list(status.values())
    if vals and all(v == "done" for v in vals):
        return "completed"
    return "completed_with_failures" if "done" in vals else "failed"


def run_summary(values: dict) -> dict:
    """Summary of a (finished or interrupted) run from graph state values."""
    status = values.get("task_status") or {}
    titles = {str(t["step_id"]): t["title"] for t in values.get("tasks") or []}

    def pick(s):
        return [{"step_id": int(k), "title": titles.get(k, ""), "reason": (values.get("task_errors") or {}).get(k)}
                for k, v in status.items() if v == s]
    return {"run_status": values.get("run_status") or "incomplete", "run_error": values.get("run_error"),
            "done": pick("done"), "failed": pick("failed"), "skipped": pick("skipped")}


def load_tasks(plan: dict) -> list:
    """plan.json dict -> TaskRecords (ordered). Persistence goes through state_manager (Phase 4)."""
    from core.schemas import TaskRecord
    return [TaskRecord(task_id=s["step_id"], title=s["title"], description=s.get("description", ""),
                       dependencies=s.get("dependencies", []), acceptance=s.get("acceptance", []))
            for s in plan.get("steps", [])]
