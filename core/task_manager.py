"""Plan validation, next-task selection and run summaries (pure functions over plain dicts)."""
import config


# Command problems the planner can fix by rewriting the check (NOT_FOUND depends on the machine, so it is not judged at plan time)
PLAN_TIME_CODES = {"SHELL_OPERATOR", "NOT_ALLOWED", "USE_INSTALL_DEPENDENCY", "BAD_COMMAND", "EMPTY_COMMAND", "BAD_INSTALL",
                   "NOT_AN_INSTALL", "OUTSIDE_ROOT"}


def _bad_commands(step, extra=()) -> list[str]:
    """Acceptance commands the validator would always reject (e.g. `pip install ...`), so the check could never pass."""
    from core.safety import SafetyError, parse_command
    out = []
    for acc in step.acceptance:
        for cmd in (acc.command, acc.start_command):
            if not cmd:
                continue
            try:
                parse_command(cmd, ".", extra_allowed=extra)
            except SafetyError as e:
                if e.code in PLAN_TIME_CODES:
                    out.append(f"step {step.step_id}: acceptance command `{cmd}` is not allowed ({e.code}: {e.message}); "
                               "checks must not install packages or use shell operators")
    return out


def _bad_paths(step, exists) -> list[str]:
    """V2: an existing project's plan must name real files in `files` and must not claim an existing file is new."""
    out = []
    for f in step.files:
        if not exists(f):
            out.append(f"step {step.step_id}: `{f}` is listed in files (existing files to edit) but does not exist; "
                       "use a path from the project listing, or put it in new_files")
    for f in step.new_files:
        if exists(f):
            out.append(f"step {step.step_id}: `{f}` is listed as new but already exists; put it in files and edit it instead")
    return out


def validate_plan(plan, extra_commands=(), path_exists=None) -> list[str]:
    """Return a list of problems with an ExecutionPlan (empty list = valid). `path_exists(rel) -> bool` enables the
    existing-project path check (V2)."""
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
        errs += _bad_commands(s, extra_commands)
        if path_exists is not None:
            errs += _bad_paths(s, path_exists)
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


def render_plan_md(plan: dict, status: dict[str, str], errors: dict[str, str] | None = None, run_status: str | None = None) -> str:
    """Human-readable view of the plan with live status. plan.json stays the machine copy; agents never edit either."""
    errors = errors or {}
    mark = {"done": "[x]", "failed": "[!]", "skipped": "[-]"}
    md = f"# Project: {plan.get('project_id', '')}\n**Goal**: {plan.get('goal', '')}\n"
    if run_status:
        md += f"**Run status**: {run_status}\n"
    md += "\n## Execution Steps\n\n"
    for s in plan.get("steps", []):
        sid = str(s["step_id"])
        st = status.get(sid, "pending")
        line = f"- {mark.get(st, '[ ]')} **Step {s['step_id']}: {s['title']}**"
        if st == "running":
            line += " (in progress)"
        elif st in ("failed", "skipped"):
            line += f" ({st.upper()}" + (f": {errors[sid]}" if sid in errors else "") + ")"
        md += line + "\n"
        md += f"  {s.get('description', '')}\n"
        for a in s.get("acceptance", []):
            md += "  - check: " + ", ".join(f"{k}={v}" for k, v in a.items() if v is not None) + "\n"
    return md
