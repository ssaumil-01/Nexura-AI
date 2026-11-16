"""Pydantic models shared across the control plane."""
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

import config
from core.processes import ProcessManager
from core.safety import rel_key


class Limits(BaseModel):
    max_file_bytes: int = config.MAX_FILE_BYTES
    max_tool_output_bytes: int = config.MAX_TOOL_OUTPUT_BYTES
    command_timeout_s: int = config.COMMAND_TIMEOUT_S
    install_timeout_s: int = config.INSTALL_TIMEOUT_S
    max_tool_calls: int = config.MAX_TOOL_CALLS_PER_TASK


class ProjectContext(BaseModel):
    """Everything a tool needs to know about the project it is acting on. One per run; never global."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    root: Path
    limits: Limits = Field(default_factory=Limits)
    approval_level: str = config.APPROVAL_LEVEL
    protected_paths: list[str] = Field(default_factory=lambda: list(config.PROTECTED_PATHS))
    ignore_patterns: list[str] = Field(default_factory=list)
    allow_protected: bool = False  # set only after explicit approval (Phase 5)

    # runtime state (not serialised)
    files_read: set[str] = Field(default_factory=set, exclude=True)
    processes: Any = Field(default_factory=ProcessManager, exclude=True)

    def key(self, full: Path | str) -> str:
        return rel_key(self.root, full)

    def reset_task(self) -> None:
        """Called between tasks: forget reads, stop background processes."""
        self.files_read.clear()
        self.processes.stop_all()


# ─── PLAN MODELS ────────────────────────────────────────────

from typing import Literal, Optional  # noqa: E402

from pydantic import model_validator  # noqa: E402

ACCEPTANCE_TYPES = ("file_exists", "syntax_ok", "command_ok", "output_contains", "http_status")


class Acceptance(BaseModel):
    """One objectively checkable condition for a step. Flat (not a union) so it works as an LLM response schema;
    required fields per `type` are enforced by the validator below."""
    type: Literal["file_exists", "syntax_ok", "command_ok", "output_contains", "http_status"]
    path: Optional[str] = Field(default=None, description="file_exists: workspace-relative file path")
    paths: Optional[list[str]] = Field(default=None, description="syntax_ok: workspace-relative files to syntax-check")
    command: Optional[str] = Field(default=None, description="command_ok / output_contains: one command, no shell operators")
    text: Optional[str] = Field(default=None, description="output_contains: text the command output must contain")
    url: Optional[str] = Field(default=None, description="http_status: URL to request")
    start_command: Optional[str] = Field(default=None, description="http_status: command that starts the server first")
    expect: Optional[int] = Field(default=None, description="http_status: expected status code (default 200)")
    wait_s: Optional[int] = Field(default=None, description="http_status: seconds to wait for the server (default 10)")
    timeout_s: Optional[int] = Field(default=None, description="command_ok / output_contains: timeout in seconds")

    @model_validator(mode="after")
    def _required_fields(self):
        need = {"file_exists": ["path"], "syntax_ok": ["paths"], "command_ok": ["command"],
                "output_contains": ["command", "text"], "http_status": ["url"]}[self.type]
        missing = [f for f in need if not getattr(self, f)]
        if missing:
            raise ValueError(f"acceptance '{self.type}' requires {', '.join(missing)}")
        return self


class Step(BaseModel):
    step_id: int = Field(description="Step ID starting from 1, incrementing sequentially")
    title: str = Field(description="Short title for the step")
    description: str = Field(description="Detailed description of what to accomplish in this step")
    dependencies: list[int] = Field(description="step_ids of EARLIER steps this step depends on", default_factory=list)
    acceptance: list[Acceptance] = Field(description="Checks that prove this step is done", default_factory=list)
    documentation_only: bool = Field(description="True only if the step writes docs and nothing can be verified", default=False)


class ExecutionPlan(BaseModel):
    project_id: str = Field(description="A unique ID for the project")
    goal: str = Field(description="The main goal of the execution plan")
    steps: list[Step] = Field(description="List of execution steps")
