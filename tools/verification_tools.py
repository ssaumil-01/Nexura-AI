import os
import subprocess
import json
import requests
from langchain_core.tools import tool

# Set dynamically by main.py
WORKSPACE_PATH = ""

@tool
def verify_code_syntax(file_path: str) -> str:
    """Verifies the syntax of a given code file without executing its runtime logic. 
    Supports .py, .js, .cpp, .c, and .json files.
    Use this to ensure there are no indentation or basic syntax errors before marking the task complete."""
    
    # Resolve relative paths gracefully
    full_path = file_path
    if not os.path.isabs(file_path) and WORKSPACE_PATH:
        full_path = os.path.join(WORKSPACE_PATH, file_path)
        
    if not os.path.exists(full_path):
        return f"ERROR: File '{file_path}' does not exist."

    ext = os.path.splitext(full_path)[1].lower()
    
    try:
        if ext == ".py":
            # Strict python compilation check
            result = subprocess.run(["python", "-m", "py_compile", full_path], capture_output=True, text=True)
            if result.returncode == 0:
                return "Syntax check passed: No errors found in Python file."
            return f"SyntaxError in Python file:\n{result.stderr}"
            
        elif ext == ".js":
            result = subprocess.run(["node", "-c", full_path], capture_output=True, text=True)
            if result.returncode == 0:
                return "Syntax check passed: No errors found in JavaScript file."
            return f"SyntaxError in JS file:\n{result.stderr}"
            
        elif ext in [".cpp", ".c", ".h"]:
            # Try gcc, fallback if not exists
            result = subprocess.run(["g++", "-fsyntax-only", full_path], capture_output=True, text=True)
            if result.returncode == 0:
                return "Syntax check passed: No errors found in C/C++ file."
            return f"SyntaxError in C/C++ file:\n{result.stderr}"
            
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
    try:
        response = requests.get(url, timeout=5)
        snippet = response.text[:200].replace('\n', ' ')
        return f"Success! Endpoint active. Status code: {response.status_code}. Response snippet: {snippet}..."
    except requests.exceptions.RequestException as e:
        return f"Endpoint check failed. The server might not be running or the URL is incorrect. Error: {e}"
