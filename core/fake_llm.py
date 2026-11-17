"""TEST-ONLY scripted models, enabled by NEXURA_FAKE_LLM=1 (used by the extension's end-to-end smoke test; never in production).
Planner: one step that creates hello.txt (or package.json if the request mentions "risky", to exercise approvals).
Coder: creates the file and requests completion. No network, no API keys."""
import json
import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def _plan(prompt: str) -> str:
    risky = "risky" in prompt.lower()
    name = "package.json" if risky else "hello.txt"
    return json.dumps({"project_id": "fake", "goal": f"Create {name}", "steps": [{
        "step_id": 1, "title": f"Create {name}", "description": f"Create the file {name}", "dependencies": [],
        "acceptance": [{"type": "file_exists", "path": name}]}]})


class ScriptedModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "nexura-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        first = str(messages[0].content) if messages else ""
        if any(isinstance(m, ToolMessage) for m in messages):
            msg = AIMessage(content="", tool_calls=[{"name": "task_complete", "args": {"summary": "done"}, "id": "fake-done"}])
        elif "Debugger Agent" in first:
            msg = AIMessage(content="", tool_calls=[{"name": "list_directory", "args": {"target_dir": "."}, "id": "fake-ls"}])
        else:
            name = "package.json" if "package.json" in first else "hello.txt"
            body = "{}\n" if name == "package.json" else "Hello from Nexura\n"
            msg = AIMessage(content="", tool_calls=[
                {"name": "create_file", "args": {"target_file": name, "content": body}, "id": "fake-create"},
                {"name": "task_complete", "args": {"summary": f"created {name}"}, "id": "fake-done"}])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def install() -> None:
    """Replace the real models with the scripted ones for this process."""
    import agents.llm
    import agents.planner
    os.environ.setdefault("GOOGLE_API_KEY", "fake-key-for-tests")
    os.environ.setdefault("PLANNER_MODEL_NAME", "fake")
    os.environ.setdefault("CODER_MODEL_NAME", "fake")
    agents.llm.set_llm(ScriptedModel())
    agents.planner.PlannerAgent.call_llm = lambda self, prompt: _plan(prompt)
