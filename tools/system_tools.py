import subprocess

def run_command(base_path: str, command: str) -> str:
    """Safely executes a shell command restricted to the workspace cwd."""
    result = subprocess.run(
        command,
        cwd=base_path,
        shell=True,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {command}\nError: {result.stderr.strip()}")
    return result.stdout.strip()
