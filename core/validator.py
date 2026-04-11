import os

class Validator:
    def __init__(self):
        self.blocked_commands = ["rm -rf", "sudo", "del /s /q", "format"]

    def validate_tool_call(self, function_name: str, function_args: dict) -> bool:
        """Validates the native tool call arguments for safety."""
        # Validate path targets
        if function_name in ["create_file", "write_file"]:
            target = function_args.get("target_file", "")
            if not self.validate_path(target): return False
            
        if function_name == "create_directory":
            target = function_args.get("target_dir", "")
            if not self.validate_path(target): return False

        # Validate command strings
        if function_name in ["run_command", "install_dependency"]:
            cmd = function_args.get("target_value", "")
            if not self.validate_command(cmd): return False

        return True

    def validate_command(self, command: str) -> bool:
        cmd_lower = command.lower()
        for blocked in self.blocked_commands:
            if blocked in cmd_lower:
                print(f"Validation Error: Command contains blocked sequence '{blocked}': {command}")
                return False
        return True
        
    def validate_path(self, target_path: str) -> bool:
        if ".." in target_path or os.path.isabs(target_path):
             print(f"Validation Error: Absolute paths or path traversal are forbidden: {target_path}")
             return False
        return True
