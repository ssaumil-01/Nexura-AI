from langchain_core.tools import tool


@tool
def task_complete(summary: str) -> str:
    """Signal that the current task is fully completed.
    Call this ONLY when all necessary tool actions for the current task are done.
    Provide a brief summary of what was accomplished."""
    return f"TASK_COMPLETE: {summary}"
