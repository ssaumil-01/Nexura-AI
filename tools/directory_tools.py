import os

def create_directory(base_path: str, target_dir: str) -> str:
    """Safely creates a directory within the workspace."""
    if ".." in target_dir or os.path.isabs(target_dir):
        raise ValueError(f"Invalid path '{target_dir}'. Must be relative and inside the workspace.")
        
    full_path = os.path.abspath(os.path.join(base_path, target_dir))
    if not full_path.startswith(os.path.abspath(base_path)):
        raise ValueError("Cannot perform modifications outside workspace.")

    os.makedirs(full_path, exist_ok=True)
    return full_path
