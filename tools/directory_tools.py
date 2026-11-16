import os
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from core.safety import SafetyError, check_writable, resolve_in_root
from tools._ctx import err, get_project


@tool
def create_directory(target_dir: str, config: RunnableConfig) -> str:
    """Creates a new directory in the workspace."""
    ctx = get_project(config)
    try:
        full = resolve_in_root(ctx.root, target_dir)
        e = check_writable(ctx.key(full), ctx.protected_paths, ctx.allow_protected)
        if e:
            raise e
    except SafetyError as e:
        return err(e.code, e.message)
    os.makedirs(full, exist_ok=True)
    return f"Successfully created directory: {target_dir}"


@tool
def list_directory(config: RunnableConfig, target_dir: str = ".") -> str:
    """Lists all files and folders in a directory within the workspace.
    Returns a tree-like structure. Use '.' for the workspace root."""
    ctx = get_project(config)
    try:
        full_path = str(resolve_in_root(ctx.root, target_dir))
    except SafetyError as e:
        return err(e.code, e.message)

    if not os.path.exists(full_path):
        return err("DIR_NOT_FOUND", f"Directory not found: {target_dir}")

    SKIP_DIRS = {"node_modules", "__pycache__", ".git", "dist", "build", ".vite", "venv"}

    entries = []
    for root, dirs, files in os.walk(full_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]

        level = os.path.relpath(root, full_path).count(os.sep)
        indent = "  " * level
        rel_root = os.path.relpath(root, full_path)
        if rel_root == ".":
            entries.append(f"{target_dir}/")
        else:
            entries.append(f"{indent}{os.path.basename(root)}/")

        sub_indent = "  " * (level + 1)
        for file in files:
            entries.append(f"{sub_indent}{file}")

    return "\n".join(entries) if entries else "Directory is empty."
