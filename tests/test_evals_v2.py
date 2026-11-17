"""V2-1.1..1.4: fixture projects, v2 tasks, retrieval labels and the upgraded eval harness (no LLM, no network)."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / "evals"
TASKS = yaml.safe_load((EV / "tasks_v2.yaml").read_text(encoding="utf-8"))
LABELS = yaml.safe_load((EV / "retrieval_labels.yaml").read_text(encoding="utf-8"))
FIXTURES = yaml.safe_load((EV / "fixtures" / "fixtures.yaml").read_text(encoding="utf-8"))
CHECK_TYPES = {"file_exists", "syntax_ok", "command_ok", "output_contains", "http_status", "python", "node"}
HAS_FLASK = importlib.util.find_spec("flask") is not None


def load_run():
    spec = importlib.util.spec_from_file_location("eval_run", EV / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def env_with_this_python():
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    return env


# ─── task set and labels ────────────────────────────────────

def test_task_set_size_and_shape():
    assert 15 <= len(TASKS) <= 25
    assert len({t["id"] for t in TASKS}) == len(TASKS)
    for t in TASKS:
        assert t["fixture"] in FIXTURES and (EV / "fixtures" / t["fixture"]).is_dir(), t["id"]
        assert t["checks"] and all(c["type"] in CHECK_TYPES for c in t["checks"]), t["id"]


def test_task_mix_covers_the_planned_kinds():
    ids = {t["id"] for t in TASKS}
    assert {"bs_fix_bug", "inv_rename", "jt_rename_format", "fn_delete", "inv_bulk_discount", "fn_title_header", "inv_currency"} <= ids
    # tasks whose key file is not named in the prompt (retrieval has to find it)
    hidden = [t for t in TASKS if LABELS[t["id"]]["files"][0] not in t["prompt"]]
    assert len(hidden) >= 8


def test_every_task_has_labels_that_point_at_real_files():
    assert set(LABELS) == {t["id"] for t in TASKS}
    for t in TASKS:
        lab = LABELS[t["id"]]
        assert lab["files"], t["id"]
        for f in lab["files"]:
            assert (EV / "fixtures" / t["fixture"] / f).is_file(), (t["id"], f)


def test_labelled_symbols_exist_in_the_labelled_files():
    for t in TASKS:
        lab = LABELS[t["id"]]
        text = "".join((EV / "fixtures" / t["fixture"] / f).read_text(encoding="utf-8") for f in lab["files"])
        for s in lab.get("symbols", []):
            assert s in text, (t["id"], s)


# ─── fixtures: documented baseline holds ────────────────────

@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_fixture_baseline_matches_the_manifest(name, tmp_path):
    meta = FIXTURES[name]
    if meta.get("needs") == "node" and not shutil.which("node"):
        pytest.skip("node not installed")
    if meta.get("needs") == "flask" and not HAS_FLASK:
        pytest.skip("flask not installed")
    ws = tmp_path / name
    shutil.copytree(EV / "fixtures" / name, ws, ignore=shutil.ignore_patterns("__pycache__"))
    run = load_run()
    old = os.environ["PATH"]
    os.environ["PATH"] = env_with_this_python()["PATH"]
    try:
        assert run.test_failures(ws, meta) == set(meta["baseline"]["failed"])
    finally:
        os.environ["PATH"] = old


def test_regression_detection_flags_newly_failing_tests_only(tmp_path):
    ws = tmp_path / "shop"
    shutil.copytree(EV / "fixtures" / "buggy_shop", ws, ignore=shutil.ignore_patterns("__pycache__"))
    run = load_run()
    meta = FIXTURES["buggy_shop"]
    old = os.environ["PATH"]
    os.environ["PATH"] = env_with_this_python()["PATH"]
    try:
        before = run.test_failures(ws, meta)
        catalog = ws / "shop" / "catalog.py"
        catalog.write_text(catalog.read_text(encoding="utf-8").replace('"apple": 0.5', '"apple": 0.75'), encoding="utf-8")
        after = run.test_failures(ws, meta)
    finally:
        os.environ["PATH"] = old
    assert before == {"tests/test_cart.py::test_total_with_discount"}      # the seeded bug is baseline, not a regression
    assert after - before == {"tests/test_cart.py::test_subtotal"}


def test_a_broken_suite_counts_as_a_failure(tmp_path):
    ws = tmp_path / "inv"
    shutil.copytree(EV / "fixtures" / "inventory_pkg", ws, ignore=shutil.ignore_patterns("__pycache__"))
    (ws / "inventory" / "models.py").write_text("this is not python\n", encoding="utf-8")
    run = load_run()
    old = os.environ["PATH"]
    os.environ["PATH"] = env_with_this_python()["PATH"]
    try:
        assert run.test_failures(ws, FIXTURES["inventory_pkg"])
    finally:
        os.environ["PATH"] = old


# ─── harness: aggregation and compare ───────────────────────

def test_aggregate_reports_mean_min_max_per_task():
    run = load_run()
    res = [{"id": "a", "passed": True, "retries": 0, "tool_calls": 4, "tokens": 100, "time_s": 1.0, "regressions": []},
           {"id": "a", "passed": False, "retries": 2, "tool_calls": 8, "tokens": 300, "time_s": 3.0, "regressions": ["t1"]},
           {"id": "b", "skipped": "node is not installed", "passed": False, "time_s": 0}]
    agg = run.aggregate(res)
    assert set(agg) == {"a"}
    a = agg["a"]
    assert (a["runs"], a["passed"], a["pass_rate"]) == (2, 1, 0.5)
    assert a["tokens"] == {"mean": 200, "min": 100, "max": 300} and a["regressions"] == {"mean": 0.5, "min": 0, "max": 1}


def test_compare_renders_a_table(tmp_path):
    doc = {"retrieval_mode": "v1", "tasks_file": "tasks_v2.yaml", "coder_model": "m",
           "summary": {"tasks": 1, "runs": 3, "repeat": 3, "pass_rate": 0.667, "tokens_per_run": {"mean": 10, "min": 5, "max": 15},
                       "retries": 1, "tool_calls": 9, "regressions": 0, "time_s": 3.0, "skipped": []},
           "per_task": {"a": {"runs": 3, "passed": 2, "tokens": {"mean": 10, "min": 5, "max": 15}, "regressions": None}}}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    out = subprocess.run([sys.executable, str(EV / "compare.py"), str(p), str(p)], capture_output=True, text=True, check=True).stdout
    assert "| success rate | 67% | 67% |" in out and "| a | 2/3 / 10 [5-15] / - |" in out


# ─── end to end with the oracle (scripted correct agent) ────

def run_oracle(tmp_path, only, sabotage=""):
    env = env_with_this_python()
    env["ORACLE_SABOTAGE"] = sabotage
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(EV / "run.py"), "--oracle", "--no-venv", "--tasks", str(EV / "tasks_v2.yaml"),
                    "--only", ",".join(only), "--out", str(out), "--timeout", "120"], cwd=ROOT, env=env, capture_output=True, timeout=900)
    files = list(out.glob("*_oracle.json"))
    assert files, "no result file"
    return json.loads(files[0].read_text(encoding="utf-8"))


@pytest.mark.slow
def test_oracle_solves_the_offline_v2_tasks(tmp_path):
    ids = [t["id"] for t in TASKS if t["fixture"] in ("inventory_pkg", "buggy_shop")]
    ids += [t["id"] for t in TASKS if t["fixture"] == "flask_notes"] if HAS_FLASK else []
    ids += [t["id"] for t in TASKS if t["fixture"] == "js_todo"] if shutil.which("node") else []
    doc = run_oracle(tmp_path, ids)
    bad = {r["id"]: [c for c in r.get("checks", []) if not c["ok"]] or r.get("error") for r in doc["results"] if not r["passed"]}
    assert not bad, bad
    assert doc["summary"]["regressions"] == 0 and doc["tasks_file"] == "tasks_v2.yaml"
    shop = next(r for r in doc["results"] if r["id"] == "bs_grand_total")
    assert shop["baseline_failed"] == ["tests/test_cart.py::test_total_with_discount"] and shop["regressions"] == []


@pytest.mark.slow
def test_an_unsolved_v2_task_fails(tmp_path):
    doc = run_oracle(tmp_path, ["bs_fix_bug", "inv_validation"], sabotage="bs_fix_bug")
    res = {r["id"]: r for r in doc["results"]}
    assert res["bs_fix_bug"]["passed"] is False and res["inv_validation"]["passed"] is True
