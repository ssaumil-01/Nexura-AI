import os
import json
import requests
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from core.safety import SafetyError, resolve_in_root
from tools._ctx import err, get_project


@tool
def verify_code_syntax(file_path: str, config: RunnableConfig) -> str:
    """Verifies the syntax of a given code file without executing its runtime logic. 
    Supports .py, .js, .cpp, .c, and .json files.
    Use this to ensure there are no indentation or basic syntax errors before marking the task complete."""
    
    ctx = get_project(config)
    try:
        full_path = str(resolve_in_root(ctx.root, file_path))
    except SafetyError as e:
        return err(e.code, e.message)
    if not os.path.exists(full_path):
        return f"ERROR: File '{file_path}' does not exist."

    ext = os.path.splitext(full_path)[1].lower()

    def check(argv):   # through the execution backend, like every other command
        return ctx.backend.run(argv, str(ctx.root), ctx.limits.command_timeout_s, max_bytes=ctx.limits.max_tool_output_bytes, echo=False)
    
    try:
        if ext == ".py":
            # Strict python compilation check
            result = check(["python", "-m", "py_compile", full_path])
            if result.ok:
                return "Syntax check passed: No errors found in Python file."
            return f"SyntaxError in Python file:\n{result.stdout}"
            
        elif ext == ".js":
            result = check(["node", "-c", full_path])
            if result.ok:
                return "Syntax check passed: No errors found in JavaScript file."
            return f"SyntaxError in JS file:\n{result.stdout}"
            
        elif ext in [".cpp", ".c", ".h"]:
            # Try gcc, fallback if not exists
            result = check(["g++", "-fsyntax-only", full_path])
            if result.ok:
                return "Syntax check passed: No errors found in C/C++ file."
            return f"SyntaxError in C/C++ file:\n{result.stdout}"
            
        elif ext == ".json":
            with open(full_path, "r", encoding="utf-8") as f:
                json.load(f)
            return "Syntax check passed: Valid JSON format."
            
        else:
            return f"Language extension '{ext}' is not strictly supported by this static syntax checker. Use `run_command` to execute dynamically."
            
    except json.JSONDecodeError as e:
        return f"SyntaxError in JSON file: {e}"
    except FileNotFoundError as e:
        tool_name = str(e).split("'")[1] if "'" in str(e) else "Compiler"
        return f"ERROR: {tool_name} is not installed on the system to check this syntax."
    except Exception as e:
        return f"Verification error: {e}"

@tool
def verify_webapp_endpoint(url: str) -> str:
    """Pings a live web dashboard or API endpoint (e.g. http://localhost:8501) to verify it is running and accessible.
    Returns the status code and a snippet of the response text."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        return f"ERROR: NOT_LOCAL: Only localhost URLs can be checked, got: {url}"
    try:
        response = requests.get(url, timeout=5)
        snippet = response.text[:200].replace('\n', ' ')
        return f"Success! Endpoint active. Status code: {response.status_code}. Response snippet: {snippet}..."
    except requests.exceptions.RequestException as e:
        return f"Endpoint check failed. The server might not be running or the URL is incorrect. Error: {e}"
