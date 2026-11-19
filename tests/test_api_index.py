"""V2-2.12: index endpoints (token-protected, path-checked), workspace event stream, and runs keeping the index fresh."""
import json
import time

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("chromadb")
import config  # noqa: E402
import server  # noqa: E402
from core.state_manager import StateManager, make_checkpointer  # noqa: E402
from rag.embedder import FakeEmbedder  # noqa: E402
from rag.indexer import IndexManager  # noqa: E402
from tests.test_api import AUTH, TOKEN  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch, git_repo):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "INDEX_DIR", tmp_path / "data" / "index")
    (git_repo / "app.py").write_text("def handler(request):\n    return 'ok'\n", encoding="utf-8")
    mgr = IndexManager(lambda: FakeEmbedder(dim=16))
    app = server.create_app(TOKEN, StateManager(tmp_path / "data" / "n.db"), make_checkpointer(tmp_path / "data" / "c.db"), index_manager=mgr)
    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as c:
        c.wid = c.post("/workspaces", json={"path": str(git_repo), "trusted": True}).json()["workspace_id"]
        c.mgr, c.repo = mgr, git_repo
        yield c


def wait_ready(c, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get(f"/workspaces/{c.wid}/index").json()
        if st["state"] != "indexing":
            return st
        time.sleep(0.05)
    raise AssertionError("index did not finish")


def test_every_index_endpoint_needs_the_token(client):
    bare = TestClient(client.app, base_url="http://127.0.0.1")
    wid = client.wid
    calls = [("post", f"/workspaces/{wid}/index", {}), ("get", f"/workspaces/{wid}/index", None),
             ("post", f"/workspaces/{wid}/index/cancel", None), ("delete", f"/workspaces/{wid}/index", None),
             ("post", f"/workspaces/{wid}/index/changes", {"paths": []}), ("get", f"/workspaces/{wid}/events", None)]
    for method, url, body in calls:
        for headers in ({}, {"Authorization": "Bearer wrong-token-wrong-token"}):
            r = getattr(bare, method)(url, headers=headers, **({"json": body} if body is not None else {}))
            assert r.status_code == 401, (method, url)


def test_build_status_events_and_delete(client):
    r = client.post(f"/workspaces/{client.wid}/index", json={})
    assert r.status_code == 202 and r.json()["state"] == "indexing"
    st = wait_ready(client)
    assert st["state"] == "ready" and st["files"] >= 2 and st["chunks"] >= 1
    with client.stream("GET", f"/workspaces/{client.wid}/events") as s:
        events = [line[len("event: "):] for line in s.iter_lines() if line.startswith("event: ")]
    assert events[0] == "index_started" and events[-1] == "index_finished"
    assert client.delete(f"/workspaces/{client.wid}/index").json() == {"deleted": True}
    assert client.get(f"/workspaces/{client.wid}/index").json()["state"] == "none"
    assert (client.repo / "app.py").exists()


def test_changes_endpoint_rejects_escapes_and_reindexes(client):
    client.post(f"/workspaces/{client.wid}/index", json={})
    wait_ready(client)
    for bad in ("../outside.py", "C:\\Windows\\x.py", "/etc/passwd"):
        r = client.post(f"/workspaces/{client.wid}/index/changes", json={"paths": [bad]})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "OUTSIDE_ROOT", bad
    (client.repo / "app.py").write_text("def brand_new_handler():\n    return 1\n", encoding="utf-8")
    r = client.post(f"/workspaces/{client.wid}/index/changes", json={"paths": ["app.py"]}).json()
    assert r["updated"] and r["indexed"] == 1
    assert "brand_new_handler" in {c.symbol for c in client.mgr.existing(client.wid).lexical.chunks_of("app.py")}


def test_unknown_workspace_is_404(client):
    assert client.get("/workspaces/" + "a" * 32 + "/index").status_code == 404
    assert client.post("/workspaces/not-hex/index", json={}).status_code == 422


def test_indexing_off_is_a_clear_409(tmp_path, monkeypatch, git_repo):
    monkeypatch.setattr(config, "INDEXING_ENABLED", False)
    app = server.create_app(TOKEN, StateManager(tmp_path / "n.db"), make_checkpointer(tmp_path / "c.db"))
    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as c:
        wid = c.post("/workspaces", json={"path": str(git_repo), "trusted": True}).json()["workspace_id"]
        r = c.post(f"/workspaces/{wid}/index", json={})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "INDEXING_DISABLED"


def test_a_run_gets_the_workspace_index(client, monkeypatch):
    import agents.llm as llm_mod
    import agents.planner as planner_mod
    monkeypatch.setenv("NEXURA_FAKE_LLM", "1")
    from core import fake_llm
    monkeypatch.setattr(llm_mod, "_override", None)
    monkeypatch.setattr(planner_mod.PlannerAgent, "call_llm", planner_mod.PlannerAgent.call_llm)
    fake_llm.install()
    client.post(f"/workspaces/{client.wid}/index", json={})
    wait_ready(client)
    rid = client.post("/runs", json={"workspace_id": client.wid, "prompt": "make a file"}).json()["run_id"]
    h = client.app.state.manager.get(rid)
    assert h.ctx.indexer is client.mgr.existing(client.wid)
    client.post(f"/runs/{rid}/cancel")


def test_undo_reindexes_restored_files(client, monkeypatch):
    """After an undo the index must not keep chunks of the undone version."""
    client.post(f"/workspaces/{client.wid}/index", json={})
    wait_ready(client)
    ix = client.mgr.existing(client.wid)
    (client.repo / "app.py").write_text("def undone_version():\n    return 2\n", encoding="utf-8")
    ix.update(["app.py"])
    assert "undone_version" in {c.symbol for c in ix.lexical.chunks_of("app.py")}
    (client.repo / "app.py").write_text("def handler(request):\n    return 'ok'\n", encoding="utf-8")   # what undo restores

    class Res:
        applied, restored, deleted, conflicts, error = True, ["app.py"], [], [], None

    run_id = "ab" * 6
    monkeypatch.setattr(server.runner, "undo", lambda *a, **k: Res())

    class Row:
        id, project_id = run_id, client.wid
    monkeypatch.setattr(client.app.state.sm, "get_run", lambda rid: Row())
    r = client.post(f"/runs/{run_id}/undo", json={"scope": "run"})
    assert r.status_code == 200, r.text
    assert {c.symbol for c in ix.lexical.chunks_of("app.py")} == {"handler"}
    json.dumps(r.json())


# ─── V2-3.7: debug retrieval ────────────────────────────────

def test_debug_retrieve_needs_token_and_a_ready_index(client):
    bare = TestClient(client.app, base_url="http://127.0.0.1")
    assert bare.get(f"/workspaces/{client.wid}/retrieve", params={"q": "handler"}).status_code == 401
    r = client.get(f"/workspaces/{client.wid}/retrieve", params={"q": "handler"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INDEX_NOT_READY"
    assert client.get(f"/workspaces/{client.wid}/retrieve", params={"q": ""}).status_code == 422
    assert client.get(f"/workspaces/{client.wid}/retrieve", params={"q": "x", "k": 999}).status_code == 422


def test_debug_retrieve_returns_reasons_and_never_unsafe_content(client):
    client.post(f"/workspaces/{client.wid}/index", json={})
    wait_ready(client)
    res = client.get(f"/workspaces/{client.wid}/retrieve", params={"q": "handler request", "k": 5}).json()["results"]
    assert res[0]["path"] == "app.py" and res[0]["symbol"] == "handler" and res[0]["sources"] and res[0]["reason"]
    secret = "AIza" + "Z" * 35
    (client.repo / "app.py").write_text(f"def handler(request):\n    key = '{secret}'\n    return 'ok'\n", encoding="utf-8")
    res = client.get(f"/workspaces/{client.wid}/retrieve", params={"q": "handler request"}).json()["results"]
    assert all(secret not in r["snippet"] and r["path"] != "app.py" for r in res)     # stale + now secret: filtered out


def test_retrieval_events_reach_the_run_stream():
    from core.run_manager import _map_db_event
    for t in ("retrieval", "retrieval_fallback", "index_updated", "index_update_failed"):
        [(name, data)] = _map_db_event({"type": t, "task_id": 3, "data": {"reason": "x"}})
        assert name == t and data["task_id"] == 3
