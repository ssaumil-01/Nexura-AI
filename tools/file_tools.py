import os
from langchain_core.tools import tool

# Module-level workspace path — set dynamically by main.py before graph runs
WORKSPACE_PATH = ""


def _validate_path(target: str) -> str | None:
    """Returns an error string if path is unsafe, None if OK."""
    if ".." in target or os.path.isabs(target):
        return f"ERROR: Path traversal or absolute paths are forbidden: {target}"
    full_path = os.path.abspath(os.path.join(WORKSPACE_PATH, target))
    if not full_path.startswith(os.path.abspath(WORKSPACE_PATH)):
        return f"ERROR: Cannot access files outside workspace: {target}"
    return None


@tool
def create_file(target_file: str, content: str) -> str:
    """Creates a new file in the workspace with the specified content."""
    err = _validate_path(target_file)
    if err:
        return err
    
    full_path = os.path.abspath(os.path.join(WORKSPACE_PATH, target_file))
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully created file: {target_file}"


@tool
def write_file(target_file: str, content: str) -> str:
    """Overwrites an existing file in the workspace with new content."""
    err = _validate_path(target_file)
    if err:
        return err
    
    full_path = os.path.abspath(os.path.join(WORKSPACE_PATH, target_file))
    if not os.path.exists(full_path):
        # Create it if it doesn't exist (graceful fallback)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
    
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully updated file: {target_file}"


@tool
def read_file(target_file: str) -> str:
    """Reads and returns the content of a file in the workspace."""
    err = _validate_path(target_file)
    if err:
        return err
    
    full_path = os.path.abspath(os.path.join(WORKSPACE_PATH, target_file))
    if not os.path.exists(full_path):
        return f"ERROR: File not found: {target_file}"
    
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        return f"ERROR: Cannot read binary file: {target_file}"


@tool
def patch_file(target_file: str, search_text: str, replace_text: str) -> str:
    """Patches a file by finding search_text and replacing it with replace_text.
    Much more efficient than rewriting the entire file for small changes.
    The search_text must be an exact match of existing content in the file."""
    err = _validate_path(target_file)
    if err:
        return err
    
    full_path = os.path.abspath(os.path.join(WORKSPACE_PATH, target_file))
    if not os.path.exists(full_path):
        return f"ERROR: File not found: {target_file}"
    
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"ERROR: Cannot read binary file: {target_file}"
    
    if search_text not in content:
        return f"ERROR: search_text not found in {target_file}. Make sure it matches exactly."
    
    # Count occurrences
    count = content.count(search_text)
    new_content = content.replace(search_text, replace_text)
    
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    
    return f"Successfully patched {target_file} ({count} occurrence(s) replaced)."
