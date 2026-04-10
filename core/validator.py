import os

class Validator:
    def __init__(self):
        self.blocked_commands = ["rm -rf", "sudo", "del /s /q", "format"]
        self.allowed_actions = ["create_file", "modify_file", "install_dependency", "run_command", "create_directory"]

    def validate_action(self, action_type: str, target: dict) -> bool:
        if action_type not in self.allowed_actions:
            print(f"Validation Error: Unknown action type: {action_type}")
            return False
            
        target_type = target.get("type")
        target_value = target.get("value")
        
        if not target_type or not target_value:
            print("Validation Error: Missing target type or value")
            return False
            
        # Target specific validations
        if target_type in ["file", "directory"] and not self.validate_path(target_value):
            return False
            
        if target_type == "command" and not self.validate_command(target_value):
            return False
            
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
