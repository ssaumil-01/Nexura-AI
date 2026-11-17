"""Nexura local API. Binds to 127.0.0.1 only; every request needs `Authorization: Bearer <NEXURA_TOKEN>`.
The workspace path is registered once (POST /workspaces) and never taken from a request body afterwards."""
import asyncio
import hmac
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import config as nexura_config
from core import metrics as metrics_mod
from core import runner
from core.checkpoints import GitError
from core.orchestrator import build_graph
from core.run_manager import RunManager, StateError
from core.runner import DirtyWorkspace, ResumeError, workspace_id_for
from core.safety import SafetyError, resolve_in_root
from core.state_manager import StateManager, WorkspaceBusy, make_checkpointer

ID = re.compile(r"^[a-f0-9]{8,64}$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_PROMPT = 20_000


# ─── request models ─────────────────────────────────────────

class WorkspaceReq(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    trusted: bool = False


class RunReq(BaseModel):
    workspace_id: str
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT)
    approval_level: Literal["ask_every_edit", "ask_risky", "auto_within_limits"] = "ask_risky"
    dirty_policy: Literal["checkpoint", "abort"] = "checkpoint"
    dirty_files: list[str] = Field(default_factory=list, max_length=500)
    git_init: bool = False


class PlanReq(BaseModel):
    decision: Literal["approve", "feedback"]
    text: Optional[str] = Field(default=None, max_length=MAX_PROMPT)


class ApproveReq(BaseModel):
    decision: Literal["approve", "reject"]
    note: Optional[str] = Field(default=None, max_length=2000)


class UndoReq(BaseModel):
    scope: Literal["task", "run"]
    task_id: Optional[int] = None
    confirm_conflicts: bool = False


class EditsReq(BaseModel):
    files: list[str] = Field(max_length=500)


class IndexReq(BaseModel):
    rebuild: bool = False


class ChangesReq(BaseModel):
    paths: list[str] = Field(max_length=1000)


def _host_name(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    return h.split("]")[0] + "]" if h.startswith("[") else h.rsplit(":", 1)[0] if ":" in h else h


def create_app(token: str | None = None, sm: StateManager | None = None, checkpointer=None, index_manager=None) -> FastAPI:
    token = token or os.getenv("NEXURA_TOKEN")
    if not token or len(token) < 16:
        raise RuntimeError("NEXURA_TOKEN must be set to a secret of at least 16 characters.")
    if os.getenv("NEXURA_FAKE_LLM") == "1":   # TEST ONLY: scripted models, no network (extension smoke test)
        from core import fake_llm
        fake_llm.install()
    sm = sm or StateManager(nexura_config.DB_PATH)
    manager = RunManager(sm, checkpointer or make_checkpointer(nexura_config.CHECKPOINT_DB_PATH), build_graph)
    workspaces: dict[str, dict] = {}   # workspace_id -> {"path", "trusted"} (registered by the extension)
    ws_events: dict[str, list[dict]] = {}   # workspace_id -> buffered workspace events (index_*), for SSE
    ws_lock = threading.Lock()
    index = {"manager": index_manager}

    def ws_emit(workspace_id: str, type: str, data: dict) -> None:
        with ws_lock:
            buf = ws_events.setdefault(workspace_id, [])
            buf.append({"id": (buf[-1]["id"] + 1) if buf else 1, "event": type, "data": data})
            del buf[:-1000]

    def indexing():
        """The index manager; 409 when indexing is turned off, 503 when the optional packages are missing."""
        if not nexura_config.INDEXING_ENABLED and index["manager"] is None:
            raise HTTPException(status_code=409, detail={"code": "INDEXING_DISABLED",
                                                         "message": "Code indexing is off (set NEXURA_INDEXING_ENABLED=true)."})
        if index["manager"] is None:
            try:
                from rag.embedder import make_embedder
                from rag.indexer import IndexManager
            except ImportError:
                raise HTTPException(status_code=503, detail={"code": "INDEX_DEPS_MISSING",
                                                             "message": "Install the indexing packages: pip install -r requirements-index.txt"})
            index["manager"] = IndexManager(make_embedder, emit=ws_emit)
        else:
            index["manager"].emit = ws_emit
        return index["manager"]

    def reindex_quietly(workspace_id: str, paths: list[str]) -> None:
        """After undo / external edits: refresh those files in an existing index. Never fails the request."""
        mgr = index["manager"]
        if mgr is not None and paths:
            try:
                mgr.notify_changes(workspace_id, paths)
            except Exception as e:
                ws_emit(workspace_id, "index_update_failed", {"error": str(e)[:300]})

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(manager.shutdown)

    app = FastAPI(title="Nexura-AI", lifespan=lifespan)   # no CORS middleware: only the extension host calls this
    app.state.manager, app.state.sm = manager, sm

    @app.middleware("http")
    async def only_localhost(request: Request, call_next):
        if _host_name(request.headers.get("host", "")) not in LOCAL_HOSTS:   # also blocks DNS rebinding
            return JSONResponse({"detail": "Only localhost is allowed."}, status_code=403)
        return await call_next(request)

    def auth(authorization: str | None = Header(default=None)) -> None:
        supplied = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="Missing or invalid token.", headers={"WWW-Authenticate": "Bearer"})

    def check_id(value: str, what: str = "id") -> str:
        if not ID.match(value):
            raise HTTPException(status_code=422, detail=f"Invalid {what}.")
        return value

    def workspace_of(workspace_id: str) -> dict:
        ws = workspaces.get(check_id(workspace_id, "workspace_id"))
        if ws is None:
            raise HTTPException(status_code=404, detail="Unknown workspace. Register it with POST /workspaces first.")
        return ws

    def run_or_404(run_id: str):
        row = sm.get_run(check_id(run_id, "run_id"))
        if row is None:
            raise HTTPException(status_code=404, detail="No such run.")
        return row

    def start_errors(fn):
        try:
            return fn()
        except GitError as e:
            raise HTTPException(status_code=409, detail={"code": e.code, "message": e.message})
        except DirtyWorkspace as e:
            raise HTTPException(status_code=409, detail={"code": "DIRTY_WORKSPACE", "message": str(e), "files": e.files[:100]})
        except WorkspaceBusy as e:
            raise HTTPException(status_code=409, detail={"code": "WORKSPACE_BUSY", "message": str(e)})
        except ResumeError as e:
            raise HTTPException(status_code=409, detail={"code": "CANNOT_RESUME", "message": str(e)})

    # ── endpoints ──
    @app.get("/health", dependencies=[Depends(auth)])
    async def health():
        return {"ok": True, "active_runs": sum(1 for h in manager.runs.values() if h.state != "finished")}

    @app.post("/workspaces", dependencies=[Depends(auth)])
    async def register_workspace(req: WorkspaceReq):
        p = Path(req.path)
        if not p.is_absolute() or not p.is_dir():
            raise HTTPException(status_code=400, detail="path must be an existing absolute directory.")
        wid = workspace_id_for(str(p))
        workspaces[wid] = {"path": str(p.resolve()), "trusted": req.trusted}
        sm.upsert_project(wid, str(p.resolve()))
        return {"workspace_id": wid}

    @app.post("/runs", dependencies=[Depends(auth)])
    async def create_run(req: RunReq):
        ws = workspace_of(req.workspace_id)
        ctx, state = await asyncio.to_thread(start_errors, lambda: runner.start_run(
            sm, req.workspace_id, ws["path"], req.prompt, approval_level=req.approval_level, git_init=req.git_init,
            dirty_policy=req.dirty_policy, dirty_files=req.dirty_files, trusted=ws["trusted"]))
        if index["manager"] is not None:   # keep the index fresh while the run edits files
            ctx.indexer = index["manager"].existing(req.workspace_id)
        manager.start(ctx, state)
        return {"run_id": ctx.run_id}

    @app.get("/runs/{run_id}", dependencies=[Depends(auth)])
    async def get_run(run_id: str):
        check_id(run_id, "run_id")
        h = manager.get(run_id)
        state, waiting, summary = (h.state, h.waiting, h.final) if h else ("idle", None, None)   # read BEFORE the DB:
        row = run_or_404(run_id)                                  # a finished handle always has its final DB status already
        tasks = [{k: t[k] for k in ("task_id", "title", "status", "attempts", "reason")} for t in sm.list_tasks(row.id)]
        return {"run_id": row.id, "status": row.status, "error": row.error, "prompt": row.prompt, "tasks": tasks,
                "state": state, "waiting": waiting, "summary": summary, "plan": row.plan}

    @app.get("/runs/{run_id}/events", dependencies=[Depends(auth)])
    async def run_events(run_id: str, last_event_id: Optional[str] = Header(default=None)):
        row = run_or_404(run_id)
        try:
            last = int(last_event_id or 0)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid Last-Event-ID.")
        h = manager.get(row.id)

        async def gen():
            nonlocal last
            if h is None:   # not active in this process: replay what the DB knows
                for e in sm.list_events(row.id):
                    if e["id"] > last:
                        yield {"id": str(e["id"]), "event": e["type"], "data": json.dumps(e["data"], default=str)}
                return
            while True:
                for e in h.since(last):
                    last = e["id"]
                    yield {"id": str(e["id"]), "event": e["event"], "data": json.dumps(e["data"], default=str)}
                if h.state == "finished" and not h.since(last):
                    return
                await asyncio.sleep(0.05)
        return EventSourceResponse(gen())

    def respond(run_id: str, kind: str, value) -> dict:
        run_or_404(run_id)
        try:
            manager.respond(run_id, kind, value)
        except StateError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True}

    @app.post("/runs/{run_id}/plan", dependencies=[Depends(auth)])
    async def plan_decision(run_id: str, req: PlanReq):
        if req.decision == "feedback" and not (req.text or "").strip():
            raise HTTPException(status_code=422, detail="feedback requires text.")
        return respond(run_id, "plan", "yes" if req.decision == "approve" else req.text.strip())

    @app.post("/runs/{run_id}/approve", dependencies=[Depends(auth)])
    async def approve(run_id: str, req: ApproveReq):
        return respond(run_id, "approval", {"decision": req.decision, "note": req.note or ""})

    @app.post("/runs/{run_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(run_id: str):
        row = run_or_404(run_id)
        if not manager.cancel(row.id):
            raise HTTPException(status_code=409, detail="The run is not active.")
        return {"ok": True}

    @app.post("/runs/{run_id}/resume", dependencies=[Depends(auth)])
    async def resume(run_id: str):
        row = run_or_404(run_id)
        if manager.active(row.id):
            raise HTTPException(status_code=409, detail="The run is still active.")
        ws = workspaces.get(row.project_id)
        if ws is None:
            raise HTTPException(status_code=409, detail="Register the workspace (POST /workspaces) before resuming.")
        ctx, state = await asyncio.to_thread(start_errors, lambda: runner.prepare_resume(sm, row.id, trusted=ws["trusted"]))
        manager.start(ctx, state, thread_id=manager.resume_thread_id(row.id))
        return {"run_id": row.id}

    @app.post("/runs/{run_id}/undo", dependencies=[Depends(auth)])
    async def undo(run_id: str, req: UndoReq):
        row = run_or_404(run_id)
        if manager.active(row.id):
            raise HTTPException(status_code=409, detail="Stop the run before undoing its changes.")
        if req.scope == "task" and req.task_id is None:
            raise HTTPException(status_code=422, detail="task_id is required for scope 'task'.")
        res = await asyncio.to_thread(runner.undo, sm, row.id, req.task_id if req.scope == "task" else None, req.confirm_conflicts)
        await asyncio.to_thread(reindex_quietly, row.project_id, res.restored + res.deleted)
        return {"applied": res.applied, "restored": res.restored, "deleted": res.deleted, "conflicts": res.conflicts, "error": res.error}

    @app.post("/runs/{run_id}/edits", dependencies=[Depends(auth)])
    async def external_edits(run_id: str, req: EditsReq):
        """The extension reports files the user edited during the run (pauses the run before it overwrites them)."""
        row = run_or_404(run_id)
        h = manager.get(row.id)
        if h is None or h.state == "finished":
            raise HTTPException(status_code=409, detail="The run is not active.")
        marked = 0
        for f in req.files:
            try:
                h.ctx.external_edits.add(h.ctx.key(h.ctx.root / f))
                marked += 1
            except ValueError:
                pass
        return {"marked": marked}

    # ── code index (V2) ──
    @app.post("/workspaces/{workspace_id}/index", dependencies=[Depends(auth)], status_code=202)
    async def start_index(workspace_id: str, req: IndexReq):
        ws = workspace_of(workspace_id)
        return await asyncio.to_thread(indexing().start, workspace_id, ws["path"], req.rebuild)

    @app.get("/workspaces/{workspace_id}/index", dependencies=[Depends(auth)])
    async def index_status(workspace_id: str):
        workspace_of(workspace_id)
        return indexing().status(workspace_id)

    @app.post("/workspaces/{workspace_id}/index/cancel", dependencies=[Depends(auth)])
    async def cancel_index(workspace_id: str):
        workspace_of(workspace_id)
        return {"cancelled": indexing().cancel(workspace_id)}

    @app.delete("/workspaces/{workspace_id}/index", dependencies=[Depends(auth)])
    async def delete_index(workspace_id: str):
        workspace_of(workspace_id)
        return {"deleted": await asyncio.to_thread(indexing().delete, workspace_id)}

    @app.post("/workspaces/{workspace_id}/index/changes", dependencies=[Depends(auth)])
    async def index_changes(workspace_id: str, req: ChangesReq):
        """Files changed outside a run (the extension's file watcher). Paths are checked like any other workspace path."""
        ws = workspace_of(workspace_id)
        mgr = indexing()
        rels = []
        for f in req.paths:
            try:
                full = resolve_in_root(ws["path"], f)
            except SafetyError as e:
                raise HTTPException(status_code=422, detail={"code": e.code, "message": e.message})
            rels.append(os.path.relpath(full, os.path.realpath(ws["path"])).replace("\\", "/"))
        result = await asyncio.to_thread(mgr.notify_changes, workspace_id, rels)
        return {"updated": result is not None, **(result or {})}

    @app.get("/workspaces/{workspace_id}/retrieve", dependencies=[Depends(auth)])
    async def debug_retrieve(workspace_id: str, q: str = Query(min_length=1, max_length=2000), k: int = Query(default=8, ge=1, le=50)):
        """Read-only: what retrieval returns for a query, with sources and reasons. Same safety filter as the prompt context."""
        ws = workspace_of(workspace_id)
        ix = indexing().ready_indexer(workspace_id)
        if ix is None:
            raise HTTPException(status_code=409, detail={"code": "INDEX_NOT_READY", "message": "Build the index first."})

        def run():
            from core.context_builder import safe_snippet
            from core.safety import load_ignore_patterns
            patterns, cache, out = load_ignore_patterns(ws["path"]), {}, []
            for r in ix.retriever().retrieve(q, k=k):
                snippet = safe_snippet(Path(ws["path"]), r.chunk, patterns, cache)
                if snippet is not None:
                    out.append({"path": r.chunk.path, "lines": [r.chunk.start_line, r.chunk.end_line], "symbol": r.chunk.symbol,
                                "kind": r.chunk.kind, "score": r.score, "sources": r.sources, "reason": r.reason,
                                "snippet": "\n".join(snippet.splitlines()[:30])})
            return out
        return {"query": q, "results": await asyncio.to_thread(run)}

    @app.get("/workspaces/{workspace_id}/events", dependencies=[Depends(auth)])
    async def workspace_events(workspace_id: str, last_event_id: Optional[str] = Header(default=None)):
        """Workspace events (index_started/progress/finished/failed/cancelled). Ends when no indexing job is running."""
        workspace_of(workspace_id)
        try:
            last = int(last_event_id or 0)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid Last-Event-ID.")

        async def gen():
            nonlocal last
            while True:
                with ws_lock:
                    new = [e for e in ws_events.get(workspace_id, []) if e["id"] > last]
                for e in new:
                    last = e["id"]
                    yield {"id": str(e["id"]), "event": e["event"], "data": json.dumps(e["data"], default=str)}
                mgr = index["manager"]
                if not new and (mgr is None or mgr.status(workspace_id).get("state") != "indexing"):
                    return
                await asyncio.sleep(0.05)
        return EventSourceResponse(gen())

    @app.get("/metrics/{run_id}", dependencies=[Depends(auth)])
    async def metrics(run_id: str):
        m = metrics_mod.compute(sm, check_id(run_id, "run_id"))
        if m is None:
            raise HTTPException(status_code=404, detail="No such run.")
        return m

    return app


if __name__ == "__main__":
    import uvicorn
    nexura_config.configure_observability()
    port = int(os.getenv("NEXURA_PORT", "8000"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
