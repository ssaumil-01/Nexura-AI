from typing import TypedDict, Annotated, Optional
from langgraph.graph.message import add_messages


class NexuraState(TypedDict):
    """Shared state that flows through every node in the LangGraph pipeline."""
    
    # LLM conversation history (auto-appended via add_messages)
    messages: Annotated[list, add_messages]
    
    # Original user request
    user_prompt: str
    
    # Path to project workspace
    workspace_path: str
    
    # Generated plan (dict from planner)
    plan: Optional[dict]
    
    # List of tasks extracted from plan
    tasks: list[dict]
    
    # Index of the current task being executed
    current_task_index: int
    
    # Log of all tool call results for propagation to future tasks
    execution_history: list[dict]
    
    # Whether human-in-the-loop is enabled
    is_interactive: bool
    
    # User feedback on the plan (used for re-planning loop)
    plan_feedback: Optional[str]
    
    # Counter for tool calls in the current task (prevents infinite loops)
    tool_calls_count: int

    # Per-task status keyed by str(step_id): pending | running | done | failed | skipped
    task_status: dict[str, str]

    # Failure reason per failed task, keyed by str(step_id)
    task_errors: dict[str, str]

    # running | completed | completed_with_failures | failed | cancelled
    run_status: Optional[str]

    # Why the run could not even start/continue (e.g. planner failure, replan cap)
    run_error: Optional[str]

    # Transient: outcome of the current task ("done" | "failed") once decided, plus the reason
    task_outcome: Optional[str]
    task_reason: Optional[str]

    # How many times the user asked for a re-plan
    replan_count: int
