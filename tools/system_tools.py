from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from core.processes import run_bounded
from core.safety import SafetyError, parse_command
from tools._ctx import err, get_project


def _run(ctx, command: str, allow_install: bool, timeout: int) -> str:
    try:
        parsed = parse_command(command, ctx.root, allow_install=allow_install)
    except SafetyError as e:
        return err(e.code, e.message)
    try:
        code, out, timed_out = run_bounded(parsed.argv, str(ctx.root), timeout, ctx.limits.max_tool_output_bytes)
    except OSError as e:
        return err("EXEC_FAILED", str(e))
    if timed_out:
        return err("TIMEOUT", f"Command timed out after {timeout}s and was killed. Partial output:\n{out}")
    if code != 0:
        return f"Command failed (exit code {code}):\n{out}"
    return out if out.strip() else "Command executed successfully (no output)."


@tool
def run_command(command: str, config: RunnableConfig) -> str:
    """Runs ONE allowlisted command (python, pip, node, npm, npx, uvicorn, pytest, streamlit, flask) to completion in the workspace.
    No shell operators (| & ; > <), no chaining. Do not use for servers: use start_process.
    Package installs go through install_dependency."""
    ctx = get_project(config)
    return _run(ctx, command, False, ctx.limits.command_timeout_s)


@tool
def install_dependency(command: str, config: RunnableConfig) -> str:
    """Installs project dependencies with 'pip install ...' or 'npm install ...' (e.g. 'pip install requests', 'npm install')."""
    ctx = get_project(config)
    return _run(ctx, command, True, ctx.limits.install_timeout_s)


@tool
def start_process(command: str, config: RunnableConfig) -> str:
    """Starts a long-running allowlisted command (e.g. a web server) in the background and returns a process handle.
    Use read_process_output to see logs and stop_process to stop it. All processes are stopped when the task ends."""
    ctx = get_project(config)
    try:
        parsed = parse_command(command, ctx.root)
        handle = ctx.processes.start(parsed.argv, str(ctx.root), command)
    except SafetyError as e:
        return err(e.code, e.message)
    except OSError as e:
        return err("EXEC_FAILED", str(e))
    return f"Started process {handle}."


@tool
def read_process_output(handle: str, config: RunnableConfig) -> str:
    """Returns new output (since the last read) and the running/exited status of a background process."""
    out = get_project(config).processes.read(handle, get_project(config).limits.max_tool_output_bytes)
    return out if out is not None else err("NO_SUCH_PROCESS", f"No process with handle {handle}.")


@tool
def stop_process(handle: str, config: RunnableConfig) -> str:
    """Stops a background process (and everything it spawned)."""
    if get_project(config).processes.stop(handle):
        return f"Stopped process {handle}."
    return err("NO_SUCH_PROCESS", f"No process with handle {handle}.")
