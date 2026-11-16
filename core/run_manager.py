"""In-process registry of runs. Each run's graph executes in a worker thread; the API talks to it through events and
`respond()` (plan decisions / approvals). Cancel stops managed processes immediately and ends the run."""
import queue
import threading
import time
from typing import Any, Callable

from langgraph.types import Command

from core.schemas import ProjectContext
from core.state_manager import StateManager
from core.task_manager import run_summary

CANCEL = object()
MAX_EVENTS = 5000


class StateError(RuntimeError):
    """The run is not in a state that accepts this request."""


class RunHandle:
    def __init__(self, ctx: ProjectContext, graph, cfg: dict):
        self.run_id, self.ctx, self.graph, self.cfg = ctx.run_id, ctx, graph, cfg
        self.events: list[dict] = []      # {"id", "event", "data"}; id = position + 1
        self.lock = threading.Lock()
        self.state = "running"            # running | awaiting_plan | awaiting_approval | finished
        self.waiting: Any = None          # the current interrupt payload
        self.cancelled = False
        self.final: dict | None = None
        self.resume_q: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self._db_cursor = 0

    def emit(self, event: str, data: dict) -> None:
        with self.lock:
            self.events.append({"id": len(self.events) + 1, "event": event, "data": data})
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]

    def since(self, last_id: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["id"] > last_id]


# DB event type -> SSE contract events
def _map_db_event(e: dict) -> list[tuple[str, dict]]:
    d, t, tid = e["data"], e["type"], e["task_id"]
    if t == "task_started":
        return [("task_started", {"task_id": tid, **d})]
    if t == "tool_call":
        return [("tool_call", {"task_id": tid, "tool": d.get("tool"), "args": d.get("args")}),
                ("tool_result", {"task_id": tid, "tool": d.get("tool"), "ok": d.get("ok"), "code": d.get("code"), "summary": d.get("summary")})]
    if t == "validation_rejected":
        return [("tool_result", {"task_id": tid, "tool": d.get("tool"), "ok": False, "code": d.get("code"), "rejected": True})]
    if t == "verification_passed":
        return [("verification", {"task_id": tid, "ok": True, **d})]
    if t == "verification_failed":
        return [("verification", {"task_id": tid, "ok": False, **d})]
    if t in ("task_done", "task_failed"):
        return [(t, {"task_id": tid, **d})]
    return []


class RunManager:
    def __init__(self, sm: StateManager, checkpointer, build_graph: Callable):
        self.sm, self.checkpointer, self._build = sm, checkpointer, build_graph
        self.runs: dict[str, RunHandle] = {}
        self._lock = threading.Lock()

    # ── lifecycle ──
    def start(self, ctx: ProjectContext, state: dict, thread_id: str | None = None) -> RunHandle:
        graph = self._build(interactive=False, checkpointer=self.checkpointer)
        cfg = {"configurable": {"thread_id": thread_id or ctx.run_id, "project": ctx}, "recursion_limit": 500,
               "metadata": {"run_id": ctx.run_id}}
        h = RunHandle(ctx, graph, cfg)
        with self._lock:
            self.runs[ctx.run_id] = h
        h.thread = threading.Thread(target=self._work, args=(h, state), name=f"run-{ctx.run_id}", daemon=True)
        h.thread.start()
        return h

    def resume_thread_id(self, run_id: str) -> str:
        return f"{run_id}-r{int(time.time())}"

    def get(self, run_id: str) -> RunHandle | None:
        return self.runs.get(run_id)

    def active(self, run_id: str) -> bool:
        h = self.runs.get(run_id)
        return bool(h and h.state != "finished")

    def respond(self, run_id: str, kind: str, value: Any) -> None:
        """kind 'plan' answers the plan review; kind 'approval' answers an approval request."""
        h = self.runs.get(run_id)
        if h is None or h.state == "finished":
            raise StateError("The run is not active.")
        need = {"plan": "awaiting_plan", "approval": "awaiting_approval"}[kind]
        with h.lock:
            if h.state != need:
                raise StateError(f"The run is not {need.replace('_', ' ')} (state: {h.state}).")
            h.state = "running"
            h.waiting = None
        h.resume_q.put(value)

    def cancel(self, run_id: str) -> bool:
        h = self.runs.get(run_id)
        if h is None or h.state == "finished":
            return False
        h.cancelled = True
        h.ctx.processes.stop_all()
        h.resume_q.put(CANCEL)
        return True

    def shutdown(self) -> None:
        for rid in list(self.runs):
            self.cancel(rid)
        for h in list(self.runs.values()):
            if h.thread and h.thread.is_alive():
                h.thread.join(timeout=5)
            h.ctx.processes.stop_all()

    # ── worker ──
    def _pull_db_events(self, h: RunHandle) -> None:
        for e in self.sm.list_events(h.run_id, h._db_cursor):
            h._db_cursor = e["id"]
            for name, data in _map_db_event(e):
                h.emit(name, data)

    def _work(self, h: RunHandle, first_input: dict) -> None:
        graph, cfg, inp = h.graph, h.cfg, first_input
        h._db_cursor = (self.sm.list_events(h.run_id) or [{"id": 0}])[-1]["id"]   # only events from this execution
        try:
            while True:
                for _ in graph.stream(inp, cfg, stream_mode="updates"):
                    self._pull_db_events(h)
                    if h.cancelled:
                        break
                self._pull_db_events(h)
                if h.cancelled:
                    break
                snap = graph.get_state(cfg)
                if not (snap.next and snap.tasks and snap.tasks[0].interrupts):
                    break
                payload = snap.tasks[0].interrupts[0].value
                with h.lock:
                    h.waiting = payload
                    h.state = "awaiting_plan" if isinstance(payload, str) else "awaiting_approval"
                if isinstance(payload, str):
                    h.emit("plan_ready", {"plan": snap.values.get("plan"), "tasks": snap.values.get("tasks")})
                else:
                    h.emit("approval_required", payload)
                value = h.resume_q.get()
                if value is CANCEL or h.cancelled:
                    break
                inp = Command(resume=value)
            values = graph.get_state(cfg).values
            if h.cancelled:
                self.sm.update_run(h.run_id, "cancelled")
                status = "cancelled"
            else:
                status = values.get("run_status") or "failed"
            summary = run_summary({**values, "run_status": status})
            h.final = summary
            h.emit("run_finished", {"run_status": status, "summary": summary})
        except BaseException as e:   # the run must always end in a recorded state
            msg = f"{type(e).__name__}: {str(e)[:300]}"
            h.emit("error", {"message": msg})
            self.sm.update_run(h.run_id, "failed", msg)
            h.final = {"run_status": "failed", "run_error": msg}
            h.emit("run_finished", {"run_status": "failed", "summary": h.final})
        finally:
            h.ctx.processes.stop_all()
            with h.lock:
                h.state = "finished"
                h.waiting = None
