import os
import subprocess
import threading
from langchain_core.tools import tool

# Module-level workspace path — set dynamically by main.py before graph runs
WORKSPACE_PATH = ""

BLOCKED_COMMANDS = ["rm -rf", "sudo", "del /s /q", "format"]


def _validate_command(command: str) -> str | None:
    """Returns an error string if command is dangerous, None if OK."""
    cmd_lower = command.lower()
    for blocked in BLOCKED_COMMANDS:
        if blocked in cmd_lower:
            return f"ERROR: Blocked dangerous command sequence '{blocked}' in: {command}"
    return None


def _run_with_streaming(command: str, timeout: int = 60) -> str:
    """Runs a command with real-time stdout/stderr streaming."""
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=WORKSPACE_PATH,
            bufsize=1
        )
        
        output_lines = []
        
        def read_output():
            for line in process.stdout:
                line = line.rstrip()
                if line:
                    print(f"    | {line}")
                    output_lines.append(line)
        
        reader_thread = threading.Thread(target=read_output, daemon=True)
        reader_thread.start()
        
        process.wait(timeout=timeout)
        reader_thread.join(timeout=2)
        
        output = "\n".join(output_lines)
        
        if process.returncode != 0:
            return f"Command failed (exit code {process.returncode}):\n{output}"
        return output if output.strip() else "Command executed successfully (no output)."
        
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        output = "\n".join(output_lines) if output_lines else ""
        return f"ERROR: Command timed out after {timeout}s. Partial output:\n{output}"
    except Exception as e:
        return f"ERROR: {str(e)}"


@tool
def run_command(command: str) -> str:
    """Executes a safe shell command in the workspace directory.
    IMPORTANT: The system runs on Windows. Use Windows commands (dir, type, del) not Linux (ls, cat, rm)."""
    err = _validate_command(command)
    if err:
        return err
    return _run_with_streaming(command, timeout=60)


@tool
def install_dependency(command: str) -> str:
    """Installs a language or system dependency using the provided command (e.g. 'pip install requests', 'npm install')."""
    err = _validate_command(command)
    if err:
        return err
    return _run_with_streaming(command, timeout=120)
