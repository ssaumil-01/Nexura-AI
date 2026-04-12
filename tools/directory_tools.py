import os
from langchain_core.tools import tool

# Module-level workspace path — set dynamically by main.py before graph runs
WORKSPACE_PATH = ""


@tool
def create_directory(target_dir: str) -> str:
    """Creates a new directory in the workspace."""
    if ".." in target_dir or os.path.isabs(target_dir):
        return f"ERROR: Path traversal or absolute paths are forbidden: {target_dir}"
    
    full_path = os.path.abspath(os.path.join(WORKSPACE_PATH, target_dir))
    if not full_path.startswith(os.path.abspath(WORKSPACE_PATH)):
        return f"ERROR: Cannot create directory outside workspace: {target_dir}"
    
    os.makedirs(full_path, exist_ok=True)
    return f"Successfully created directory: {target_dir}"


@tool
def list_directory(target_dir: str = ".") -> str:
    """Lists all files and folders in a directory within the workspace. 
    Returns a tree-like structure. Use '.' for the workspace root."""
    full_path = os.path.abspath(os.path.join(WORKSPACE_PATH, target_dir))
    if not full_path.startswith(os.path.abspath(WORKSPACE_PATH)):
        return "ERROR: Cannot list directories outside workspace."
    
    if not os.path.exists(full_path):
        return f"ERROR: Directory not found: {target_dir}"
    
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
