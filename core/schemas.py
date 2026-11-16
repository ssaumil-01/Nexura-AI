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
