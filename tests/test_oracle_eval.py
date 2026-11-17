"""No-LLM end-to-end check of V1: a scripted perfect agent solves the eval tasks through the REAL pipeline (planner parsing,
validator, execution manager, verification, git checkpoints, approvals, state). Independent eval checks grade the result.
A deliberately wrong solution must be reported as FAIL, so a green run here means something."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Tasks that need only the Python standard library plus pytest (web frameworks are covered by `python evals/run.py --oracle`).
OFFLINE = ["calc_cli", "static_site", "algo_tests", "json_config", "package_bugfix", "add_function", "todo_cli", "csv_report",
           "validators", "tests_for_existing"]


def run_oracle(tmp_path, only, sabotage=""):
    env = dict(os.environ, ORACLE_SABOTAGE=sabotage)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]   # the agent's `python` must be this one
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(ROOT / "evals" / "run.py"), "--oracle", "--no-venv", "--only", ",".join(only),
                    "--out", str(out), "--timeout", "120"], cwd=ROOT, env=env, capture_output=True, timeout=600)
    files = list(out.glob("*_oracle.json"))
    assert files, "the eval runner produced no result file"
    return {r["id"]: r for r in json.loads(files[0].read_text(encoding="utf-8"))["results"]}


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_oracle_solves_every_offline_task(tmp_path):
    ids = OFFLINE + (["node_script"] if shutil.which("node") else [])
    results = run_oracle(tmp_path, ids)
    failed = {i: (r.get("run_status"), [c for c in r.get("checks", []) if not c["ok"]], r.get("error")) for i, r in results.items() if not r["passed"]}
    assert not failed, failed
    assert all(r["run_status"] == "completed" for r in results.values())


def test_a_wrong_solution_is_reported_as_failure_not_success(tmp_path):
    results = run_oracle(tmp_path, ["calc_cli", "csv_report"], sabotage="calc_cli")
    assert results["calc_cli"]["passed"] is False                      # our independent checks catch it
    assert results["calc_cli"]["run_status"] != "completed"            # and the pipeline itself does not claim success
    assert results["csv_report"]["passed"] is True                     # the other task is unaffected
