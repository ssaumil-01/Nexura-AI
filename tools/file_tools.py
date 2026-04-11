import os

def create_file(base_path: str, target_file: str, content: str) -> str:
    """Safely creates a file within the workspace, writing content."""
    full_path = os.path.join(base_path, target_file)
    if not os.path.abspath(full_path).startswith(os.path.abspath(base_path)):
        raise PermissionError(f"Cannot write file outside workspace: {target_file}")
        
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return full_path

def write_file(base_path: str, target_file: str, content: str) -> str:
    """Safely over-writes an existing file within the workspace."""
    full_path = os.path.join(base_path, target_file)
    if not os.path.abspath(full_path).startswith(os.path.abspath(base_path)):
        raise PermissionError(f"Cannot overwrite file outside workspace: {target_file}")
        
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"File to modify does not exist: {target_file}")
        
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return full_path

def read_file(base_path: str, target_file: str) -> str:
    """Safely reads a file from the workspace."""
    full_path = os.path.join(base_path, target_file)
    if not os.path.abspath(full_path).startswith(os.path.abspath(base_path)):
        raise PermissionError(f"Cannot read file outside workspace: {target_file}")
        
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"File to read does not exist: {target_file}")
        
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()
