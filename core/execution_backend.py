"""Where commands actually run. Validation, the allowlist, path checks, approvals and protected paths all happen BEFORE a backend
is called (validator + core.safety.parse_command); a backend only runs an already-approved argv inside the workspace.

V2 has one backend, LocalBackend (V1's runner, unchanged). The interface exists so another backend can be added later
without touching the control plane."""
from abc import ABC, abstractmethod

import config
from core.processes import ProcessManager, run_bounded
from core.schemas import ExecResult


class ExecutionBackend(ABC):
    name: str = "abstract"

    @abstractmethod
    def run(self, argv: list[str], cwd: str, timeout: int, env: dict | None = None, max_bytes: int | None = None,
            echo: bool = True) -> ExecResult:
        """Run to completion. ok/exit_code/stdout (stderr merged); code TIMEOUT or COMMAND_FAILED on failure.
        Raises OSError if the program cannot be started."""

    @abstractmethod
    def start_process(self, argv: list[str], cwd: str, command: str) -> str:
        """Start a background process and return a handle."""

    @abstractmethod
    def read_process_output(self, handle: str, max_bytes: int) -> str | None:
        """New output since the last read plus running/exited status; None for an unknown handle."""

    @abstractmethod
    def stop_process(self, handle: str) -> bool:
        """Stop the process and everything it spawned. False for an unknown handle."""

    @abstractmethod
    def stop_all(self) -> None: ...

    @abstractmethod
    def pid(self, handle: str) -> int | None: ...

    @abstractmethod
    def __len__(self) -> int: ...


class LocalBackend(ExecutionBackend):
    """Runs on this machine with shell=False and process-tree kill (exactly V1's behavior)."""
    name = "local"

    def __init__(self):
        self._procs = ProcessManager()

    def run(self, argv, cwd, timeout, env=None, max_bytes=None, echo=True) -> ExecResult:
        code, out, timed_out = run_bounded(argv, cwd, timeout, max_bytes or config.MAX_TOOL_OUTPUT_BYTES, echo=echo, env=env)
        truncated = "[truncated]" in out
        if timed_out:
            return ExecResult(ok=False, code="TIMEOUT", exit_code=None, stdout=out, truncated=truncated)
        if code != 0:
            return ExecResult(ok=False, code="COMMAND_FAILED", exit_code=code, stdout=out, truncated=truncated)
        return ExecResult(ok=True, exit_code=0, stdout=out, truncated=truncated)

    def start_process(self, argv, cwd, command) -> str:
        return self._procs.start(argv, cwd, command)

    def read_process_output(self, handle, max_bytes):
        return self._procs.read(handle, max_bytes)

    def stop_process(self, handle) -> bool:
        return self._procs.stop(handle)

    def stop_all(self) -> None:
        self._procs.stop_all()

    def pid(self, handle):
        return self._procs.pid(handle)

    def __len__(self):
        return len(self._procs)


BACKENDS = {"local": LocalBackend}


def make_backend(name: str | None = None) -> ExecutionBackend:
    name = name or config.EXECUTION_BACKEND
    if name not in BACKENDS:
        raise config.ConfigError(f"Execution backend {name!r} is not available. Use one of: {', '.join(BACKENDS)}.")
    return BACKENDS[name]()
