import subprocess
import platform

def run_command(base_path: str, command: str) -> str:
    """Safely executes a shell command restricted to the workspace cwd."""
    
    # On Windows, using powershell flawlessly handles linux-like paths (./executable) and aliases.
    if platform.system() == "Windows":
        cmd_args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        use_shell = False
    else:
        cmd_args = command
        use_shell = True

    try:
        # Added a 15-second timeout to prevent indefinite hanging on interactive prompts (e.g. std::cin)
        result = subprocess.run(
            cmd_args,
            cwd=base_path,
            shell=use_shell,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15
        )
        
        # Globally strip carriage returns which mangle console outputs on Windows
        stdout_clean = result.stdout.replace('\r', '').strip()
        stderr_clean = result.stderr.replace('\r', '').strip()
        
        if result.returncode != 0:
            error_msg = f"Command '{command}' failed with exit code {result.returncode}.\n"
            if stderr_clean:
                error_msg += f"STDERR:\n{stderr_clean}\n"
            if stdout_clean:
                error_msg += f"STDOUT:\n{stdout_clean}\n"
            raise RuntimeError(error_msg.strip('\n'))
            
        return stdout_clean
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command execution timed out after 15 seconds. Ensure the application is not waiting for user input.")
    except Exception as e:
        raise RuntimeError(f"Error executing command '{command}': {str(e)}")

def install_dependency(base_path: str, command: str) -> str:
    """Executes a dependency installation command."""
    # This acts as an alias to run_command but can be extended with specific env checks.
    print(f"[SystemTools] Installing dependency via command: {command}")
    return run_command(base_path, command)
