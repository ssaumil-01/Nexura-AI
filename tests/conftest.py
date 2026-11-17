import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeChatModel(BaseChatModel):
    """Scripted chat model: returns the queued AIMessages in order. Items may be Exceptions (raised)."""

    script: list[Any] = []
    calls: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("FakeChatModel script exhausted")
        item = self.script.pop(0)
        if callable(item):  # lets a test run a side effect (e.g. an external edit) at this point of the script
            item = item()
        if isinstance(item, BaseException):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])


# A check that always passes: for tests of pipeline mechanics whose scripted coder changes nothing
# (a step with no checks and no changes is "nothing verified" and fails).
ALWAYS_OK = [{"type": "command_ok", "command": 'python -c "print(1)"'}]


def tool_call_msg(name: str, args: dict, call_id: str = "c1", content: str = "") -> AIMessage:
    return AIMessage(content=content, tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.fixture
def fake_llm():
    return FakeChatModel(script=[], calls=[])


@pytest.fixture
def workspace(tmp_path) -> Path:
    ws = tmp_path / "proj"
    ws.mkdir()
    return ws


@pytest.fixture
def git_repo(workspace) -> Path:
    def git(*a):
        subprocess.run(["git", *a], cwd=workspace, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (workspace / "README.md").write_text("hi\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    return workspace


# ─── slow tests (end-to-end eval runs): skipped locally unless --runslow; CI and scripts/check.py pass it ───

def pytest_addoption(parser):
    parser.addoption("--runslow", action="store_true", default=False, help="also run tests marked slow")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: end-to-end eval runs (minutes); run with --runslow")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--runslow"):
        return
    skip = pytest.mark.skip(reason="slow: run with --runslow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
