"""V2-1.8: the contract every ExecutionBackend must pass. Subclass BackendContract and override make_backend() for a new backend."""
import os
import sys
import time

import psutil
import pytest

import config
from core.execution_backend import ExecutionBackend, LocalBackend, make_backend
from core.schemas import Action, ProjectContext

PY = sys.executable


class BackendContract:
    def make_backend(self) -> ExecutionBackend:
        raise NotImplementedError

    @pytest.fixture
    def backend(self):
        b = self.make_backend()
        yield b
        b.stop_all()

    def test_has_a_name(self, backend):
        assert isinstance(backend.name, str) and backend.name

    def test_run_ok(self, backend, tmp_path):
        r = backend.run([PY, "-c", "print('hello')"], str(tmp_path), timeout=30)
        assert r.ok and r.exit_code == 0 and "hello" in r.stdout and r.code is None

    def test_run_failure_reports_exit_code_and_output(self, backend, tmp_path):
        r = backend.run([PY, "-c", "import sys; print('boom'); sys.exit(3)"], str(tmp_path), timeout=30)
        assert not r.ok and r.exit_code == 3 and r.code == "COMMAND_FAILED" and "boom" in r.stdout

    def test_run_timeout_kills_and_reports(self, backend, tmp_path):
        t0 = time.time()
        r = backend.run([PY, "-c", "import time; print('start', flush=True); time.sleep(60)"], str(tmp_path), timeout=2)
        assert not r.ok and r.code == "TIMEOUT" and r.exit_code is None and time.time() - t0 < 30

    def test_output_is_truncated(self, backend, tmp_path):
        r = backend.run([PY, "-c", "print('x' * 50000)"], str(tmp_path), timeout=30, max_bytes=1000)
        assert r.truncated and len(r.stdout) < 2000

    def test_runs_in_the_given_cwd_only(self, backend, tmp_path):
        r = backend.run([PY, "-c", "import os; open('made.txt', 'w').write('1'); print(os.getcwd())"], str(tmp_path), timeout=30)
        assert r.ok and (tmp_path / "made.txt").exists()
        assert os.path.normcase(os.path.realpath(r.stdout.strip())) == os.path.normcase(os.path.realpath(tmp_path))

    def test_env_is_passed(self, backend, tmp_path):
        env = dict(os.environ, NEXURA_CONTRACT="42")
        r = backend.run([PY, "-c", "import os; print(os.environ['NEXURA_CONTRACT'])"], str(tmp_path), timeout=30, env=env)
        assert r.ok and "42" in r.stdout

    def test_missing_executable_raises_oserror(self, backend, tmp_path):
        with pytest.raises(OSError):
            backend.run(["definitely-not-a-real-program-xyz"], str(tmp_path), timeout=10)

    def test_background_process_output_and_stop_kills_the_tree(self, backend, tmp_path):
        child = "import time, sys; print('child', flush=True); time.sleep(120)"
        parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); print('parent up', flush=True); time.sleep(120)"
        h = backend.start_process([PY, "-c", parent], str(tmp_path), "parent")
        pid = backend.pid(h)
        deadline = time.time() + 20
        out = ""
        while "parent up" not in out and time.time() < deadline:
            out += backend.read_process_output(h, 10_000) or ""
            time.sleep(0.2)
        assert "parent up" in out and "running" in out
        kids = psutil.Process(pid).children(recursive=True)
        assert backend.stop_process(h) is True
        for p in [psutil.Process(pid) if psutil.pid_exists(pid) else None, *kids]:
            if p is not None:
                assert not p.is_running() or p.status() == psutil.STATUS_ZOMBIE
        assert backend.stop_process(h) is False and backend.read_process_output(h, 100) is None

    def test_stop_all(self, backend, tmp_path):
        for _ in range(2):
            backend.start_process([PY, "-c", "import time; time.sleep(120)"], str(tmp_path), "sleep")
        assert len(backend) == 2
        backend.stop_all()
        assert len(backend) == 0


class TestLocalBackend(BackendContract):
    def make_backend(self):
        return LocalBackend()


# ─── V2-1.6: wiring ─────────────────────────────────────────

def test_config_selects_the_local_backend_by_default():
    assert config.EXECUTION_BACKEND == "local" and isinstance(make_backend(), LocalBackend)


def test_unknown_backend_is_a_config_error():
    with pytest.raises(config.ConfigError, match="docker"):
        config.env_choice("NEXURA_EXECUTION_BACKEND", "local", ("local",), {"NEXURA_EXECUTION_BACKEND": "docker"})
    with pytest.raises(config.ConfigError):
        make_backend("docker")


class RecordingBackend(LocalBackend):
    name = "recording"

    def __init__(self):
        super().__init__()
        self.calls = []

    def run(self, argv, cwd, timeout, env=None, max_bytes=None, echo=True):
        self.calls.append(("run", list(argv)))
        return super().run(argv, cwd, timeout, env, max_bytes, echo)

    def start_process(self, argv, cwd, command):
        self.calls.append(("start", list(argv)))
        return super().start_process(argv, cwd, command)


@pytest.fixture
def rec_ctx(tmp_path):
    ctx = ProjectContext(run_id="t", root=tmp_path, backend=RecordingBackend())
    yield ctx
    ctx.backend.stop_all()


def test_commands_go_through_the_context_backend(rec_ctx):
    from core import execution_manager
    r = execution_manager.run(Action(action="run_command", command="python -c \"print(7)\""), rec_ctx)
    assert r.ok and "7" in r.stdout
    assert rec_ctx.backend.calls and rec_ctx.backend.calls[0][0] == "run"


def test_validator_and_allowlist_run_before_the_backend(rec_ctx):
    from core import execution_manager, validator
    bad = Action(action="run_command", command="curl http://example.com")
    assert not validator.validate(bad, rec_ctx).ok
    r = execution_manager.run(bad, rec_ctx)   # even if something skipped the validator, the allowlist still applies first
    assert not r.ok and rec_ctx.backend.calls == []


def test_syntax_check_uses_the_backend(rec_ctx):
    from core import execution_manager
    (rec_ctx.root / "ok.py").write_text("x = 1\n", encoding="utf-8")
    rec_ctx.files_read.add(rec_ctx.key(rec_ctx.root / "ok.py"))
    r = execution_manager.run(Action(action="verify_code_syntax", path="ok.py"), rec_ctx)
    assert r.ok and any(c[0] == "run" and "py_compile" in c[1] for c in rec_ctx.backend.calls)


def test_processes_alias_still_works(rec_ctx):
    assert rec_ctx.processes is rec_ctx.backend
