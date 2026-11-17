"""Nexura AI -- central configuration. Importing this module has no network or print side effects."""

import os
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def env_choice(name: str, default: str, allowed: tuple, env=None) -> str:
    """Read a setting that must be one of `allowed` (case-insensitive); anything else is a readable ConfigError."""
    value = (os.environ if env is None else env).get(name, default).strip().lower()
    if value not in allowed:
        raise ConfigError(f"{name}={value!r} is not valid. Use one of: {', '.join(allowed)}.")
    return value


def env_int(name: str, default: int, env=None) -> int:
    """Read a positive integer setting."""
    raw = (os.environ if env is None else env).get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError(f"{name}={raw!r} is not valid. Use a whole number of 1 or more.")
    return value


def env_bool(name: str, default: bool, env=None) -> bool:
    raw = (os.environ if env is None else env).get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}={raw!r} is not valid. Use true or false.")


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
LLM_TIMEOUT_S = int(os.getenv("NEXURA_LLM_TIMEOUT_S", "120"))   # per model request; a stalled connection must not hang a run

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
PROMPT_VERSIONS = {"planner": "5", "coder": "2", "debugger": "1"}
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

# --- V2: code intelligence (every feature is off by default, so V1 behavior is unchanged) ---
RETRIEVAL_MODE = env_choice("NEXURA_RETRIEVAL_MODE", "v1", ("v1", "v2"))   # v1 = V1 context builder, v2 = retrieval
INDEXING_ENABLED = env_bool("NEXURA_INDEXING_ENABLED", False)
PATCH_MODE = env_choice("NEXURA_PATCH_MODE", "v1", ("v1", "v2"))
EMBEDDING_MODEL = os.getenv("NEXURA_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")   # local only; downloaded after consent
CHUNK_MAX_TOKENS = env_int("NEXURA_CHUNK_MAX_TOKENS", 400)
CHUNK_MAX_LINES = env_int("NEXURA_CHUNK_MAX_LINES", 80)
INDEX_MAX_FILE_BYTES = env_int("NEXURA_INDEX_MAX_FILE_BYTES", 500_000)
INDEX_MAX_FILES = env_int("NEXURA_INDEX_MAX_FILES", 5000)
TOP_K = env_int("NEXURA_TOP_K", 8)
RRF_K = env_int("NEXURA_RRF_K", 60)
RETRIEVAL_CONTEXT_BUDGET = env_int("NEXURA_RETRIEVAL_CONTEXT_BUDGET", 40_000)   # characters
INDEX_DIR = DATA_DIR / "index"                # index/<workspace_id>/{chroma/, lexical.db, manifest.json}; never inside the workspace
