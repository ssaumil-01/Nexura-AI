"""Phase 6A: the HTTP API (auth, localhost only, id validation, SSE, plan/approval flow, cancel, undo, resume)."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import config
import server
from core.state_manager import StateManager, make_checkpointer
from core.task_manager import load_tasks
from tests.conftest import ALWAYS_OK

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
COMPLETE = ("task_complete", {"summary": "ok"})


def msg(*calls):
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"c{i}"} for i, (n, a) in enumerate(calls)])


def plan_of(*titles):
    return {"project_id": "p", "goal": "g", "steps": [
        {"step_id": i + 1, "title": t, "description": "d", "dependencies": [], "acceptance": ALWAYS_OK} for i, t in enumerate(titles)]}


@pytest.fixture
def api(tmp_path, monkeypatch, fake_llm, git_repo):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", data / "nexura.db")
    monkeypatch.setattr(config, "CHECKPOINT_DB_PATH", data / "ck.db")
    import agents.llm as llm_mod
    import core.orchestrator as orch
    monkeypatch.setattr(llm_mod, "_override", fake_llm)
    holder = {"plan": plan_of("one")}

    def stub_planner(state, config):
        ctx = config["configurable"]["project"]
        p = holder["plan"]
        ctx.state_manager.save_plan(ctx.run_id, p, load_tasks(p))
        return {"plan": p, "tasks": p["steps"], "current_task_index": 0, "plan_feedback": None,
                "task_status": {str(t["step_id"]): "pending" for t in p["steps"]}, "task_errors": {}, "run_status": "running"}
    monkeypatch.setattr(orch, "planner_node", stub_planner)

    app = server.create_app(TOKEN, StateManager(data / "nexura.db"), make_checkpointer(data / "ck.db"))
    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as client:
        client.ws = git_repo
        client.llm = fake_llm
        client.plan = holder
        client.wid = client.post("/workspaces", json={"path": str(git_repo), "trusted": True}).json()["workspace_id"]
        yield client


def wait(client, run_id, pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/runs/{run_id}").json()
        if pred(r):
            return r
        time.sleep(0.05)
    raise AssertionError(f"timeout; last: { {k: r[k] for k in ('state', 'status', 'waiting')} }")


def start(api, script, plan=None, **body):
    api.llm.script[:] = script
    api.plan["plan"] = plan or plan_of("one")
    r = api.post("/runs", json={"workspace_id": api.wid, "prompt": "do it", **body})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def events(api, run_id, headers=None):
    out = []
    with api.stream("GET", f"/runs/{run_id}/events", headers=headers or {}) as r:
        assert r.status_code == 200
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("id:"):
                eid = int(line.split(":", 1)[1])
            elif line.startswith("data:"):
                out.append({"id": eid, "event": event, "data": json.loads(line.split(":", 1)[1])})
    return out


def approve_plan(api, run_id):
    wait(api, run_id, lambda r: r["state"] == "awaiting_plan")
    assert api.post(f"/runs/{run_id}/plan", json={"decision": "approve"}).status_code == 200


# ─── security ───────────────────────────────────────────────

def test_token_required_everywhere(api):
    bare = TestClient(api.app, base_url="http://127.0.0.1")
    for method, url in [("get", "/health"), ("post", "/workspaces"), ("post", "/runs"), ("get", "/runs/aaaaaaaa"),
                        ("get", "/runs/aaaaaaaa/events"), ("post", "/runs/aaaaaaaa/cancel"), ("get", "/metrics/aaaaaaaa")]:
        assert getattr(bare, method)(url).status_code == 401, url
    assert bare.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert bare.get("/health", headers={"Authorization": TOKEN}).status_code == 401      # scheme required
    assert api.get("/health").json()["ok"] is True


def test_token_must_be_strong():
    with pytest.raises(RuntimeError):
        server.create_app("short")


def test_only_localhost_hosts(api):
    assert api.get("/health", headers={"Host": "evil.example.com"}).status_code == 403
    assert api.get("/health", headers={"Host": "127.0.0.1:5555"}).status_code == 200
    assert api.get("/health", headers={"Host": "localhost:5555"}).status_code == 200
    assert api.get("/health", headers={"Host": "[::1]:5555"}).status_code == 200


def test_no_cors_and_removed_endpoints(api):
    r = api.get("/health", headers={"Origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    for url in ("/files/x", "/file-content/x", "/chat/stream", "/projects"):
        assert api.get(url).status_code in (404, 405), url


@pytest.mark.parametrize("bad", ["abc", "..", "ZZZZZZZZ", "a" * 65, "%27%3B%20DROP", "12345678%00"])
def test_bad_ids_rejected(api, bad):
    for url in (f"/runs/{bad}", f"/runs/{bad}/events", f"/metrics/{bad}"):
        assert api.get(url).status_code in (404, 422), url
    assert api.post(f"/runs/{bad}/cancel").status_code in (404, 422)
    assert api.post("/runs", json={"workspace_id": bad, "prompt": "x"}).status_code in (404, 422)


def test_workspace_registration_rules(api, tmp_path):
    assert api.post("/workspaces", json={"path": "relative/dir"}).status_code == 400
    assert api.post("/workspaces", json={"path": str(tmp_path / "nope")}).status_code == 400
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert api.post("/workspaces", json={"path": str(f)}).status_code == 400
    again = api.post("/workspaces", json={"path": str(api.ws), "trusted": False}).json()["workspace_id"]
    assert again == api.wid and len(again) == 32
    assert api.post("/runs", json={"workspace_id": "a" * 32, "prompt": "x"}).status_code == 404   # never registered
    assert "path" not in server.RunReq.model_fields                                                  # roots are never taken from run requests


def test_request_validation(api):
    assert api.post("/runs", json={"workspace_id": api.wid, "prompt": ""}).status_code == 422
    assert api.post("/runs", json={"workspace_id": api.wid, "prompt": "x", "approval_level": "yolo"}).status_code == 422
    assert api.post("/runs", json={"workspace_id": api.wid, "prompt": "x" * 30000}).status_code == 422


# ─── run flow ───────────────────────────────────────────────

def test_full_run_with_sse_contract_and_reconnect(api):
    run_id = start(api, [msg(("create_file", {"target_file": "a.txt", "content": "A"}), COMPLETE)])
    r = wait(api, run_id, lambda r: r["state"] == "awaiting_plan")
    assert r["waiting"].startswith("Review the plan")
    approve_plan(api, run_id)
    final = wait(api, run_id, lambda r: r["state"] == "finished")
    assert final["status"] == "completed" and final["tasks"][0]["status"] == "done" and (api.ws / "a.txt").read_text() == "A"

    evs = events(api, run_id)
    names = [e["event"] for e in evs]
    for n in ("plan_ready", "task_started", "tool_call", "tool_result", "verification", "task_done", "run_finished"):
        assert n in names, (n, names)
    assert names.index("plan_ready") < names.index("task_started") < names.index("tool_call") < names.index("verification") < names.index("task_done")
    assert names[-1] == "run_finished" and evs[-1]["data"]["run_status"] == "completed"
    assert [e["id"] for e in evs] == list(range(1, len(evs) + 1))
    assert evs[0]["data"]["plan"]["goal"] == "g"
    tc = next(e for e in evs if e["event"] == "tool_call")
    assert tc["data"]["tool"] == "create_file" and tc["data"]["task_id"] == 1

    again = events(api, run_id, {"Last-Event-ID": "3"})                  # reconnect
    assert [e["id"] for e in again] == [e["id"] for e in evs if e["id"] > 3]
    m = api.get(f"/metrics/{run_id}").json()
    assert m["task_success_rate"] == 1.0 and m["status"] == "completed"
    assert api.get(f"/metrics/{'0' * 12}").status_code == 404


def test_plan_feedback_replans_and_wrong_state_is_409(api):
    run_id = start(api, [msg(COMPLETE)])
    wait(api, run_id, lambda r: r["state"] == "awaiting_plan")
    assert api.post(f"/runs/{run_id}/approve", json={"decision": "approve"}).status_code == 409   # not an approval request
    assert api.post(f"/runs/{run_id}/plan", json={"decision": "feedback"}).status_code == 422     # needs text
    assert api.post(f"/runs/{run_id}/plan", json={"decision": "feedback", "text": "add tests"}).status_code == 200
    wait(api, run_id, lambda r: r["state"] == "awaiting_plan")                                       # new plan awaiting review
    approve_plan(api, run_id)
    assert wait(api, run_id, lambda r: r["state"] == "finished")["status"] == "completed"
    assert api.post(f"/runs/{run_id}/plan", json={"decision": "approve"}).status_code == 409


def test_approval_flow_over_api(api):
    (api.ws / "old.txt").write_text("keep")
    run_id = start(api, [msg(("delete_file", {"target_file": "old.txt"})), msg(COMPLETE)])
    approve_plan(api, run_id)
    r = wait(api, run_id, lambda r: r["state"] == "awaiting_approval")
    p = r["waiting"]
    assert p["type"] == "approval" and p["files"] == ["old.txt"] and "delete" in p["reason"] and p["diffs"][0]["change_type"] == "deleted"
    assert api.post(f"/runs/{run_id}/approve", json={"decision": "reject", "note": "keep"}).status_code == 200
    assert wait(api, run_id, lambda r: r["state"] == "finished")["status"] == "completed" and (api.ws / "old.txt").exists()
    assert "approval_required" in [e["event"] for e in events(api, run_id)]


def test_cancel_stops_run_and_releases_workspace(api):
    run_id = start(api, [msg(COMPLETE)])
    wait(api, run_id, lambda r: r["state"] == "awaiting_plan")
    assert api.post(f"/runs/{run_id}/cancel").status_code == 200
    final = wait(api, run_id, lambda r: r["state"] == "finished")
    assert final["status"] == "cancelled" and final["summary"]["run_status"] == "cancelled"
    assert api.post(f"/runs/{run_id}/cancel").status_code == 409                                    # already over
    run2 = start(api, [msg(COMPLETE)])                                                              # lock released: workspace usable again
    assert api.post(f"/runs/{run2}/cancel").status_code == 200


def test_one_run_per_workspace_and_git_errors(api, tmp_path):
    start(api, [msg(COMPLETE)])
    r = api.post("/runs", json={"workspace_id": api.wid, "prompt": "second"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "WORKSPACE_BUSY"
    plain = tmp_path / "plain"
    plain.mkdir()
    wid = api.post("/workspaces", json={"path": str(plain), "trusted": True}).json()["workspace_id"]
    r = api.post("/runs", json={"workspace_id": wid, "prompt": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_A_REPO"
    ok = api.post("/runs", json={"workspace_id": wid, "prompt": "x", "git_init": True})
    assert ok.status_code == 200 and (plain / ".git").is_dir()


def test_untrusted_workspace_cannot_mutate(api, tmp_path):
    ws = tmp_path / "untrusted"
    ws.mkdir()
    wid = api.post("/workspaces", json={"path": str(ws), "trusted": False}).json()["workspace_id"]
    api.llm.script[:] = [msg(("create_file", {"target_file": "x.txt", "content": "1"})), msg(COMPLETE)]
    run_id = api.post("/runs", json={"workspace_id": wid, "prompt": "x", "git_init": True}).json()["run_id"]
    approve_plan(api, run_id)
    wait(api, run_id, lambda r: r["state"] == "finished")
    assert not (ws / "x.txt").exists()


def test_undo_over_api(api):
    run_id = start(api, [msg(("create_file", {"target_file": "a.txt", "content": "A"}), COMPLETE)])
    assert api.post(f"/runs/{run_id}/undo", json={"scope": "run"}).status_code == 409                # still active
    approve_plan(api, run_id)
    wait(api, run_id, lambda r: r["state"] == "finished")
    assert api.post(f"/runs/{run_id}/undo", json={"scope": "task"}).status_code == 422               # needs task_id
    (api.ws / "a.txt").write_text("edited by user")
    r = api.post(f"/runs/{run_id}/undo", json={"scope": "run"}).json()
    assert r["applied"] is False and r["conflicts"] == ["a.txt"]
    r = api.post(f"/runs/{run_id}/undo", json={"scope": "run", "confirm_conflicts": True}).json()
    assert r["applied"] and r["deleted"] == ["a.txt"] and not (api.ws / "a.txt").exists()


def test_resume_over_api(api):
    plan = plan_of("first", "second")
    run_id = start(api, [msg(("create_file", {"target_file": "a.txt", "content": "A"}), COMPLETE), RuntimeError("model crashed")], plan)
    approve_plan(api, run_id)
    first = wait(api, run_id, lambda r: r["state"] == "finished")
    assert first["status"] == "completed_with_failures" and [t["status"] for t in first["tasks"]] == ["done", "failed"]
    api.llm.script[:] = [msg(("create_file", {"target_file": "b.txt", "content": "B"}), COMPLETE)]
    api.llm.calls.clear()
    assert api.post(f"/runs/{run_id}/resume").status_code == 200
    final = wait(api, run_id, lambda r: r["state"] == "finished" and r["status"] == "completed")
    assert [t["status"] for t in final["tasks"]] == ["done", "done"] and (api.ws / "b.txt").exists()
    assert len(api.llm.calls) == 1 and "Task 2" in str(api.llm.calls[0][0].content)                 # task 1 not redone
    assert api.post(f"/runs/{run_id}/resume").status_code == 409                                    # nothing left to resume


def test_external_edit_notification(api):
    (api.ws / "f.txt").write_text("v1")
    run_id = start(api, [msg(("read_file", {"target_file": "f.txt"})), msg(("write_file", {"target_file": "f.txt", "content": "agent"}), COMPLETE)])
    approve_plan(api, run_id)
    api.llm.script[1] = lambda: (api.post(f"/runs/{run_id}/edits", json={"files": ["f.txt"]}), msg(("write_file", {"target_file": "f.txt", "content": "agent"}), COMPLETE))[1]
    r = wait(api, run_id, lambda r: r["state"] == "awaiting_approval")
    assert "modified outside Nexura" in r["waiting"]["reason"]
    api.post(f"/runs/{run_id}/approve", json={"decision": "reject"})
    wait(api, run_id, lambda r: r["state"] in ("finished", "awaiting_approval"))
    assert (api.ws / "f.txt").read_text() == "v1"


def test_api_stays_responsive_while_the_agent_works(api):
    def slow():
        time.sleep(1.0)
        return msg(COMPLETE)
    run_id = start(api, [slow])
    approve_plan(api, run_id)
    t0 = time.time()
    for _ in range(5):
        assert api.get("/health").status_code == 200
    assert time.time() - t0 < 0.8                                    # health answered while the LLM call blocked a worker thread
    wait(api, run_id, lambda r: r["state"] == "finished")


def test_fake_llm_mode_runs_end_to_end_without_a_model(tmp_path, monkeypatch, git_repo):
    """NEXURA_FAKE_LLM=1 (used by the extension smoke test): plan + code + verify with scripted models."""
    import agents.llm as llm_mod
    import agents.planner as planner_mod
    monkeypatch.setenv("NEXURA_FAKE_LLM", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "d")
    monkeypatch.setattr(llm_mod, "_override", None)
    monkeypatch.setattr(planner_mod.PlannerAgent, "call_llm", planner_mod.PlannerAgent.call_llm)  # restored after the test
    app = server.create_app(TOKEN, StateManager(tmp_path / "d" / "n.db"), make_checkpointer(tmp_path / "d" / "c.db"))
    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as c:
        wid = c.post("/workspaces", json={"path": str(git_repo), "trusted": True}).json()["workspace_id"]
        rid = c.post("/runs", json={"workspace_id": wid, "prompt": "make a file"}).json()["run_id"]
        approve_plan(c, rid)
        assert wait(c, rid, lambda r: r["state"] == "finished")["status"] == "completed"
        assert (git_repo / "hello.txt").read_text().startswith("Hello from Nexura")
