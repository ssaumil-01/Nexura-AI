"""V2-4: plan versions, migration 3 from a V1 (schema 2) database, read APIs."""
import json
import sqlite3

from core.state_manager import MIGRATIONS, StateManager
from core.task_manager import load_tasks
from tests.test_api import api, plan_of, start, wait  # noqa: F401  (fixture)


def old_db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for v in (1, 2):
        c.executescript(MIGRATIONS[v])
        c.execute("INSERT INTO schema_version VALUES (?)", (v,))
    c.execute("INSERT INTO runs (id, project_id, prompt, status, plan_json, created_at) VALUES ('r1','p','x','completed',?,5.0)",
              (json.dumps(plan_of("a")),))
    c.execute("INSERT INTO runs (id, project_id, prompt, status, created_at) VALUES ('r2','p','x','failed',6.0)")
    c.commit()
    c.close()


def test_migration_3_moves_current_plan_to_version_1_and_is_idempotent(tmp_path):
    old_db(tmp_path / "n.db")
    sm = StateManager(tmp_path / "n.db")
    assert sm.schema_version() == 3
    assert [v["version"] for v in sm.list_plan_versions("r1")] == [1]
    assert sm.get_plan_version("r1", 1)["plan"]["steps"][0]["title"] == "a"
    assert sm.list_plan_versions("r2") == []                       # a run without a plan gets no version
    assert sm.get_run("r1").plan["steps"][0]["title"] == "a"       # runs.plan_json still current
    again = StateManager(tmp_path / "n.db")
    assert again.schema_version() == 3 and len(again.list_plan_versions("r1")) == 1
    for t in ("project_profile", "repo_summary"):
        assert again._all(f"SELECT COUNT(*) FROM {t}")[0][0] == 0


def test_replan_appends_a_version_and_keeps_history(tmp_path):
    sm = StateManager(tmp_path / "n.db")
    sm.create_run("r", "p", "x")
    p1, p2 = plan_of("one"), plan_of("one", "two")
    assert sm.save_plan("r", p1, load_tasks(p1)) == 1
    assert sm.save_plan("r", p2, load_tasks(p2), feedback="add a second step") == 2
    vs = sm.list_plan_versions("r")
    assert [(v["version"], v["reason"], v["feedback"]) for v in vs] == [(1, "initial", None), (2, "replan", "add a second step")]
    assert len(sm.get_plan_version("r", 1)["plan"]["steps"]) == 1       # history not discarded
    assert len(sm.get_run("r").plan["steps"]) == 2                      # current plan
    assert [t["title"] for t in sm.list_tasks("r")] == ["one", "two"]
    assert sm.get_plan_version("r", 9) is None


def test_plan_api_lists_versions_and_requires_auth(api):  # noqa: F811
    run_id = start(api, [])
    wait(api, run_id, lambda r: r["state"] == "awaiting_plan")
    api.plan["plan"] = plan_of("one", "two")
    api.post(f"/runs/{run_id}/plan", json={"decision": "feedback", "text": "two steps"})
    wait(api, run_id, lambda r: r["state"] == "awaiting_plan" and len(sm_versions(api, run_id)) == 2)
    assert [v["version"] for v in sm_versions(api, run_id)] == [1, 2]
    assert api.get(f"/runs/{run_id}/plans/1").json()["plan"]["steps"][0]["title"] == "one"
    assert len(api.get(f"/runs/{run_id}/plans/2").json()["plan"]["steps"]) == 2
    assert api.get(f"/runs/{run_id}/plans/7").status_code == 404
    assert api.get(f"/runs/{run_id}/plans", headers={"Authorization": "Bearer bad"}).status_code in (401, 403)
    api.post(f"/runs/{run_id}/cancel")


def sm_versions(client, run_id):
    return client.get(f"/runs/{run_id}/plans").json()["versions"]
