"""Nexura AI -- central configuration. Importing this module has no network or print side effects."""

import os
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


# --- Limits ------------------------------------------------
MAX_RETRIES = 3
MAX_TOOL_CALLS_PER_TASK = 15
MAX_REPLANS = 5
FAILURE_POLICY = os.getenv("NEXURA_FAILURE_POLICY", "stop")  # stop | continue
MAX_FILE_BYTES = 1_000_000
MAX_TOOL_OUTPUT_BYTES = 8000
CONTEXT_CHAR_BUDGET = 60_000
COMMAND_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 180
TEMPERATURE = 0.2

# --- Safety ------------------------------------------------
ALLOWED_COMMANDS = ["python", "python3", "py", "pip", "pip3", "node", "npm", "npx", "uvicorn", "pytest", "streamlit", "flask"]
ALLOWED_ACTIONS = [
    "create_file", "write_file", "patch_file", "read_file", "batch_file_operations",
    "delete_file", "rename_file",
    "create_directory", "list_directory", "run_command", "install_dependency",
    "start_process", "read_process_output", "stop_process",
    "verify_code_syntax", "verify_webapp_endpoint", "web_search", "task_complete",
]
PROTECTED_PATHS = [
    ".git/", ".env*", ".vscode/", ".github/workflows/",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock", "composer.lock",
    "*.pem", "*.key", "*.p12", "id_rsa*", "credentials*.json", "*.keystore",
]
try:  # extra protected patterns from the editor settings (JSON list in an env var)
    import json as _json
    PROTECTED_PATHS += [str(p) for p in _json.loads(os.getenv("NEXURA_EXTRA_PROTECTED", "[]")) if isinstance(p, str)]
except ValueError:
    pass
APPROVAL_LEVEL = os.getenv("NEXURA_APPROVAL_LEVEL", "ask_risky")  # ask_every_edit | ask_risky | auto_within_limits

# --- Prompts / environment hints ---------------------------
PROMPT_VERSIONS = {"planner": "3", "coder": "2", "debugger": "1"}
SHELL_HINT = os.getenv("NEXURA_SHELL_HINT", "Windows PowerShell" if os.name == "nt" else "POSIX sh")


def model_name(role: str) -> str:
    """Model for 'coder' or 'planner'; raises ConfigError if the env var is unset."""
    var = f"{role.upper()}_MODEL_NAME"
    value = os.getenv(var)
    if not value:
        raise ConfigError(f"{var} is not set. Copy .env.example to .env and set it.")
    return value


def configure_observability() -> None:
    """Enable LangSmith tracing if LANGCHAIN_API_KEY is set. Call from entry points only."""
    api_key = os.getenv("LANGCHAIN_API_KEY", "")
    if api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ.setdefault("LANGCHAIN_PROJECT", "nexura-ai")
        os.environ.setdefault("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
        print(f"[Config] LangSmith tracing ENABLED -> project: {os.environ['LANGCHAIN_PROJECT']}")
    else:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        print("[Config] LangSmith tracing DISABLED (no LANGCHAIN_API_KEY found)")

MAX_BATCH_OPS = 10
RISK_MAX_FILES = 8        # more files than this in one task is "risky"
RISK_MAX_LINES = 400      # more changed lines than this in one task is "risky"
APPROVE_INSTALLS = True   # package installs always need approval

# --- Data locations (the VS Code extension points NEXURA_DATA_DIR at its storage dir) ---
from pathlib import Path  # noqa: E402

DATA_DIR = Path(os.getenv("NEXURA_DATA_DIR") or Path(__file__).resolve().parent / "memory")
DB_PATH = DATA_DIR / "nexura.db"              # state_manager (projects, runs, tasks, file_changes, events)
CHECKPOINT_DB_PATH = DATA_DIR / "checkpoints.db"  # LangGraph checkpoints
