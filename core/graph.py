import os
import random
import re
from typing import Any
import time
from dotenv import load_dotenv

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import interrupt, Command
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

import config as app_config
from core.state import NexuraState
from core.context_builder import ContextBuilder
from core.task_manager import final_run_status, pick_next

from tools.file_tools import create_file, write_file, read_file, patch_file, batch_file_operations
from tools.directory_tools import create_directory, list_directory
from tools.system_tools import run_command, install_dependency, start_process, read_process_output, stop_process
from tools.signals import task_complete
from tools.search_tools import web_search
from tools.verification_tools import verify_code_syntax, verify_webapp_endpoint

load_dotenv()

# ─── TOOLS ──────────────────────────────────────────────────

all_tools = [create_file, write_file, read_file, patch_file, batch_file_operations,
             create_directory, list_directory,
             run_command, install_dependency, start_process, read_process_output, stop_process, task_complete,
             web_search, verify_code_syntax, verify_webapp_endpoint]
TOOLS_BY_NAME = {t.name: t for t in all_tools}

# ─── MODEL ──────────────────────────────────────────────────

def get_llm(model_name: str):
    """Factory function to initialize the correct LLM provider."""
    if any(m in model_name.lower() for m in ["llama", "mixtral", "gemma"]):
        # Groq-based models
        return ChatGroq(model=model_name, temperature=0.2)
    else:
        # Default to Google Generative AI
        return ChatGoogleGenerativeAI(model=model_name, temperature=0.2)

coder_model_name = os.getenv("CODER_MODEL_NAME")
llm = get_llm(coder_model_name)
llm_with_tools = llm.bind_tools(all_tools)


# ─── HELPERS ────────────────────────────────────────────────

def _short(value: Any, n: int = 200) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= n else text[:n] + "..."


def _is_error(text: str) -> bool:
    return text.startswith("ERROR") or text.startswith("Command failed") or text.startswith("SKIPPED")


def _backoff_delay(attempt: int, error_text: str) -> float:
    """Seconds to wait after a rate limit: server's retryDelay if present, else exponential; plus jitter."""
    m = re.search(r"retryDelay['\": ]+(\d+(?:\.\d+)?)s", error_text)
    base = float(m.group(1)) + 2 if m else min(5 * 2 ** attempt, 60)
    return min(base, 120) + random.uniform(0, base * 0.25)


def _is_rate_limit(error_text: str) -> bool:
    return "429" in error_text or "RESOURCE_EXHAUSTED" in error_text


# ─── NODE FUNCTIONS ─────────────────────────────────────────

def planner_node(state: NexuraState) -> dict:
    """Generates the execution plan from user prompt. Supports re-planning with feedback."""
    from agents.planner import PlannerAgent, PlanError

    feedback = state.get("plan_feedback")
    previous_plan = state.get("plan")

    if feedback and previous_plan:
        print("\n--- [1] Re-running Planner with feedback ---")
    else:
        print("\n--- [1] Running Planner ---")

    try:
        plan = PlannerAgent().plan(
            user_prompt=state["user_prompt"],
            project_path=state["workspace_path"],
            feedback=feedback,
            previous_plan=previous_plan
        )
    except PlanError as e:
        print(f"[Planner] FAILED: {e}")
        return {"run_status": "failed", "run_error": f"Planner failed: {e}"}

    plan_dict = plan.model_dump()
    tasks = plan_dict.get("steps", [])

    print(f"[Planner] Generated {len(tasks)} tasks for project: {plan_dict.get('project_id', 'unknown')}")

    return {
        "plan": plan_dict,
        "tasks": tasks,
        "current_task_index": 0,
        "plan_feedback": None,  # Clear feedback after re-plan
        "task_status": {str(t["step_id"]): "pending" for t in tasks},
        "task_errors": {},
        "run_status": "running",
        "run_error": None,
    }


def route_after_planner(state: NexuraState) -> str:
    return END if state.get("run_status") == "failed" else "review_plan"


def review_plan_node(state: NexuraState) -> Command:
    """Displays the plan and asks the user for approval or feedback."""
    plan = state["plan"]
    tasks = state["tasks"]

    # Display the plan to the user
    print("\n" + "=" * 50)
    print("GENERATED PLAN")
    print("=" * 50)
    print(f"Project: {plan.get('project_id', 'unknown')}")
    print(f"Goal: {plan.get('goal', '')}")
    print(f"\nSteps ({len(tasks)}):")
    for task in tasks:
        deps = task.get('dependencies', [])
        dep_str = f" (depends on: {deps})" if deps else ""
        print(f"  {task['step_id']}. {task['title']}{dep_str}")
        print(f"     {task['description']}")
        for acc in task.get("acceptance", []):
            print(f"     check: {_short({k: v for k, v in acc.items() if v is not None}, 120)}")
    print("=" * 50)

    # Interrupt and wait for user input
    user_input = interrupt(
        "Review the plan above. Type 'yes' to approve, or provide feedback to re-plan:"
    )

    if user_input.strip().lower() in ["yes", "y", "approve", ""]:
        print("[Review] Plan approved! Starting execution...")
        return Command(goto="task_picker")

    replans = state.get("replan_count", 0)
    if replans >= app_config.MAX_REPLANS:
        msg = f"Re-plan limit reached ({app_config.MAX_REPLANS}). Stopping."
        print(f"[Review] {msg}")
        return Command(goto=END, update={"run_status": "failed", "run_error": msg})
    print("[Review] Feedback received. Re-planning...")
    return Command(
        goto="planner",
        update={"plan_feedback": user_input.strip(), "replan_count": replans + 1}
    )


def task_picker_node(state: NexuraState, config: RunnableConfig) -> dict:
    """Picks the next task and builds the agent prompt with full context."""
    idx = state["current_task_index"]
    tasks = state["tasks"]

    if idx >= len(tasks):
        return {}  # No more tasks — will route to END

    task = tasks[idx]
    config["configurable"]["project"].reset_task()  # fresh read-tracking, no stray processes

    # Build live workspace context
    context_builder = ContextBuilder(state["workspace_path"])
    workspace_context = context_builder.build_context()

    # Build execution history summary (last 5 for token efficiency)
    history_summary = "No previous executions yet."
    if state["execution_history"]:
        history_lines = []
        for h in state["execution_history"][-5:]:
            history_lines.append(f"- Step {h['step_id']}: {h['tool']} -> {h['result'][:100]}")
        history_summary = "\n".join(history_lines)

    print(f"\n--- [Orchestrator] === Executing Task {task['step_id']}: {task['title']} === ---")

    prompt = f"""You are an expert autonomous Coder Agent. Your goal is to complete the following task with MAXIMUM efficiency and HIGH quality.

========================
STRATEGY
========================
1. THINK FIRST: Start each response with a brief assessment of the current state and a plan of action.
2. EFFICIENCY: Call MULTIPLE tools in a single turn. Use `batch_file_operations` for multiple file changes.
3. EXPLORE: If you are unsure about the codebase structure or contents, use `list_directory` and `read_file` before making edits.
4. VERIFY: When possible, use `run_command` or other tools to verify that your changes work as intended.

========================
CURRENT TASK
========================
Task {task['step_id']}: {task['title']}
Description: {task['description']}

========================
PROJECT GOAL
========================
{state['plan'].get('goal', '')}

========================
WORKSPACE CONTEXT
========================
{workspace_context}

========================
RECENT EXECUTION HISTORY
========================
{history_summary}

========================
RULES
========================
1. RAW CONTENT: When writing code to files, provide the RAW code. Do NOT wrap it in markdown code blocks like ```python ... ```.
2. COMMANDS: `run_command` runs ONE program (python, pip, node, npm, npx, uvicorn, pytest, streamlit, flask) with no shell operators (| & ; > <). Servers go through `start_process`; installs through `install_dependency`.
3. FILES: Use `create_file` for NEW files. To change an existing file, `read_file` it first, then `write_file` or `patch_file`. `batch_file_operations` follows the same rules.
4. COMPLETION: Only call `task_complete` after you have verified the task is fully resolved.
"""

    status = dict(state.get("task_status") or {})
    status[str(task["step_id"])] = "running"
    return {
        # Remove ALL previous messages (the add_messages reducer ignores a bare []), then start fresh
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(content=prompt)],
        "tool_calls_count": 0,  # Reset counter for new task
        "task_outcome": None,
        "task_reason": None,
        "task_status": status,
    }


def coder_agent_node(state: NexuraState) -> dict:
    """Calls the LLM with tools bound. Rate limits: exponential backoff + jitter; exhaustion fails the task."""
    detail = ""
    for attempt in range(app_config.MAX_RETRIES):
        try:
            response = llm_with_tools.invoke(state["messages"])
            break
        except Exception as e:
            error_str = str(e)
            if not _is_rate_limit(error_str):
                print(f"  [!] LLM Error: {_short(error_str)}")
                return {"task_outcome": "failed", "task_reason": f"LLM_ERROR: {_short(error_str, 150)}"}
            detail = _short(error_str, 150)
            if attempt + 1 < app_config.MAX_RETRIES:
                wait = _backoff_delay(attempt, error_str)
                print(f"  [!] Rate limited (attempt {attempt + 1}/{app_config.MAX_RETRIES}). Waiting {wait:.1f}s...")
                time.sleep(wait)
    else:
        print("  [!] LLM unavailable: retries exhausted.")
        return {"task_outcome": "failed", "task_reason": f"LLM_UNAVAILABLE: {detail}"}

    if getattr(response, "tool_calls", None):
        return {"messages": [response]}

    # The model answered in prose without calling a tool: nudge it, and count it so this can't loop forever
    count = state.get("tool_calls_count", 0) + 1
    update = {"messages": [response, HumanMessage(content="You must act by calling a tool. Call `task_complete` when the task is done.")],
              "tool_calls_count": count}
    if count >= app_config.MAX_TOOL_CALLS_PER_TASK:
        update.update(task_outcome="failed", task_reason="NO_TOOL_CALLS: model kept answering without calling tools")
    return update


def route_after_coder(state: NexuraState) -> str:
    if state.get("task_outcome"):
        return "advance_task"
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return "coder_agent"  # nudged; ask again
    return "approval" if state.get("is_interactive") else "tools"


def approval_node(state: NexuraState) -> Command:
    """Interactive mode: show the pending tool calls and wait for approve/reject."""
    calls = state["messages"][-1].tool_calls
    task = state["tasks"][state["current_task_index"]]
    decision = interrupt({
        "type": "tool_approval",
        "task_id": task["step_id"],
        "calls": [{"name": c["name"], "args": c["args"]} for c in calls],
    })
    if str(decision).strip().lower() in ("yes", "y", "approve"):
        return Command(goto="tools")

    count = state.get("tool_calls_count", 0) + len(calls)
    update = {
        "messages": [ToolMessage(content=f"REJECTED by user: {decision}", tool_call_id=c["id"], name=c["name"], status="error") for c in calls],
        "tool_calls_count": count,
        "execution_history": state["execution_history"] + [
            {"step_id": task["step_id"], "tool": c["name"], "args": _short(c["args"]), "result": "rejected by user", "status": "rejected"}
            for c in calls],
    }
    if count >= app_config.MAX_TOOL_CALLS_PER_TASK:
        update.update(task_outcome="failed", task_reason="TOOL_CAP: too many rejected/failed tool calls")
        return Command(goto="advance_task", update=update)
    return Command(goto="coder_agent", update=update)


def tools_node(state: NexuraState, config: RunnableConfig) -> dict:
    """Executes the latest batch of tool calls, in order. Counts and logs every call.
    Calls after `task_complete` in the same batch are ignored."""
    calls = state["messages"][-1].tool_calls
    task = state["tasks"][state["current_task_index"]]
    count = state.get("tool_calls_count", 0)
    messages, history, completed = [], [], False

    for tc in calls:
        if completed:
            messages.append(ToolMessage(content="SKIPPED: ignored because task_complete was already called in this batch.",
                                        tool_call_id=tc["id"], name=tc["name"], status="error"))
            continue
        tool = TOOLS_BY_NAME.get(tc["name"])
        if tool is None:
            tm = ToolMessage(content=f"ERROR: UNKNOWN_TOOL: {tc['name']}", tool_call_id=tc["id"], name=tc["name"], status="error")
        else:
            try:
                tm = tool.invoke(tc, config)
            except Exception as e:
                tm = ToolMessage(content=f"ERROR: TOOL_EXCEPTION: {_short(e, 300)}", tool_call_id=tc["id"], name=tc["name"], status="error")
        content = tm.content if isinstance(tm.content, str) else str(tm.content)
        count += 1
        messages.append(tm)
        history.append({"step_id": task["step_id"], "tool": tc["name"], "args": _short(tc["args"]),
                        "result": _short(content), "status": "error" if _is_error(content) or tm.status == "error" else "ok"})
        if tc["name"] == "task_complete":
            completed = True

    update = {"messages": messages, "tool_calls_count": count,
              "execution_history": state["execution_history"] + history}
    if completed:
        update["task_outcome"] = "done"
    elif count >= app_config.MAX_TOOL_CALLS_PER_TASK:
        print(f"  [!] Hit max tool calls limit ({app_config.MAX_TOOL_CALLS_PER_TASK}). Failing task.")
        update.update(task_outcome="failed", task_reason=f"TOOL_CAP: {app_config.MAX_TOOL_CALLS_PER_TASK} tool calls without task_complete")
    return update


def route_after_tools(state: NexuraState) -> str:
    return "advance_task" if state.get("task_outcome") else "coder_agent"


def advance_task_node(state: NexuraState, config: RunnableConfig) -> dict:
    """Records the REAL outcome of the current task and moves to the next runnable one."""
    idx = state["current_task_index"]
    task = state["tasks"][idx]
    sid = str(task["step_id"])
    config["configurable"]["project"].reset_task()

    outcome = state.get("task_outcome")
    status = dict(state.get("task_status") or {})
    errors = dict(state.get("task_errors") or {})
    if outcome == "done":
        status[sid] = "done"
        result = "done"
        print(f"[Orchestrator] Task {task['step_id']} completed.")
    else:
        status[sid] = "failed"
        errors[sid] = state.get("task_reason") or "UNKNOWN"
        result = f"failed: {errors[sid]}"
        print(f"[Orchestrator] Task {task['step_id']} FAILED: {errors[sid]}")

    nxt, status = pick_next(state["tasks"], status)
    update = {
        "task_status": status,
        "task_errors": errors,
        "execution_history": state["execution_history"] + [
            {"step_id": task["step_id"], "tool": "task_outcome", "args": "", "result": result,
             "status": "ok" if outcome == "done" else "error"}],
        "current_task_index": nxt if nxt is not None else len(state["tasks"]),
        "tool_calls_count": 0,
        "task_outcome": None,
        "task_reason": None,
    }
    if nxt is None:
        update["run_status"] = final_run_status(status)
    return update


def should_continue(state: NexuraState) -> str:
    """Check if there are more tasks to execute."""
    if state["current_task_index"] >= len(state["tasks"]):
        print(f"\n[Orchestrator] Run finished: {state.get('run_status')}")
        return "end"
    return "continue"


# ─── GRAPH ASSEMBLY ─────────────────────────────────────────

def build_graph(interactive: bool = False, checkpointer: Any = None):
    """Builds and compiles the LangGraph StateGraph."""

    builder = StateGraph(NexuraState)

    builder.add_node("planner", planner_node)
    builder.add_node("task_picker", task_picker_node)
    builder.add_node("coder_agent", coder_agent_node)
    builder.add_node("approval", approval_node)
    builder.add_node("tools", tools_node)
    builder.add_node("advance_task", advance_task_node)
    builder.add_node("review_plan", review_plan_node)

    builder.add_edge(START, "planner")

    # planner -> review_plan, or END if no valid plan could be produced
    builder.add_conditional_edges("planner", route_after_planner, {"review_plan": "review_plan", END: END})

    # review_plan uses Command() to go to task_picker, planner or END

    builder.add_conditional_edges("task_picker", should_continue, {
        "continue": "coder_agent",
        "end": END
    })

    builder.add_conditional_edges("coder_agent", route_after_coder, {
        "tools": "tools", "approval": "approval", "advance_task": "advance_task", "coder_agent": "coder_agent"
    })

    # approval uses Command() to go to tools, coder_agent or advance_task

    builder.add_conditional_edges("tools", route_after_tools, {
        "advance_task": "advance_task", "coder_agent": "coder_agent"
    })

    builder.add_edge("advance_task", "task_picker")

    if checkpointer is None:
        checkpointer = InMemorySaver()

    return builder.compile(checkpointer=checkpointer)


from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
