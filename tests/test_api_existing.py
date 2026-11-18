"""V2-5: profile / commands / summary endpoints, baseline flag on runs, per-file approval, token on every new endpoint."""
from fastapi.testclient import TestClient

from tests.test_api import api, approve_plan, events, msg, plan_of, start, wait  # noqa: F401  (fixture + helpers)

NEW_ENDPOINTS = [("get", "/workspaces/{w}/profile"), ("post", "/workspaces/{w}/profile/detect"), ("put", "/workspaces/{w}/profile"),
                 ("get", "/workspaces/{w}/commands"), ("post", "/workspaces/{w}/commands"), ("delete", "/workspaces/{w}/commands/poetry"),
                 ("get", "/workspaces/{w}/summary"), ("post", "/workspaces/{w}/summary"), ("get", "/runs/aaaaaaaa/plans"),
                 ("get", "/runs/aaaaaaaa/plans/1")]


def test_new_endpoints_need_the_token(api):  # noqa: F811
    bare = TestClient(api.app, base_url="http://127.0.0.1")
    for method, url in NEW_ENDPOINTS:
        assert getattr(bare, method)(url.format(w=api.wid)).status_code == 401, url
        assert getattr(bare, method)(url.format(w=api.wid), headers={"Authorization": "Bearer nope"}).status_code == 401, url


def test_unknown_workspace_is_404_and_bad_id_is_422(api):  # noqa: F811
    assert api.get("/workspaces/aaaaaaaa/profile").status_code == 404
    assert api.get("/workspaces/..%2f/profile").status_code in (404, 422)
    assert api.post("/workspaces/aaaaaaaa/commands", json={"exe": "poetry"}).status_code == 404


def test_profile_is_detected_stored_and_overridable(api):  # noqa: F811
    (api.ws / "requirements.txt").write_text("flask\npytest\n")
    (api.ws / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    v = api.get(f"/workspaces/{api.wid}/profile").json()
    assert v["profile"]["frameworks"] == ["Flask"] and v["profile"]["test_commands"] == ["python -m pytest"] and v["needs_approval"] == []
    put = api.put(f"/workspaces/{api.wid}/profile", json={"overrides": {
        "test_commands": ["make test"], "bogus": ["x"], "run_commands": "not a list", "lint_command": "ruff check ."}}).json()
    assert put["profile"]["test_commands"] == ["make test"] and put["profile"]["lint_command"] == "ruff check ."
    assert put["overrides"] == {"test_commands": ["make test"], "lint_command": "ruff check ."}     # junk was dropped, not stored
    assert put["needs_approval"] == ["make"]
    again = api.post(f"/workspaces/{api.wid}/profile/detect", json={}).json()                      # re-detect keeps overrides
    assert again["profile"]["test_commands"] == ["make test"] and again["detected"]["test_commands"] == ["python -m pytest"]
    assert api.post(f"/workspaces/{api.wid}/profile/detect", json={"subproject": "../x"}).status_code == 422


def test_commands_grant_is_per_workspace_validated_and_revocable(api):  # noqa: F811
    assert api.post(f"/workspaces/{api.wid}/commands", json={"exe": "bash"}).status_code == 422
    assert api.post(f"/workspaces/{api.wid}/commands", json={"exe": "../poetry"}).status_code == 422
    assert api.post(f"/workspaces/{api.wid}/commands", json={"exe": "Poetry"}).json()["granted"] == ["poetry"]
    assert api.get(f"/workspaces/{api.wid}/commands").json()["granted"] == ["poetry"]
    assert api.delete(f"/workspaces/{api.wid}/commands/poetry").json()["granted"] == []


def test_run_with_baseline_but_no_test_command_is_skipped_with_a_notice(api):  # noqa: F811
    run_id = start(api, [msg(("task_complete", {"summary": "ok"}))], baseline=True)
    approve_plan(api, run_id)
    wait(api, run_id, lambda r: r["state"] == "finished")
    evs = events(api, run_id)
    skipped = [e for e in evs if e["event"] == "baseline_skipped"]
    assert skipped and "no test command" in skipped[0]["data"]["reason"]


def test_summary_endpoint_without_a_model_reports_unavailable(api, monkeypatch):  # noqa: F811
    assert api.get(f"/workspaces/{api.wid}/summary").json() == {"summary": None, "version": None}
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("PLANNER_MODEL_NAME", "gemini-test")
    r = api.post(f"/workspaces/{api.wid}/summary")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "SUMMARY_UNAVAILABLE"


def test_summary_endpoint_generates_and_caches(api, monkeypatch):  # noqa: F811
    from agents.planner import PlannerAgent
    monkeypatch.setenv("PLANNER_MODEL_NAME", "gemini-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    (api.ws / "app.py").write_text("print('hi')\n")
    calls = []
    monkeypatch.setattr(PlannerAgent, "call_text", lambda self, p: calls.append(p) or "A tiny script.")
    assert api.post(f"/workspaces/{api.wid}/summary").json() == {"summary": "A tiny script.", "version": 1}
    assert api.post(f"/workspaces/{api.wid}/summary").json()["version"] == 1 and len(calls) == 1
    assert api.get(f"/workspaces/{api.wid}/summary").json()["summary"] == "A tiny script."
    assert api.get(f"/workspaces/{api.wid}/profile").json()["summary"] == "A tiny script."


def test_per_file_approval_runs_the_approved_files_and_rejects_the_rest(api):  # noqa: F811
    (api.ws / "a.txt").write_text("a-old")
    (api.ws / "b.txt").write_text("b-old")
    script = [msg(("read_file", {"target_file": "a.txt"}), ("read_file", {"target_file": "b.txt"})),
              msg(("write_file", {"target_file": "a.txt", "content": "a-new"}), ("write_file", {"target_file": "b.txt", "content": "b-new"})),
              msg(("task_complete", {"summary": "ok"}))]
    run_id = start(api, script, approval_level="ask_every_edit")
    approve_plan(api, run_id)
    r = wait(api, run_id, lambda r: r["state"] == "awaiting_approval")
    assert sorted(r["waiting"]["files"]) == ["a.txt", "b.txt"]
    assert api.post(f"/runs/{run_id}/approve", json={"decision": "approve", "rejected_files": ["b.txt"]}).status_code == 200
    wait(api, run_id, lambda r: r["state"] in ("finished", "awaiting_approval"))
    assert (api.ws / "a.txt").read_text() == "a-new" and (api.ws / "b.txt").read_text() == "b-old"
    api.post(f"/runs/{run_id}/cancel")


def test_per_file_rejecting_everything_is_a_normal_rejection(api):  # noqa: F811
    (api.ws / "a.txt").write_text("a-old")
    script = [msg(("read_file", {"target_file": "a.txt"})), msg(("write_file", {"target_file": "a.txt", "content": "a-new"})),
              msg(("task_complete", {"summary": "ok"}))]
    run_id = start(api, script, approval_level="ask_every_edit")
    approve_plan(api, run_id)
    wait(api, run_id, lambda r: r["state"] == "awaiting_approval")
    api.post(f"/runs/{run_id}/approve", json={"decision": "approve", "rejected_files": ["a.txt"]})
    wait(api, run_id, lambda r: r["state"] in ("finished", "awaiting_approval"))
    assert (api.ws / "a.txt").read_text() == "a-old"
    api.post(f"/runs/{run_id}/cancel")
