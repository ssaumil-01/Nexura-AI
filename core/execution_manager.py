import os
from tools.directory_tools import create_directory
from tools.file_tools import create_file, write_file
from tools.system_tools import run_command, install_dependency

class ExecutionManager:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        # Ensure workspace exists
        os.makedirs(self.workspace_path, exist_ok=True)

    def execute_tool_call(self, function_name: str, function_args: dict) -> tuple[bool, str]:
        """
        Routes the native tool call arguments to the Python backend.
        Returns (True, "Success") on success, (False, "Error message") on failure.
        """
        try:
            if function_name == "create_directory":
                result = create_directory(self.workspace_path, function_args.get("target_dir", ""))
                print(f"[ExecutionManager] Created directory: {result}")
                
            elif function_name == "create_file":
                result = create_file(self.workspace_path, function_args.get("target_file", ""), function_args.get("content", ""))
                print(f"[ExecutionManager] Created file: {result}")
                
            elif function_name == "write_file":
                result = write_file(self.workspace_path, function_args.get("target_file", ""), function_args.get("content", ""))
                print(f"[ExecutionManager] Modified file: {result}")
                
            elif function_name == "install_dependency":
                target_cmd = function_args.get("target_value", "")
                print(f"[ExecutionManager] Installing dependency: {target_cmd}")
                result = install_dependency(self.workspace_path, target_cmd)
                print(f"[ExecutionManager] Output:\n{result}")
                
            elif function_name == "run_command":
                target_cmd = function_args.get("target_value", "")
                print(f"[ExecutionManager] Running command: {target_cmd}")
                result = run_command(self.workspace_path, target_cmd)
                print(f"[ExecutionManager] Output:\n{result}")
                
            else:
                print(f"[ExecutionManager] Error: Unhandled tool call '{function_name}'")
                return False, f"Unhandled tool call '{function_name}'"

            return True, "Success"

        except Exception as e:
            print(f"[ExecutionManager] Execution failed: {e}")
            return False, str(e)

