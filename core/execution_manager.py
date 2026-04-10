import os
from tools.file_tools import create_directory, create_file, modify_file
from tools.system_tools import run_command

class ExecutionManager:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        # Ensure workspace exists
        os.makedirs(self.workspace_path, exist_ok=True)

    def execute_task(self, task: dict, content: str = None) -> bool:
        """
        Routes the validated task to the appropriate tool.
        Returns True on success, False on failure.
        """
        action_type = task.get("action_type")
        target_value = task.get("target", {}).get("value")

        try:
            if action_type == "create_directory":
                result = create_directory(self.workspace_path, target_value)
                print(f"[ExecutionManager] Created directory: {result}")
                
            elif action_type == "create_file":
                if content is None:
                    raise ValueError("Content is required for creating a file.")
                result = create_file(self.workspace_path, target_value, content)
                print(f"[ExecutionManager] Created file: {result}")
                
            elif action_type == "modify_file":
                if content is None:
                    raise ValueError("Content is required for modifying a file.")
                result = modify_file(self.workspace_path, target_value, content)
                print(f"[ExecutionManager] Modified file: {result}")
                
            elif action_type in ["run_command", "install_dependency"]:
                print(f"[ExecutionManager] Running command: {target_value}")
                result = run_command(self.workspace_path, target_value)
                print(f"[ExecutionManager] Output:\n{result}")

            else:
                print(f"[ExecutionManager] Error: Unhandled action type '{action_type}'")
                return False

            return True

        except Exception as e:
            print(f"[ExecutionManager] Execution failed: {e}")
            return False
