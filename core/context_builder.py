import os

# Directories to skip entirely
IGNORED_DIRS = {
    "node_modules", "__pycache__", ".git", ".next", "dist", "build",
    ".vite", ".cache", "venv", ".env", "coverage", ".turbo"
}

# Files to skip
IGNORED_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    ".DS_Store", "Thumbs.db", "plan.json", "plan.md"
}

# Extensions to skip (binary / non-useful)
IGNORED_EXTENSIONS = {
    ".pyc", ".pyo", ".exe", ".dll", ".so", ".o", ".a",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".map"
}

# Max file size to read (prevent token blowup on large files)
MAX_FILE_SIZE = 10_000  # 10 KB


class ContextBuilder:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        
    def build_context(self) -> str:
        """Reads relevant source files in the workspace to build the current state context."""
        if not os.path.exists(self.workspace_path):
            return "Workspace is currently empty."
            
        context_str = ""
        has_files = False
        
        for root, dirs, files in os.walk(self.workspace_path):
            # Skip hidden + ignored directories
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in IGNORED_DIRS]
            
            for file in files:
                # Skip ignored files
                if file in IGNORED_FILES:
                    continue
                    
                # Skip ignored extensions
                _, ext = os.path.splitext(file)
                if ext.lower() in IGNORED_EXTENSIONS:
                    continue
                
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, self.workspace_path)
                
                # Skip files that are too large
                try:
                    file_size = os.path.getsize(file_path)
                    if file_size > MAX_FILE_SIZE:
                        context_str += f"\n--- {rel_path} ---\n[File too large: {file_size} bytes, skipped]\n"
                        continue
                except OSError:
                    continue
                
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        context_str += f"\n--- {rel_path} ---\n```\n{content}\n```\n"
                        has_files = True
                except UnicodeDecodeError:
                    context_str += f"\n--- {rel_path} ---\n[Binary or Unreadable File]\n"
                except Exception:
                    pass
        
        if not has_files:
            return "Workspace is currently empty."
            
        return context_str.strip()