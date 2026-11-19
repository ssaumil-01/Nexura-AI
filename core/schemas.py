"""Pydantic models shared across the control plane."""
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

import config
from core.safety import rel_key


def _default_backend():
    from core.execution_backend import make_backend   # lazy: execution_backend imports this module
    return make_backend()


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
    allow_protected: bool = False  # set only while executing an action the user explicitly approved
    trusted: bool = True           # False (untrusted VS Code workspace): every mutating action is rejected
    dirty_files: set[str] = Field(default_factory=set)   # files with unsaved/uncommitted user changes: never touched
    git_checkpoints: bool = False  # take a git checkpoint before each task (set by the runner)
    extra_commands: list[str] = Field(default_factory=list)   # executables the user approved for THIS workspace (V2-5.2)
    baseline: Optional[dict] = Field(default=None, exclude=True)   # test results before the run (V2-5.3)
    baseline_command: Optional[str] = None   # the test command the user approved running before/after the run (None = no baseline, '' = asked for but none found)

    # persistence (all optional so tools/tests work without them)
    project_id: Optional[str] = None
    run_dir: Optional[Path] = None          # where plan.json / plan.md / logs live (outside the user's workspace)
    state_manager: Any = Field(default=None, exclude=True)
    logger: Any = Field(default=None, exclude=True)

    # runtime state (not serialised)
    files_read: set[str] = Field(default_factory=set, exclude=True)
    known_hashes: dict[str, str] = Field(default_factory=dict, exclude=True)   # key -> sha256 Nexura last saw/wrote
    external_edits: set[str] = Field(default_factory=set, exclude=True)        # keys the user edited during the run
    backend: Any = Field(default_factory=lambda: _default_backend(), exclude=True)   # core.execution_backend
    indexer: Any = Field(default=None, exclude=True)   # rag.indexer.Indexer when code indexing is enabled (V2)

    def emit(self, type: str, task_id: int | None = None, agent: str = "orchestrator", **data) -> None:
        """Record a lifecycle event in the state DB and the JSONL log (whichever are attached)."""
        if self.state_manager is not None:
            self.state_manager.add_event(self.run_id, type, task_id, **data)
        if self.logger is not None:
            self.logger.log(agent=agent, task_id=task_id, result=type, output_summary=str(data) if data else "")

    @property
    def processes(self):
        """V1 name for the backend (it owns the background processes)."""
        return self.backend

    def key(self, full: Path | str) -> str:
        return rel_key(self.root, full)

    def reset_task(self) -> None:
        """Called between tasks: forget reads, stop background processes."""
        self.files_read.clear()
        self.processes.stop_all()


# ─── PLAN MODELS ────────────────────────────────────────────

from typing import Optional  # noqa: E402

from pydantic import model_validator  # noqa: E402

ACCEPTANCE_TYPES = ("file_exists", "syntax_ok", "command_ok", "output_contains", "http_status")


DEFAULT_PORTS = {"streamlit": 8501, "flask": 5000, "uvicorn": 8000, "http.server": 8000, "manage.py": 8000}


def infer_url(start_command: str) -> str | None:
    import re
    m = re.search(r"(?:--port|--server\.port|-p)[ =]+(\d{2,5})\b", start_command) or re.search(r":(\d{2,5})\b", start_command)
    port = int(m.group(1)) if m else next((p for k, p in DEFAULT_PORTS.items() if k in start_command), None)
    return f"http://127.0.0.1:{port}/" if port else None


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

    @model_validator(mode="before")
    @classmethod
    def _infer_url(cls, data):
        """Models often give only the start command for http_status; take the URL from its port (or the framework default)."""
        if isinstance(data, dict) and data.get("type") == "http_status" and not data.get("url") and data.get("start_command"):
            url = infer_url(str(data["start_command"]))
            if url:
                data = {**data, "url": url}
        return data

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
    files: list[str] = Field(description="Existing project files this step edits (workspace-relative paths); only for existing projects", default_factory=list)
    new_files: list[str] = Field(description="New files this step creates (workspace-relative paths); only for existing projects", default_factory=list)


class ExecutionPlan(BaseModel):
    project_id: str = Field(description="A unique ID for the project")
    goal: str = Field(description="The main goal of the execution plan")
    steps: list[Step] = Field(description="List of execution steps")


# ─── ACTIONS / RESULTS ──────────────────────────────────────

import time as _time  # noqa: E402


class Action(BaseModel):
    """A proposed operation. Agents only ever produce these; the orchestrator validates and executes them."""
    action: str
    path: Optional[str] = None
    content: Optional[str] = None
    command: Optional[str] = None
    search_text: Optional[str] = None
    replace_text: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class FileChange(BaseModel):
    file: str
    change_type: Literal["created", "modified", "deleted"]
    task_id: int
    timestamp: float = Field(default_factory=_time.time)
    sha256_before: Optional[str] = None
    sha256_after: Optional[str] = None


class ExecResult(BaseModel):
    ok: bool = True
    code: Optional[str] = None  # error code when not ok
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    duration_ms: int = 0
    truncated: bool = False
    file_changes: list[FileChange] = Field(default_factory=list)

    def text(self) -> str:
        """The result as the agent should see it."""
        if self.ok:
            return self.stdout
        if self.code == "COMMAND_FAILED":
            return f"Command failed (exit code {self.exit_code}):\n{self.stdout}"
        return f"ERROR: {self.code}: {self.stderr or self.stdout}"


class ValidationResult(BaseModel):
    ok: bool = True
    code: Optional[str] = None
    message: str = ""


class CheckResult(BaseModel):
    type: str
    ok: bool
    detail: str = ""


class VerificationResult(BaseModel):
    ok: bool
    checks: list[CheckResult] = Field(default_factory=list)

    def failure_text(self) -> str:
        return "\n".join(f"- {c.type}: {c.detail}" for c in self.checks if not c.ok)


class TaskRecord(BaseModel):
    task_id: int
    title: str
    description: str = ""
    dependencies: list[int] = Field(default_factory=list)
    acceptance: list[Acceptance] = Field(default_factory=list)
    status: Literal["pending", "running", "done", "failed", "skipped"] = "pending"
    attempts: int = 0
    reason: Optional[str] = None


class LogRecord(BaseModel):
    timestamp: float = Field(default_factory=_time.time)
    project_id: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[int] = None
    agent: str
    prompt_version: Optional[str] = None
    model: Optional[str] = None
    input_summary: str = ""
    output_summary: str = ""
    result: str = ""
    tokens: Optional[int] = None
