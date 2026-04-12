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
