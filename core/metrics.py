"""Run metrics computed from the state DB (read-only)."""
import time
from collections import Counter

from core.state_manager import StateManager


def compute(sm: StateManager, run_id: str) -> dict | None:
    run = sm.get_run(run_id)
    if run is None:
        return None
    tasks, events = sm.list_tasks(run_id), sm.list_events(run_id)
    done = sum(1 for t in tasks if t["status"] == "done")
    errors = Counter()
    tokens = 0
    for e in events:
        if e["type"] == "verification_failed":
            errors[e["data"].get("error_type", "unknown")] += 1
        elif e["type"] == "validation_rejected":
            errors["rejected:" + str(e["data"].get("code"))] += 1
        elif e["type"] == "llm_call":
            tokens += e["data"].get("tokens") or 0
    return {
        "run_id": run_id, "status": run.status,
        "tasks_total": len(tasks), "tasks_done": done,
        "task_success_rate": round(done / len(tasks), 3) if tasks else 0.0,
        "retries_per_task": {str(t["task_id"]): t["attempts"] for t in tasks},
        "error_types": dict(errors),
        "tokens": tokens or None,
        "wall_time_s": round((run.finished_at or time.time()) - run.created_at, 2),
    }
