import os
from typing import Any
import time
from dotenv import load_dotenv

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt, Command
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage

from core.state import NexuraState
from core.context_builder import ContextBuilder

from tools.file_tools import create_file, write_file, read_file, patch_file, batch_file_operations
from tools.directory_tools import create_directory, list_directory
from tools.system_tools import run_command, install_dependency
from tools.signals import task_complete
from tools.search_tools import web_search
from tools.verification_tools import verify_code_syntax, verify_webapp_endpoint

load_dotenv()

# ─── CONSTANTS ──────────────────────────────────────────────

MAX_TOOL_CALLS_PER_TASK = 15
MAX_LLM_RETRIES = 3

# ─── TOOLS ──────────────────────────────────────────────────

all_tools = [create_file, write_file, read_file, patch_file, batch_file_operations,
             create_directory, list_directory,
             run_command, install_dependency, task_complete,
             web_search, verify_code_syntax, verify_webapp_endpoint]

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


# ─── NODE FUNCTIONS ─────────────────────────────────────────

def planner_node(state: NexuraState) -> dict:
    """Generates the execution plan from user prompt. Supports re-planning with feedback."""
    from agents.planner import PlannerAgent
    
    feedback = state.get("plan_feedback")
    previous_plan = state.get("plan")
    
    if feedback and previous_plan:
        print("\n--- [1] Re-running Planner with feedback ---")
    else:
        print("\n--- [1] Running Planner ---")
    
    planner = PlannerAgent()
    plan = planner.plan(
        user_prompt=state["user_prompt"],
        project_path=state["workspace_path"],
        feedback=feedback,
        previous_plan=previous_plan
    )
    
    if not plan:
        raise ValueError("Planner failed to generate a plan.")
    
    plan_dict = plan.model_dump()
    tasks = plan_dict.get("steps", [])
    
    print(f"[Planner] Generated {len(tasks)} tasks for project: {plan_dict.get('project_id', 'unknown')}")
    
    return {
        "plan": plan_dict,
        "tasks": tasks,
        "current_task_index": 0,
        "plan_feedback": None  # Clear feedback after re-plan
    }


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
    print("=" * 50)
    
    # Interrupt and wait for user input
    user_input = interrupt(
        "Review the plan above. Type 'yes' to approve, or provide feedback to re-plan:"
    )
    
    if user_input.strip().lower() in ["yes", "y", "approve", ""]:
        print("[Review] Plan approved! Starting execution...")
        return Command(goto="task_picker")
    else:
        print(f"[Review] Feedback received. Re-planning...")
        return Command(
            goto="planner",
            update={"plan_feedback": user_input.strip()}
        )


def task_picker_node(state: NexuraState) -> dict:
    """Picks the next task and builds the agent prompt with full context."""
    idx = state["current_task_index"]
    tasks = state["tasks"]
    
    if idx >= len(tasks):
        return {"messages": []}  # No more tasks — will route to END
    
    task = tasks[idx]
    
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
2. POWERSHELL: All commands run via `run_command` must be valid Windows PowerShell.
3. BATCHING: Prefer `batch_file_operations` over multiple individual `write_file` or `patch_file` calls.
4. COMPLETION: Only call `task_complete` after you have verified the task is fully resolved.
"""
    
    return {
        "messages": [HumanMessage(content=prompt)],
        "tool_calls_count": 0  # Reset counter for new task
    }


def coder_agent_node(state: NexuraState) -> dict:
    """Calls the LLM with tools bound. Includes retry logic for rate limit errors."""
    
    for attempt in range(MAX_LLM_RETRIES):
        try:
            response = llm_with_tools.invoke(state["messages"])
            return {"messages": [response]}
        except Exception as e:
            error_str = str(e)
            
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                # Extract retry delay if available
                wait_time = 30  # Default
                if "retryDelay" in error_str:
                    try:
                        import re
                        match = re.search(r'(\d+\.?\d*)s', error_str.split("retryDelay")[1][:30])
                        if match:
                            wait_time = int(float(match.group(1))) + 2
                    except Exception:
                        pass
                
                print(f"  [!] Rate limited (attempt {attempt + 1}/{MAX_LLM_RETRIES}). Waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                # Non-rate-limit error — don't retry
                print(f"  [!] LLM Error: {error_str[:200]}")
                raise
    
    # All retries exhausted
    print("  [!] All retries exhausted. Skipping this tool call.")
    from langchain_core.messages import AIMessage
    return {"messages": [AIMessage(content="I encountered repeated rate limit errors. Marking task as needing attention.")]}


def check_task_complete(state: NexuraState) -> str:
    """After a tool executes, check if task_complete was called or if we hit the cap."""
    last_msg = state["messages"][-1]
    current_count = state.get("tool_calls_count", 0) + 1
    
    # ToolMessage from task_complete signal
    if hasattr(last_msg, "name") and last_msg.name == "task_complete":
        return "advance_task"
    
    # Max tool calls cap — prevent infinite loops
    if current_count >= MAX_TOOL_CALLS_PER_TASK:
        print(f"  [!] Hit max tool calls limit ({MAX_TOOL_CALLS_PER_TASK}). Force-advancing to next task.")
        return "advance_task"
    
    # Otherwise loop back to agent for more tool calls
    return "coder_agent"


def increment_tool_count(state: NexuraState) -> dict:
    """Increments the tool call counter after each tool execution."""
    return {"tool_calls_count": state.get("tool_calls_count", 0) + 1}


def advance_task_node(state: NexuraState) -> dict:
    """Marks the current task as done and advances to the next one."""
    idx = state["current_task_index"]
    task = state["tasks"][idx]
    
    print(f"[Orchestrator] Task {task['step_id']} marked as completed.")
    
    # Log to execution history
    new_history_entry = {
        "step_id": task["step_id"],
        "tool": "task_complete",
        "args": "",
        "result": "completed"
    }
    
    return {
        "current_task_index": idx + 1,
        "execution_history": state["execution_history"] + [new_history_entry],
        "messages": [],  # Clear conversation for next task
        "tool_calls_count": 0  # Reset counter
    }


def should_continue(state: NexuraState) -> str:
    """Check if there are more tasks to execute."""
    if state["current_task_index"] >= len(state["tasks"]):
        print("\n[Orchestrator] [*] All tasks successfully completed!")
        return "end"
    return "continue"


# ─── GRAPH ASSEMBLY ─────────────────────────────────────────

def build_graph(interactive: bool = False, checkpointer: Any = None):
    """Builds and compiles the LangGraph StateGraph."""
    
    builder = StateGraph(NexuraState)
    
    # Add nodes
    builder.add_node("planner", planner_node)
    builder.add_node("task_picker", task_picker_node)
    builder.add_node("coder_agent", coder_agent_node)
    builder.add_node("tools", ToolNode(all_tools))
    builder.add_node("advance_task", advance_task_node)
    builder.add_node("review_plan", review_plan_node)
    
    # Edge: START -> planner
    builder.add_edge(START, "planner")
    
    # Edge: planner -> review_plan (always review after planning)
    builder.add_edge("planner", "review_plan")
    
    # review_plan uses Command() to go to either task_picker or planner
    
    # Conditional: task_picker -> coder_agent (if tasks remain) or END
    builder.add_conditional_edges("task_picker", should_continue, {
        "continue": "coder_agent",
        "end": END
    })
    
    # Conditional: coder_agent -> tools (if tool call) or END (if no tool call)
    builder.add_conditional_edges("coder_agent", tools_condition)
    
    # Conditional: after tools -> check if task_complete was called
    builder.add_conditional_edges("tools", check_task_complete, {
        "advance_task": "advance_task",
        "coder_agent": "coder_agent"  # Loop back for more tool calls
    })
    
    # Edge: advance_task -> task_picker (pick next task)
    builder.add_edge("advance_task", "task_picker")
    
    # Compile with checkpointing
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver
        checkpointer = InMemorySaver()
    
    # HITL: interrupt before tool execution in interactive mode
    # (plan review is handled by interrupt() inside review_plan_node)
    interrupt_nodes = ["tools"] if interactive else []
    
    graph = builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_nodes
    )
    
    return graph