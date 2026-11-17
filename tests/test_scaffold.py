from tests.conftest import tool_call_msg


def test_fake_llm_scripted(fake_llm, workspace, git_repo):
    fake_llm.script.append(tool_call_msg("task_complete", {"summary": "ok"}))
    msg = fake_llm.invoke("hi")
    assert msg.tool_calls[0]["name"] == "task_complete"
    assert (git_repo / "README.md").exists()
