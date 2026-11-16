import socket
import time

import psutil
import pytest

from core.schemas import ProjectContext
from tools.system_tools import install_dependency, read_process_output, run_command, start_process, stop_process
from tools.verification_tools import verify_webapp_endpoint


@pytest.fixture
def ctx(workspace):
    c = ProjectContext(run_id="r", root=workspace)
    yield c
    c.processes.stop_all()


def call(tool, ctx, **args):
    return tool.invoke(args, {"configurable": {"project": ctx}})


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def gone(pid, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.1)
    return False


def test_run_command_ok_and_failure(ctx, workspace):
    (workspace / "ok.py").write_text("print('hello')\n")
    (workspace / "bad.py").write_text("raise SystemExit(3)\n")
    assert call(run_command, ctx, command="python ok.py") == "hello"
    assert call(run_command, ctx, command="python bad.py").startswith("Command failed (exit code 3)")
    assert "SHELL_OPERATOR" in call(run_command, ctx, command="python ok.py | more")
    assert "USE_INSTALL_DEPENDENCY" in call(run_command, ctx, command="pip install x")
    assert "BAD_INSTALL" in call(install_dependency, ctx, command="pip install --index-url http://e/s x")


def test_output_truncated(ctx, workspace):
    (workspace / "big.py").write_text("print('x' * 50000)\n")
    out = call(run_command, ctx, command="python big.py")
    assert len(out) < ctx.limits.max_tool_output_bytes + 100 and "[truncated]" in out


def test_timeout_kills_process_tree(ctx, workspace):
    (workspace / "spawn.py").write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('pid.txt', 'w').write(str(p.pid))\n"
        "time.sleep(60)\n")
    ctx.limits.command_timeout_s = 2
    assert "TIMEOUT" in call(run_command, ctx, command="python spawn.py")
    assert gone(int((workspace / "pid.txt").read_text()))


def test_managed_server_verify_and_cleanup(ctx):
    port = free_port()
    out = call(start_process, ctx, command=f"python -m http.server {port} --bind 127.0.0.1")
    assert out.startswith("Started process")
    handle = out.split()[-1].rstrip(".")
    pid = ctx.processes.pid(handle)
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        res = call(verify_webapp_endpoint, ctx, url=url)
        if res.startswith("Success"):
            break
        time.sleep(0.2)
    assert res.startswith("Success"), res
    assert "running" in call(read_process_output, ctx, handle=handle)
    assert call(stop_process, ctx, handle=handle).startswith("Stopped")
    assert gone(pid) and len(ctx.processes) == 0


def test_reset_task_stops_processes(ctx):
    handle = call(start_process, ctx, command=f"python -m http.server {free_port()} --bind 127.0.0.1").split()[-1].rstrip(".")
    pid = ctx.processes.pid(handle)
    ctx.reset_task()
    assert gone(pid)
