"""Shared helpers for tools: fetch the ProjectContext from the LangGraph config and format errors."""
from langchain_core.runnables import RunnableConfig

from core.schemas import ProjectContext


def get_project(config: RunnableConfig | None) -> ProjectContext:
    project = ((config or {}).get("configurable") or {}).get("project")
    if not isinstance(project, ProjectContext):
        raise RuntimeError("No ProjectContext found in config['configurable']['project'].")
    return project


def err(code: str, message: str) -> str:
    return f"ERROR: {code}: {message}"
