"""Eval runner: runs each task end to end on a fresh temporary git repo and checks the result with checks WE wrote
(not the agent's own acceptance checks).

    python evals/run.py --fake                      # CI smoke: scripted model, only `smoke: true` tasks, no network
    python evals/run.py --oracle                    # scripted PERFECT agent on all tasks: tests the pipeline, no LLM, no cost
    python evals/run.py --only calc_cli,todo_cli    # real model (needs GOOGLE_API_KEY + CODER/PLANNER_MODEL_NAME)
    python evals/run.py                             # all V1 tasks with the real model, 3 repeats each
    python evals/run.py --tasks evals/tasks_v2.yaml --retrieval-mode v2   # V2 tasks on existing-project fixtures

V2 tasks start from a fixture project (evals/fixtures/<name>). Before and after the run the fixture's own test suite is run,
and tests that passed before but fail after are recorded as regressions. Results: evals/results/<date>.json; compare runs
with evals/compare.py.

Plan and approval prompts are answered automatically (approve), and the number of approvals is recorded. Agent commands
(`python`, `pip`) run inside a throwaway virtual environment so evals never touch your global Python.
"""
import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "evals" / "fixtures"
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import config  # noqa: E402
from core import metrics, verification  # noqa: E402
from core.orchestrator import build_graph  # noqa: E402
from core.runner import start_run  # noqa: E402
from core.schemas import ProjectContext  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

SCRIPT_CHECKS = {"python", "node"}   # run by this harness from outside the workspace; the agent never sees them
NODE_PRELUDE = """import assert from 'node:assert/strict';
import { existsSync, readFileSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const load = (p) => import(pathToFileURL(join(process.cwd(), p)).href);
const run = (argv, env = {}) => execFileSync(process.execPath, argv, { env: { ...process.env, ...env }, encoding: 'utf8' });
"""
PY_PRELUDE = "import os, sys\nsys.path.insert(0, os.getcwd())\n"


def exe(name: str) -> str:
    """Resolve through PATH like the agent's commands do (on Windows a bare name would find the harness's own Python first)."""
    return shutil.which(name) or name


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@localhost", *args], cwd=cwd, check=True, capture_output=True)


def make_workspace(task: dict, ws: Path) -> Path:
    if task.get("fixture"):
        shutil.copytree(FIXTURES / task["fixture"], ws, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "node_modules"))
    else:
        ws.mkdir(parents=True)
    for rel, body in (task.get("seed") or {}).items():
        f = ws / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    (ws / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\nnode_modules/\n", encoding="utf-8")
    git(ws, "init", "-q")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "seed")
    return ws


# ─── checks that are ours ───────────────────────────────────

def run_script_check(check: dict, ws: Path, scratch: Path) -> dict:
    """A multi-line python/node check, written to a file OUTSIDE the workspace and run with the workspace as cwd."""
    scratch.mkdir(parents=True, exist_ok=True)
    if check["type"] == "python":
        f = scratch / f"check_{time.time_ns()}.py"
        f.write_text(PY_PRELUDE + check["code"], encoding="utf-8")
        argv = [exe("python"), str(f)]
    else:
        f = scratch / f"check_{time.time_ns()}.mjs"
        f.write_text(NODE_PRELUDE + check["code"], encoding="utf-8")
        argv = [exe("node"), str(f)]
    try:
        r = subprocess.run(argv, cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=check.get("timeout_s", 120))
        out = (r.stdout + r.stderr).strip()
        return {"type": check["type"], "ok": r.returncode == 0, "detail": out[-300:] if r.returncode else "ok"}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"type": check["type"], "ok": False, "detail": f"{type(e).__name__}: {e}"[:300]}


def run_checks(task: dict, ws: Path, scratch: Path) -> list[dict]:
    std = [c for c in task["checks"] if c["type"] not in SCRIPT_CHECKS]
    out = []
    if std:
        check_ctx = ProjectContext(run_id="check", root=ws)
        try:
            res = verification.verify({"acceptance": std}, check_ctx, [], [])
        finally:
            check_ctx.processes.stop_all()
        out += [{"type": c.type, "ok": c.ok, "detail": c.detail[:300]} for c in res.checks]
    out += [run_script_check(c, ws, scratch) for c in task["checks"] if c["type"] in SCRIPT_CHECKS]
    return out


# ─── regressions: the fixture's own test suite before vs after ──

def test_failures(ws: Path, fixture: dict) -> set[str]:
    """Failing test ids of the fixture's suite. A suite that cannot run at all counts as one failure '<suite error>'."""
    if fixture["runner"] == "pytest":
        argv = [exe("python"), "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider"]
        pattern = re.compile(r"^(?:FAILED|ERROR) (\S+)")
    else:
        argv = [exe("node"), "--test"]
        pattern = re.compile(r"^\s*not ok \d+ - (.+?)\s*$")
    try:
        r = subprocess.run(argv, cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return {"<suite error>"}
    failed = {m.group(1) for line in r.stdout.splitlines() if (m := pattern.match(line))}
    if r.returncode != 0 and not failed:
        failed.add("<suite error>")
    return failed


def load_fixtures() -> dict:
    p = FIXTURES / "fixtures.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


def missing_need(task: dict) -> str | None:
    need = task.get("needs")
    if need == "node" and not shutil.which("node"):
        return "node is not installed"
    if need == "flask" and subprocess.run([exe("python"), "-c", "import flask"], capture_output=True).returncode != 0:
        return "flask is not installed in the agent's Python"
    return None


# ─── one run ────────────────────────────────────────────────

def drive(graph, state: dict, cfg: dict, deadline: float) -> tuple[dict, int]:
    """Run the graph answering every question with 'approve'. Returns (final state values, approvals given)."""
    approvals, inp = 0, state
    while True:
        out = graph.invoke(inp, cfg)
        interrupts = out.get("__interrupt__") if isinstance(out, dict) else None
        if not interrupts:
            break
        payload = interrupts[0].value
        if time.time() > deadline:
            inp = Command(resume={"decision": "reject", "note": "eval time limit"} if not isinstance(payload, str) else "yes")
            continue
        if isinstance(payload, str):
            inp = Command(resume="yes")
        else:
            approvals += 1
            inp = Command(resume={"decision": "approve"})
    return graph.get_state(cfg).values, approvals


def run_task_limited(task: dict, repeat: int, work: Path, args) -> dict:
    """Hard per-task limit: a stuck task must not stall the whole eval (the worker thread is abandoned, not killed)."""
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(r=run_task(task, repeat, work, args)), daemon=True)
    t.start()
    t.join(args.timeout + 120)
    if t.is_alive():
        return {"id": task["id"], "title": task["title"], "repeat": repeat, "passed": False,
                "error": f"hard timeout after {args.timeout + 120}s", "time_s": args.timeout + 120}
    return box["r"]


def run_task(task: dict, repeat: int, work: Path, args) -> dict:
    t0 = time.time()
    rec = {"id": task["id"], "title": task["title"], "repeat": repeat, "passed": False}
    ws = make_workspace(task, work / "ws" / f"{task['id']}-{repeat}")
    fixture = args.fixtures.get(task.get("fixture")) if task.get("fixture") else None
    before = test_failures(ws, fixture) if fixture else set()
    sm = StateManager(work / "data" / "nexura.db")
    try:
        ctx, state = start_run(sm, f"eval-{task['id']}-{repeat}", ws, task["prompt"], approval_level=args.approval)
        graph = build_graph(checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": ctx.run_id, "project": ctx}, "recursion_limit": 1000}
        values, approvals = drive(graph, state, cfg, t0 + args.timeout)
        ctx.processes.stop_all()
        m = metrics.compute(sm, ctx.run_id) or {}
        events = sm.list_events(ctx.run_id)
        rec.update(
            run_status=values.get("run_status"), tasks_total=m.get("tasks_total"), tasks_done=m.get("tasks_done"),
            retries=sum((m.get("retries_per_task") or {}).values()), error_types=m.get("error_types"),
            tool_calls=sum(1 for e in events if e["type"] == "tool_call"),
            llm_calls=sum(1 for e in events if e["type"] == "llm_call"), approvals=approvals, tokens=m.get("tokens"),
            task_errors=values.get("task_errors"), run_error=values.get("run_error"))
        rec["checks"] = run_checks(task, ws, work / "checks")
        rec["passed"] = all(c["ok"] for c in rec["checks"])
        if fixture:
            rec["regressions"] = sorted(test_failures(ws, fixture) - before)
            rec["baseline_failed"] = sorted(before)
    except Exception as e:   # an eval must never take the whole run down
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    rec["time_s"] = round(time.time() - t0, 1)
    return rec


# ─── aggregation ────────────────────────────────────────────

def spread(values: list) -> dict | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    return {"mean": round(statistics.mean(vals), 2), "min": min(vals), "max": max(vals)}


def aggregate(results: list[dict]) -> dict:
    by_task: dict[str, list[dict]] = {}
    for r in results:
        if not r.get("skipped"):
            by_task.setdefault(r["id"], []).append(r)
    per_task = {}
    for tid, runs in by_task.items():
        per_task[tid] = {"runs": len(runs), "passed": sum(r["passed"] for r in runs),
                         "pass_rate": round(sum(r["passed"] for r in runs) / len(runs), 3),
                         **{k: spread([r.get(k) for r in runs]) for k in ("retries", "tool_calls", "tokens", "time_s")},
                         "regressions": spread([len(r["regressions"]) for r in runs if "regressions" in r])}
    return per_task


def setup_venv(path: Path) -> None:
    """Throwaway venv first on PATH, so the agent's `python`/`pip` never touch the real environment."""
    if not path.exists():
        subprocess.run([sys.executable, "-m", "venv", str(path)], check=True)
    bindir = path / ("Scripts" if os.name == "nt" else "bin")
    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ["PATH"]
    os.environ["VIRTUAL_ENV"] = str(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", default=str(ROOT / "evals" / "tasks.yaml"))
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--fake", action="store_true", help="scripted model, smoke tasks only (no network, no cost)")
    ap.add_argument("--oracle", action="store_true", help="scripted correct solutions for every task (no LLM): checks the pipeline itself")
    ap.add_argument("--no-venv", action="store_true", help="oracle only: run agent commands in the current environment")
    ap.add_argument("--retrieval-mode", choices=["v1", "v2"], default=config.RETRIEVAL_MODE, help="context builder to use (V2)")
    ap.add_argument("--repeat", type=int, default=None, help="runs per task (default 3 with a real model, 1 for --fake/--oracle)")
    ap.add_argument("--approval", default="auto_within_limits", choices=["ask_every_edit", "ask_risky", "auto_within_limits"])
    ap.add_argument("--timeout", type=int, default=900, help="soft time limit per task, seconds")
    ap.add_argument("--venv", default=str(ROOT / ".evalvenv"), help="throwaway venv for agent commands")
    ap.add_argument("--out", default=str(ROOT / "evals" / "results"))
    args = ap.parse_args()
    scripted = args.fake or args.oracle
    args.repeat = args.repeat or (1 if scripted else 3)

    tasks = yaml.safe_load(Path(args.tasks).read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    elif args.fake:
        tasks = [t for t in tasks if t.get("smoke")]
    else:
        tasks = [t for t in tasks if not t.get("smoke")]
    if not tasks:
        print("no tasks selected")
        return 2

    config.RETRIEVAL_MODE = args.retrieval_mode
    os.environ["NEXURA_RETRIEVAL_MODE"] = args.retrieval_mode
    if args.fake:
        from core import fake_llm
        fake_llm.install()
    elif args.oracle:
        import oracle
        oracle.install()
        if not args.no_venv:
            setup_venv(Path(args.venv))
    else:
        config.configure_observability()
        for var in ("CODER_MODEL_NAME", "PLANNER_MODEL_NAME", "GOOGLE_API_KEY"):
            if not os.getenv(var) and not (var == "GOOGLE_API_KEY" and os.getenv("GROQ_API_KEY")):
                print(f"{var} is not set (see .env.example).")
                return 2
        setup_venv(Path(args.venv))
    args.fixtures = load_fixtures()

    work = Path(tempfile.mkdtemp(prefix="nexura-evals-"))
    config.DATA_DIR = work / "data"
    results = []
    for t in tasks:
        reason = missing_need(t)
        if reason:
            print(f"\n=== {t['id']}: SKIPPED ({reason}) ===")
            results.append({"id": t["id"], "title": t["title"], "skipped": reason, "passed": False, "time_s": 0})
            continue
        for k in range(1, args.repeat + 1):
            print(f"\n=== {t['id']}: {t['title']} (run {k}/{args.repeat}) ===")
            r = run_task_limited(t, k, work, args)
            reg = f"  regressions={r['regressions']}" if r.get("regressions") else ""
            print(f"    -> {'PASS' if r['passed'] else 'FAIL'}  status={r.get('run_status')}  {r['time_s']}s{reg}  {r.get('error', '')}")
            results.append(r)
    shutil.rmtree(work, ignore_errors=True)

    ran = [r for r in results if not r.get("skipped")]
    passed = sum(1 for r in ran if r["passed"])
    summary = {"tasks": len({r["id"] for r in ran}), "runs": len(ran), "repeat": args.repeat, "passed": passed,
               "pass_rate": round(passed / len(ran), 3) if ran else 0.0,
               "skipped": sorted({r["id"] for r in results if r.get("skipped")}),
               "retries": sum(r.get("retries") or 0 for r in ran), "tool_calls": sum(r.get("tool_calls") or 0 for r in ran),
               "approvals": sum(r.get("approvals") or 0 for r in ran), "time_s": round(sum(r["time_s"] for r in ran), 1),
               "tokens": sum(r.get("tokens") or 0 for r in ran) or None,
               "tokens_per_run": spread([r.get("tokens") for r in ran]),
               "regressions": sum(len(r.get("regressions") or []) for r in ran)}
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    doc = {"date": datetime.now().isoformat(timespec="seconds"), "commit": commit, "fake_llm": scripted, "oracle": args.oracle,
           "tasks_file": Path(args.tasks).name, "retrieval_mode": args.retrieval_mode, "temperature": config.TEMPERATURE,
           "coder_model": "oracle" if args.oracle else "fake" if args.fake else os.getenv("CODER_MODEL_NAME"),
           "planner_model": "oracle" if args.oracle else "fake" if args.fake else os.getenv("PLANNER_MODEL_NAME"),
           "approval_level": args.approval, "prompt_versions": config.PROMPT_VERSIONS, "summary": summary,
           "per_task": aggregate(results), "results": results}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{datetime.now():%Y-%m-%d_%H%M%S}{'_oracle' if args.oracle else '_fake' if args.fake else ''}.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(ran)} runs passed ({summary['tasks']} tasks x {args.repeat}). Results: {path}")
    return 0 if ran and passed == len(ran) else 1


if __name__ == "__main__":
    sys.exit(main())
