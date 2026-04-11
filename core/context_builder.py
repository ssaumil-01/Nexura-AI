import os

class ContextBuilder:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        
    def build_context(self) -> str:
        """Reads all textual files in the workspace to build the current state context."""
        if not os.path.exists(self.workspace_path):
            return "Workspace is currently empty."
            
        context_str = ""
        has_files = False
        
        for root, dirs, files in os.walk(self.workspace_path):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            
            for file in files:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, self.workspace_path)
                
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