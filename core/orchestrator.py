"""The LangGraph pipeline. Agents propose; this module validates, executes, verifies, retries and decides.

plan -> review_plan -> task_picker -> coder -> validate -> [approval] -> execute -> (loop | verify)
verify: pass -> advance_task | fail -> retry_manager -> debugger -> validate -> execute -> verify | exhausted -> advance_task(failed)
"""
import hashlib
from pathlib import Path
from typing import Any

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt, Command
from langchain_core.messages import HumanMessage, RemoveMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

import config as app_config
from agents import coder, debugger
from core import execution_manager as em
from core import approvals, checkpoints, retry_manager, validator, verification
from core.context_builder import build_task_context
from core.safety import SafetyError, redact_secrets, resolve_in_root
from core.schemas import Action
from core.state import NexuraState
from core.task_manager import final_run_status, load_tasks, pick_next, render_plan_md


# ─── HELPERS ────────────────────────────────────────────────

def _short(value: Any, n: int = 200) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= n else text[:n] + "..."


def _fail(reason: str) -> dict:
    return {"task_outcome": "failed", "task_reason": reason}


def _tool_msg(content: str, tc: dict, error: bool = False) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tc["id"], name=tc["name"], status="error" if error else "success")


def _hist(task: dict, tool: str, args: Any, result: str, status: str) -> dict:
    return {"step_id": task["step_id"], "tool": tool, "args": _short(args), "result": _short(result), "status": status}


def _cur(state: NexuraState) -> dict:
    return state["tasks"][state["current_task_index"]]


def _ctx(config: RunnableConfig):
    return config["configurable"]["project"]


def _write_plan_md(ctx, state_values: dict) -> None:
    """Re-render plan.md (a view of the plan with live status) into the run folder."""
    if ctx.run_dir and state_values.get("plan"):
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        (ctx.run_dir / "plan.md").write_text(render_plan_md(
            state_values["plan"], state_values.get("task_status") or {}, state_values.get("task_errors"),
            state_values.get("run_status")), encoding="utf-8")


def _llm_logger(ctx, task_id):
    def log(role, prompt_version, model, result, input_summary="", output_summary="", tokens=None):
        if ctx.logger is not None:
            ctx.logger.log(agent=role, task_id=task_id, prompt_version=prompt_version, model=model, input_summary=input_summary,
                           output_summary=output_summary, result=result, tokens=tokens)
        if ctx.state_manager is not None:
            ctx.state_manager.add_event(ctx.run_id, "llm_call", task_id, role=role, result=result, tokens=tokens)
        print(f"  [LLM] role={role} prompt_version={prompt_version} model={model} result={result}")
    return log


def _finish_run(ctx, status: str, error: str | None = None) -> None:
    if ctx.state_manager is not None:
        ctx.state_manager.update_run(ctx.run_id, status, error)
    ctx.emit("run_finished", status=status, error=error)


# ─── PLANNING ───────────────────────────────────────────────

def planner_node(state: NexuraState, config: RunnableConfig) -> dict:
    """Generates the execution plan from user prompt. Supports re-planning with feedback."""
    from agents.planner import PlannerAgent, PlanError

    ctx = _ctx(config)
    plan_dir = ctx.run_dir or (ctx.root / ".nexura")  # plan files live outside the user's tree when a run dir exists
    feedback = state.get("plan_feedback")
    previous_plan = state.get("plan")
    print("\n--- [1] Re-running Planner with feedback ---" if feedback and previous_plan else "\n--- [1] Running Planner ---")

    try:
        planner = PlannerAgent()
        plan = planner.plan(user_prompt=state["user_prompt"], project_path=str(plan_dir),
                                   feedback=feedback, previous_plan=previous_plan)
    except PlanError as e:
        print(f"[Planner] FAILED: {e}")
        _finish_run(ctx, "failed", f"Planner failed: {e}")
        return {"run_status": "failed", "run_error": f"Planner failed: {e}"}

    plan_dict = plan.model_dump()
    plan_dict["warnings"] = list(getattr(planner, "last_warnings", []) or [])   # e.g. acceptance checks that had to be dropped
    tasks = plan_dict.get("steps", [])
    print(f"[Planner] Generated {len(tasks)} tasks for project: {plan_dict.get('project_id', 'unknown')}")
    if ctx.state_manager is not None:
        ctx.state_manager.save_plan(ctx.run_id, plan_dict, load_tasks(plan_dict))
    return {
        "plan": plan_dict, "tasks": tasks, "current_task_index": 0, "plan_feedback": None,
        "task_status": {str(t["step_id"]): "pending" for t in tasks}, "task_errors": {},
        "run_status": "running", "run_error": None,
    }


def route_start(state: NexuraState) -> str:
    """A resumed run already has an approved plan: skip planning and review."""
    return "task_picker" if state.get("resuming") else "planner"


def route_after_planner(state: NexuraState) -> str:
    return END if state.get("run_status") == "failed" else "review_plan"


def review_plan_node(state: NexuraState, config: RunnableConfig) -> Command:
    """Displays the plan and asks the user for approval or feedback."""
    plan, tasks = state["plan"], state["tasks"]
    print("\n" + "=" * 50 + "\nGENERATED PLAN\n" + "=" * 50)
    print(f"Project: {plan.get('project_id', 'unknown')}\nGoal: {plan.get('goal', '')}\n\nSteps ({len(tasks)}):")
    for w in plan.get("warnings", []):
        print(f"  WARNING: {w}")
    for task in tasks:
        deps = task.get("dependencies", [])
        print(f"  {task['step_id']}. {task['title']}" + (f" (depends on: {deps})" if deps else ""))
        print(f"     {task['description']}")
        for acc in task.get("acceptance", []):
            print(f"     check: {_short({k: v for k, v in acc.items() if v is not None}, 120)}")
    print("=" * 50)

    user_input = interrupt("Review the plan above. Type 'yes' to approve, or provide feedback to re-plan:")
    if user_input.strip().lower() in ["yes", "y", "approve", ""]:
        print("[Review] Plan approved! Starting execution...")
        return Command(goto="task_picker")

    replans = state.get("replan_count", 0)
    if replans >= app_config.MAX_REPLANS:
        msg = f"Re-plan limit reached ({app_config.MAX_REPLANS}). Stopping."
        print(f"[Review] {msg}")
        _finish_run(_ctx(config), "failed", msg)
        return Command(goto=END, update={"run_status": "failed", "run_error": msg})
    print("[Review] Feedback received. Re-planning...")
    return Command(goto="planner", update={"plan_feedback": user_input.strip(), "replan_count": replans + 1})


# ─── TASK LIFECYCLE ─────────────────────────────────────────

def task_picker_node(state: NexuraState, config: RunnableConfig) -> dict:
    """Starts the current task: fresh conversation, budgeted workspace context, coder prompt."""
    idx, tasks = state["current_task_index"], state["tasks"]
    if idx >= len(tasks):
        return {}  # nothing left: routes to END
    task = tasks[idx]
    ctx = _ctx(config)
    ctx.reset_task()
    if ctx.state_manager is not None:
        ctx.state_manager.mark_task(ctx.run_id, task["step_id"], "running")
    ctx.emit("task_started", task["step_id"], title=task["title"])
    if ctx.git_checkpoints:   # pre-task snapshot: what undo restores
        try:
            checkpoints.snapshot(ctx.root, ctx.run_id, task["step_id"])
        except checkpoints.GitError as e:
            return {**_fail(f"CHECKPOINT_FAILED: {e.message}"), "task_status": {**(state.get("task_status") or {}), str(task["step_id"]): "running"}}

    run_changed = [fc["file"] for fc in state.get("file_changes", [])]
    workspace_context, breakdown = build_task_context(task, ctx, run_changed)
    print(f"  [Context] {breakdown}")

    history_summary = "No previous executions yet."
    if state["execution_history"]:
        history_summary = "\n".join(f"- Step {h['step_id']}: {h['tool']} -> {h['result'][:100]}"
                                    for h in state["execution_history"][-5:])

    print(f"\n--- [Orchestrator] === Executing Task {task['step_id']}: {task['title']} === ---")
    prompt = coder.build_prompt(task, state["plan"].get("goal", ""), workspace_context, history_summary)

    status = dict(state.get("task_status") or {})
    status[str(task["step_id"])] = "running"
    started_status = {**(state.get("task_status") or {}), str(task["step_id"]): "running"}
    _write_plan_md(ctx, {**state, "task_status": started_status})
    return {
        # remove ALL previous messages (the add_messages reducer ignores a bare []), then start fresh
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(content=prompt)],
        "task_status": status, "tool_calls_count": 0, "attempts": 0, "mode": "code", "pending": [], "task_lines": 0,
        "approvals_rejected": 0,
        "changed_files": [], "command_results": [], "verify_failure": None, "retry_hint": "",
        "task_outcome": None, "task_reason": None,
    }


def _agent_turn(state: NexuraState, propose, role_label: str, config: RunnableConfig) -> dict:
    task_msgs = state["messages"]
    response, failure = propose(task_msgs, on_log=_llm_logger(_ctx(config), _cur(state)["step_id"]))
    if failure:
        print(f"  [!] {failure}")
        return _fail(failure)
    if getattr(response, "tool_calls", None):
        return {"messages": [response]}
    # prose without a tool call: nudge, and count it so this cannot loop forever
    count = state.get("tool_calls_count", 0) + 1
    attempts = state.get("attempts", 0) + (1 if role_label == "debugger" else 0)
    update = {"messages": [response, HumanMessage(content="You must act by calling a tool.")],
              "tool_calls_count": count, "attempts": attempts}
    if count >= app_config.MAX_TOOL_CALLS_PER_TASK or attempts > app_config.MAX_RETRIES:
        update.update(_fail("NO_TOOL_CALLS: model kept answering without calling tools"))
    return update


def coder_node(state: NexuraState, config: RunnableConfig) -> dict:
    return _agent_turn(state, coder.propose, "coder", config)


def debugger_node(state: NexuraState, config: RunnableConfig) -> dict:
    """Builds a focused prompt from the failure and asks the Debugger for fixes (proposals only)."""
    ctx, task = _ctx(config), _cur(state)
    names = list(dict.fromkeys(state.get("changed_files", [])
                               + [a["path"] for a in task.get("acceptance", []) if a.get("path")]
                               + [p for a in task.get("acceptance", []) for p in (a.get("paths") or [])]))[:6]
    files = {}
    for n in names:
        try:
            body = (ctx.root / n).read_text(encoding="utf-8")[:6000]
        except (OSError, UnicodeDecodeError):
            continue
        files[n] = body
        ctx.files_read.add(ctx.key(ctx.root / n))  # shown to the debugger, so it counts as read
    prompt = debugger.build_prompt(task, state.get("verify_failure") or "", state.get("error_type") or "unknown",
                                   (state.get("retry_hint") or "") + (
                                       "\nYour previous proposal was rejected: " + state["last_rejection"] if state.get("last_rejection") else ""),
                                   files)
    print(f"  [Debugger] attempt {state.get('attempts', 0)} for task {task['step_id']}")
    state = {**state, "messages": [HumanMessage(content=prompt)]}  # fresh, focused conversation
    out = _agent_turn(state, debugger.propose, "debugger", config)
    if "messages" in out:
        out["messages"] = [RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(content=prompt), *out["messages"]]
    return out


def route_after_agent(state: NexuraState) -> str:
    if state.get("task_outcome"):
        return "advance_task"
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return "debugger" if state.get("mode") == "debug" else "coder"  # nudged: ask again
    return "validate"


# ─── VALIDATE -> (APPROVAL) -> EXECUTE ──────────────────────

def _sha_file(path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _conflicts(ctx, previews: list[dict]) -> list[str]:
    """Files the user changed behind our back since Nexura last read/wrote them."""
    out = []
    for p in previews:
        if p["change_type"] == "created":
            continue
        try:
            full = resolve_in_root(ctx.root, p["file"])
        except SafetyError:
            continue
        key = ctx.key(full)
        known = ctx.known_hashes.get(key)
        if key in ctx.external_edits or (known is not None and _sha_file(full) != known):
            out.append(p["file"])
    return out


def validate_node(state: NexuraState, config: RunnableConfig) -> dict:
    """Every proposed call becomes an Action and must pass the validator before anything runs.
    Valid actions are previewed (diffs) and risk-assessed; the ones that need approval carry their reasons."""
    ctx, task, mode = _ctx(config), _cur(state), state.get("mode", "code")
    calls = state["messages"][-1].tool_calls
    msgs, pending, hist, rejected, last_reject, skip_rest = [], [], [], 0, "", False
    files_in_task, lines_in_task = set(state.get("changed_files", [])), state.get("task_lines", 0)

    for tc in calls:
        if skip_rest:
            msgs.append(_tool_msg("SKIPPED: ignored because task_complete was already requested in this batch.", tc, True))
            continue
        action = em.action_from_tool_call(tc["name"], tc["args"])
        r = validator.validate(action, ctx, mode)
        if not r.ok and r.code != "APPROVAL_REQUIRED":   # protected paths are not refused: they need the user's approval
            rejected += 1
            last_reject = f"{r.code}: {r.message}"
            ctx.emit("validation_rejected", task["step_id"], code=r.code, tool=tc["name"])
            msgs.append(_tool_msg(f"ERROR: {r.code}: {r.message}", tc, True))
            hist.append(_hist(task, tc["name"], tc["args"], last_reject, "rejected"))
            continue
        previews = approvals.preview(action, ctx)
        reasons = approvals.assess(action, previews, ctx, files_in_task, lines_in_task, _conflicts(ctx, previews))
        ask = approvals.requires_approval(reasons, ctx.approval_level)
        lines = sum(p["lines"] for p in previews)
        files_in_task |= {p["file"] for p in previews}
        lines_in_task += lines
        pending.append({"id": tc["id"], "name": tc["name"], "args": tc["args"], "action": action.model_dump(),
                        "preview": previews, "reasons": ask, "lines": lines,
                        "protected": any(t.startswith("protected path") for _, t in reasons)})
        skip_rest = tc["name"] == "task_complete"

    count = state.get("tool_calls_count", 0) + rejected
    attempts = state.get("attempts", 0) + (1 if rejected else 0)
    update = {"messages": msgs, "pending": pending, "tool_calls_count": count, "attempts": attempts,
              "execution_history": state["execution_history"] + hist, "last_rejection": last_reject}
    if attempts > app_config.MAX_RETRIES:
        update.update(_fail(f"VALIDATION_REJECTED: {last_reject}"))
    elif not pending and count >= app_config.MAX_TOOL_CALLS_PER_TASK:
        update.update(_fail(f"TOOL_CAP: {app_config.MAX_TOOL_CALLS_PER_TASK} tool calls without completing"))
    return update


def route_after_validate(state: NexuraState) -> str:
    if state.get("task_outcome"):
        return "advance_task"
    pending = state.get("pending")
    if not pending:
        return "debugger" if state.get("mode") == "debug" else "coder"
    return "approval" if state.get("is_interactive") or any(p.get("reasons") for p in pending) else "execute"


def approval_node(state: NexuraState, config: RunnableConfig) -> Command:
    """Ask the user (interactive mode, or the risk gate). Payload: {type, task_id, files, diffs, reason, calls}."""
    ctx, task, pending = _ctx(config), _cur(state), state["pending"]
    reasons = [r for p in pending for r in p.get("reasons", [])]
    diffs = [d for p in pending for d in p.get("preview", [])]
    decision = interrupt({
        "type": "approval" if reasons else "tool_approval",
        "task_id": task["step_id"],
        "reason": "; ".join(dict.fromkeys(reasons)) or "interactive mode: approve every tool call",
        "files": sorted({d["file"] for d in diffs}),
        "diffs": diffs,
        "calls": [{"name": p["name"], "args": p["args"]} for p in pending],
    })
    approved, note = approvals.parse_decision(decision)
    ctx.emit("approval_decision", task["step_id"], approved=approved, note=note, reasons=reasons)
    if approved:
        return Command(goto="execute", update={"pending": [{**p, "approved_by": "user"} for p in pending]})

    count = state.get("tool_calls_count", 0) + len(pending)
    rejects = state.get("approvals_rejected", 0) + 1
    update = {
        "messages": [_tool_msg(f"REJECTED by user: {note or 'no reason given'}", p, True) for p in pending],
        "pending": [], "tool_calls_count": count, "approvals_rejected": rejects,
        "execution_history": state["execution_history"] + [_hist(task, p["name"], p["args"], "rejected by user", "rejected") for p in pending],
    }
    if rejects >= app_config.MAX_RETRIES:
        update.update(_fail("APPROVAL_REJECTED: the user rejected the proposed changes repeatedly"))
        return Command(goto="advance_task", update=update)
    if count >= app_config.MAX_TOOL_CALLS_PER_TASK:
        update.update(_fail("TOOL_CAP: too many rejected/failed tool calls"))
        return Command(goto="advance_task", update=update)
    return Command(goto="debugger" if state.get("mode") == "debug" else "coder", update=update)


def _reindex(ctx, task_id: int, paths: list[str]) -> None:
    """Keep the code index fresh after edits. An index problem is logged, never fatal (the V1 context builder is the fallback)."""
    try:
        r = ctx.indexer.update(paths)
        ctx.emit("index_updated", task_id, files=len(paths), **(r or {}))
    except Exception as e:
        ctx.emit("index_update_failed", task_id, error=redact_secrets(str(e))[:300])


def execute_node(state: NexuraState, config: RunnableConfig) -> dict:
    """Runs validated (and, where needed, approved) actions through the execution manager: the only place tools are called."""
    ctx, task = _ctx(config), _cur(state)
    count = state.get("tool_calls_count", 0)
    msgs, hist, changes = [], [], []
    changed = list(state.get("changed_files", []))
    commands = list(state.get("command_results", []))
    lines, completed = state.get("task_lines", 0), False

    for p in state["pending"]:
        action = Action(**p["action"])
        source = p.get("approved_by", "auto")
        ctx.allow_protected = bool(p.get("protected") and source == "user")   # only for an action the user approved
        try:
            res = em.run(action, ctx, task_id=task["step_id"])
        finally:
            ctx.allow_protected = False
        count += 1
        lines += p.get("lines", 0)
        text = res.text()
        safe = redact_secrets(text)
        if safe != text:
            ctx.emit("secret_redacted", task["step_id"], tool=p["name"])
        msgs.append(_tool_msg(safe, p, not res.ok))
        hist.append(_hist(task, p["name"], p["args"], safe, "ok" if res.ok else "error"))
        ctx.emit("tool_call", task["step_id"], agent="executor", tool=p["name"], ok=res.ok, code=res.code,
                 args=redact_secrets(_short(p["args"], 300)), summary=_short(safe, 300))
        if action.action == "read_file" and res.ok and action.path:
            try:
                full = resolve_in_root(ctx.root, action.path)
                ctx.known_hashes[ctx.key(full)] = _sha_file(full) or ""
            except SafetyError:
                pass
        for fc in res.file_changes:
            if ctx.state_manager is not None:
                ctx.state_manager.add_file_change(ctx.run_id, fc, source)
            if ctx.logger is not None:   # edit audit log
                ctx.logger.log(agent="audit", task_id=task["step_id"], result="file_edit",
                               output_summary=f"{fc.file} {fc.change_type} before={str(fc.sha256_before)[:12]} after={str(fc.sha256_after)[:12]} approval={source}")
            key = ctx.key(ctx.root / fc.file)
            ctx.external_edits.discard(key)
            if fc.sha256_after:
                ctx.known_hashes[key] = fc.sha256_after
            else:
                ctx.known_hashes.pop(key, None)
            changes.append(fc.model_dump())
            if fc.file not in changed:
                changed.append(fc.file)
        if action.action == "run_command":
            commands.append({"command": action.command, "exit_code": res.exit_code if res.exit_code is not None else -1})
        if action.action == "task_complete":
            completed = True

    if changes and ctx.indexer is not None:   # invariant: re-indexed before the next task's context is built
        _reindex(ctx, task["step_id"], [c["file"] for c in changes])

    update = {"messages": msgs, "pending": [], "tool_calls_count": count, "changed_files": changed, "task_lines": lines,
              "command_results": commands, "file_changes": state.get("file_changes", []) + changes,
              "execution_history": state["execution_history"] + hist,
              "want_verify": completed or state.get("mode") == "debug"}
    if not update["want_verify"] and count >= app_config.MAX_TOOL_CALLS_PER_TASK:
        print(f"  [!] Hit max tool calls limit ({app_config.MAX_TOOL_CALLS_PER_TASK}). Failing task.")
        update.update(_fail(f"TOOL_CAP: {app_config.MAX_TOOL_CALLS_PER_TASK} tool calls without task_complete"))
    return update


def route_after_execute(state: NexuraState) -> str:
    if state.get("task_outcome"):
        return "advance_task"
    return "verify" if state.get("want_verify") else "coder"


# ─── VERIFY / RETRY ─────────────────────────────────────────

def verify_node(state: NexuraState, config: RunnableConfig) -> dict:
    """A task is done only if verification passes. Failure goes through the retry manager to the debugger."""
    ctx, task = config["configurable"]["project"], _cur(state)
    result = verification.verify(task, ctx, state.get("changed_files", []), state.get("command_results", []))
    for c in result.checks:
        print(f"  [Verify] {'PASS' if c.ok else 'FAIL'} {c.type}: {_short(c.detail, 160)}")
    if result.ok:
        ctx.emit("verification_passed", task["step_id"], checks=len(result.checks))
        return {"task_outcome": "done", "want_verify": False,
                "execution_history": state["execution_history"] + [_hist(task, "verification", "", "passed", "ok")]}

    attempts = state.get("attempts", 0) + 1
    failure = result.failure_text()
    decision = retry_manager.decide(task, attempts, failure)
    entry = {**_hist(task, "verification", "", failure, "error"), "error_type": decision.error_type}
    ctx.emit("verification_failed", task["step_id"], error_type=decision.error_type, attempt=attempts, detail=_short(failure, 300))
    update = {"attempts": attempts, "want_verify": False, "execution_history": state["execution_history"] + [entry]}
    if decision.action == "fail":
        update.update(_fail(f"VERIFICATION_FAILED: {_short(failure, 300)}"))
    else:
        update.update({"mode": "debug", "verify_failure": failure, "retry_hint": decision.hint, "error_type": decision.error_type})
    return update


def route_after_verify(state: NexuraState) -> str:
    return "advance_task" if state.get("task_outcome") else "debugger"


def advance_task_node(state: NexuraState, config: RunnableConfig) -> dict:
    """Records the REAL outcome of the current task and moves to the next runnable one."""
    task = _cur(state)
    sid = str(task["step_id"])
    ctx = _ctx(config)
    ctx.reset_task()

    outcome = state.get("task_outcome")
    status, errors = dict(state.get("task_status") or {}), dict(state.get("task_errors") or {})
    if outcome == "done":
        status[sid], result = "done", "done"
        print(f"[Orchestrator] Task {task['step_id']} completed (verified).")
    else:
        status[sid] = "failed"
        errors[sid] = state.get("task_reason") or "UNKNOWN"
        result = f"failed: {errors[sid]}"
        print(f"[Orchestrator] Task {task['step_id']} FAILED: {errors[sid]}")

    before = dict(status)
    nxt, status = pick_next(state["tasks"], status)
    if ctx.state_manager is not None:
        ctx.state_manager.mark_task(ctx.run_id, task["step_id"], status[sid], attempts=state.get("attempts", 0),
                                    reason=errors.get(sid))
        for k, v in status.items():
            if v == "skipped" and before.get(k) != "skipped":
                ctx.state_manager.mark_task(ctx.run_id, int(k), "skipped")
    ctx.emit("task_done" if outcome == "done" else "task_failed", task["step_id"], reason=errors.get(sid), attempts=state.get("attempts", 0))
    update = {
        "task_status": status, "task_errors": errors,
        "execution_history": state["execution_history"] + [_hist(task, "task_outcome", "", result, "ok" if outcome == "done" else "error")],
        "current_task_index": nxt if nxt is not None else len(state["tasks"]),
        "tool_calls_count": 0, "task_outcome": None, "task_reason": None, "mode": "code", "pending": [],
    }
    if nxt is None:
        update["run_status"] = final_run_status(status)
        _finish_run(ctx, update["run_status"])
    _write_plan_md(ctx, {**state, **update})
    return update


def should_continue(state: NexuraState) -> str:
    if state["current_task_index"] >= len(state["tasks"]):
        print(f"\n[Orchestrator] Run finished: {state.get('run_status')}")
        return "end"
    return "advance" if state.get("task_outcome") else "continue"


# ─── GRAPH ASSEMBLY ─────────────────────────────────────────

def build_graph(interactive: bool = False, checkpointer: Any = None):
    """Builds and compiles the LangGraph StateGraph."""
    b = StateGraph(NexuraState)
    for name, fn in [("planner", planner_node), ("review_plan", review_plan_node), ("task_picker", task_picker_node),
                     ("coder", coder_node), ("debugger", debugger_node), ("validate", validate_node),
                     ("approval", approval_node), ("execute", execute_node), ("verify", verify_node),
                     ("advance_task", advance_task_node)]:
        b.add_node(name, fn)

    b.add_conditional_edges(START, route_start, {"planner": "planner", "task_picker": "task_picker"})
    b.add_conditional_edges("planner", route_after_planner, {"review_plan": "review_plan", END: END})
    # review_plan, approval: Command(goto=...)
    b.add_conditional_edges("task_picker", should_continue, {"continue": "coder", "end": END, "advance": "advance_task"})
    agent_routes = {"validate": "validate", "advance_task": "advance_task", "coder": "coder", "debugger": "debugger"}
    b.add_conditional_edges("coder", route_after_agent, agent_routes)
    b.add_conditional_edges("debugger", route_after_agent, agent_routes)
    b.add_conditional_edges("validate", route_after_validate, {
        "approval": "approval", "execute": "execute", "advance_task": "advance_task", "coder": "coder", "debugger": "debugger"})
    b.add_conditional_edges("execute", route_after_execute, {"verify": "verify", "coder": "coder", "advance_task": "advance_task"})
    b.add_conditional_edges("verify", route_after_verify, {"advance_task": "advance_task", "debugger": "debugger"})
    b.add_edge("advance_task", "task_picker")

    return b.compile(checkpointer=checkpointer or InMemorySaver())
