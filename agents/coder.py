"""Coder agent: builds the task prompt and asks the model for tool calls. It only PROPOSES; it never executes."""
import config
from agents import llm


def prompt_version() -> str:
    return config.PROMPT_VERSIONS["coder"]


def _acceptance_text(task: dict) -> str:
    acc = task.get("acceptance") or []
    if not acc:
        return "(none listed: changed files must pass a syntax check and any commands you run must exit 0)"
    return "\n".join("- " + ", ".join(f"{k}={v}" for k, v in a.items() if v is not None) for a in acc)


V2_FILES_RULE = ("3. FILES: Use `create_file` for NEW files. To change an existing file, `read_file` it first, then use `apply_patch` "
                 "(one or more {search_text, replace_text} hunks; each search_text must match exactly ONE place, so include a few surrounding lines). "
                 "Prefer `apply_patch` over `write_file` for existing files: it keeps the rest of the file untouched. Use `write_file` only when you "
                 "rewrite most of the file. `batch_file_operations` follows the same rules.")
V1_FILES_RULE = ("3. FILES: Use `create_file` for NEW files. To change an existing file, `read_file` it first, then `write_file` or `patch_file`. "
                 "`batch_file_operations` follows the same rules.")


def build_prompt(task: dict, goal: str, workspace_context: str, history_summary: str) -> str:
    files_rule = V2_FILES_RULE if config.PATCH_MODE == "v2" else V1_FILES_RULE
    return f"""You are an expert autonomous Coder Agent. (prompt version {prompt_version()})
Your goal is to complete the following task with MAXIMUM efficiency and HIGH quality.
You only PROPOSE actions through tool calls; a controller validates and runs them, then verifies the result independently.

========================
STRATEGY
========================
1. THINK FIRST: Start each response with a brief assessment of the current state and a plan of action.
2. EFFICIENCY: Call MULTIPLE tools in a single turn. Use `batch_file_operations` for multiple file changes.
3. EXPLORE: If you are unsure about the codebase structure or contents, use `list_directory` and `read_file` before making edits.
4. VERIFY: Use `run_command` to check that your changes work. Your task is only accepted if the acceptance checks pass.

========================
CURRENT TASK
========================
Task {task['step_id']}: {task['title']}
Description: {task['description']}

========================
ACCEPTANCE CHECKS (these will be run to verify the task)
========================
{_acceptance_text(task)}

========================
PROJECT GOAL
========================
{goal}

========================
WORKSPACE CONTEXT
========================
{workspace_context}

========================
RECENT EXECUTION HISTORY
========================
{history_summary}

========================
RULES
========================
1. RAW CONTENT: When writing code to files, provide the RAW code. Do NOT wrap it in markdown code blocks like ```python ... ```.
2. COMMANDS: run on {config.SHELL_HINT} without a shell. Run tests with `python -m pytest` (not bare `pytest`). `run_command` runs ONE program (python, pip, node, npm, npx, uvicorn, pytest, streamlit, flask) with no shell operators (| & ; > <). Servers go through `start_process`; installs through `install_dependency`.
{files_rule}
4. COMPLETION: Call `task_complete` only after you have verified the task yourself. It is a request: the acceptance checks decide.
"""


def propose(messages: list, on_log=None):
    """Ask the model for the next tool calls. Returns (AIMessage | None, failure_reason | None)."""
    return llm.invoke("coder", messages, prompt_version(), on_log)
