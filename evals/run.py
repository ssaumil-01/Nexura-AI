"""Eval runner: runs each task from tasks.yaml end to end on a fresh temporary git repo and checks the result with
checks WE wrote (not the agent's own acceptance checks).

    python evals/run.py --fake                      # CI smoke: scripted model, only `smoke: true` tasks, no network
    python evals/run.py --oracle                    # scripted PERFECT agent on all tasks: tests the pipeline, no LLM, no cost
    python evals/run.py --only calc_cli,todo_cli    # real model (needs GOOGLE_API_KEY + CODER/PLANNER_MODEL_NAME)
    python evals/run.py                             # all real-model tasks

Plan and approval prompts are answered automatically (approve), and the number of approvals is recorded. Agent commands
(`python`, `pip`) run inside a throwaway virtual environment so evals never touch your global Python.
Results: evals/results/<date>.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@localhost", *args], cwd=cwd, check=True, capture_output=True)


def make_workspace(task: dict, base: Path) -> Path:
    ws = base / task["id"]
    ws.mkdir(parents=True)
    for rel, body in (task.get("seed") or {}).items():
        f = ws / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    (ws / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    git(ws, "init", "-q")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "seed")
    return ws


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


def run_task_limited(task: dict, base: Path, data: Path, args) -> dict:
    """Hard per-task limit: a stuck task must not stall the whole eval (the worker thread is abandoned, not killed)."""
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(r=run_task(task, base, data, args)), daemon=True)
    t.start()
    t.join(args.timeout + 120)
    if t.is_alive():
        return {"id": task["id"], "title": task["title"], "passed": False, "error": f"hard timeout after {args.timeout + 120}s",
                "time_s": args.timeout + 120}
    return box["r"]


def run_task(task: dict, base: Path, data: Path, args) -> dict:
    t0 = time.time()
    rec = {"id": task["id"], "title": task["title"], "passed": False}
    ws = make_workspace(task, base)
    sm = StateManager(data / "nexura.db")
    try:
        ctx, state = start_run(sm, "eval-" + task["id"], ws, task["prompt"], approval_level=args.approval)
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
        # our own checks, independent of the agent's acceptance criteria
        check_ctx = ProjectContext(run_id="check", root=ws)
        res = verification.verify({"acceptance": task["checks"]}, check_ctx, [], [])
        check_ctx.processes.stop_all()
        rec["checks"] = [{"type": c.type, "ok": c.ok, "detail": c.detail[:300]} for c in res.checks]
        rec["passed"] = res.ok
    except Exception as e:   # an eval must never take the whole run down
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    rec["time_s"] = round(time.time() - t0, 1)
    return rec


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
    ap.add_argument("--approval", default="auto_within_limits", choices=["ask_every_edit", "ask_risky", "auto_within_limits"])
    ap.add_argument("--timeout", type=int, default=900, help="soft time limit per task, seconds")
    ap.add_argument("--venv", default=str(ROOT / ".evalvenv"), help="throwaway venv for agent commands")
    ap.add_argument("--out", default=str(ROOT / "evals" / "results"))
    args = ap.parse_args()

    tasks = yaml.safe_load(Path(args.tasks).read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    elif args.fake:
        tasks = [t for t in tasks if t.get("smoke")]
    elif args.oracle:
        tasks = [t for t in tasks if not t.get("smoke")]
    else:
        tasks = [t for t in tasks if not t.get("smoke")]
    if not tasks:
        print("no tasks selected")
        return 2

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

    work = Path(tempfile.mkdtemp(prefix="nexura-evals-"))
    config.DATA_DIR = work / "data"
    results = []
    for t in tasks:
        print(f"\n=== {t['id']}: {t['title']} ===")
        r = run_task_limited(t, work / "ws", work / "data", args)
        print(f"    -> {'PASS' if r['passed'] else 'FAIL'}  status={r.get('run_status')}  {r['time_s']}s  {r.get('error', '')}")
        results.append(r)
    shutil.rmtree(work, ignore_errors=True)

    passed = sum(1 for r in results if r["passed"])
    summary = {"tasks": len(results), "passed": passed, "pass_rate": round(passed / len(results), 3),
               "retries": sum(r.get("retries") or 0 for r in results), "tool_calls": sum(r.get("tool_calls") or 0 for r in results),
               "approvals": sum(r.get("approvals") or 0 for r in results), "time_s": round(sum(r["time_s"] for r in results), 1),
               "tokens": sum(r.get("tokens") or 0 for r in results) or None}
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    doc = {"date": datetime.now().isoformat(timespec="seconds"), "commit": commit, "fake_llm": args.fake or args.oracle, "oracle": args.oracle,
           "coder_model": "oracle" if args.oracle else "fake" if args.fake else os.getenv("CODER_MODEL_NAME"),
           "planner_model": "oracle" if args.oracle else "fake" if args.fake else os.getenv("PLANNER_MODEL_NAME"),
           "approval_level": args.approval, "prompt_versions": config.PROMPT_VERSIONS, "summary": summary, "results": results}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{datetime.now():%Y-%m-%d_%H%M%S}{'_oracle' if args.oracle else '_fake' if args.fake else ''}.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(results)} passed. Results: {path}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
