"""Run the V2 task set for two settings (arms) INTERLEAVED, task by task and repeat by repeat, so a quota or time limit that stops
the run midway still leaves paired results. Each arm's results are merged into one file that compare.py can read.

    python evals/paired.py --repeat 3                       # arm A = v1/v1, arm B = v2/v1 (retrieval v2, patching v1)
    python evals/paired.py --only fn_delete,bs_fix_bug --repeat 1 --model-dir <local model folder>

Arms are `<retrieval>/<patch>` (default `v1/v1,v2/v1`). Same model and temperature for all arms (set CODER_MODEL_NAME /
PLANNER_MODEL_NAME in the environment to use another model than .env). Stops early when the model API reports an exhausted quota.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

import yaml  # noqa: E402

import run as evrun  # noqa: E402


def one(task_id: str, arm: str, args, tmp: Path) -> dict | None:
    retrieval, patch = arm.split("/")
    cmd = [sys.executable, str(ROOT / "evals" / "run.py"), "--tasks", args.tasks, "--only", task_id, "--repeat", "1",
           "--retrieval-mode", retrieval, "--patch-mode", patch, "--timeout", str(args.timeout), "--out", str(tmp)]
    if args.model_dir:
        cmd += ["--model-dir", args.model_dir]
    before = set(tmp.glob("*.json"))
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    new = sorted(set(tmp.glob("*.json")) - before)
    if not new:
        print(f"    no result file ({out.stdout[-300:]} {out.stderr[-300:]})")
        return None
    return json.loads(new[-1].read_text(encoding="utf-8"))


def merge(docs: list[dict], arm: str, args) -> dict:
    results = [r for d in docs for r in d["results"]]
    for i, r in enumerate(results):    # every task ran with repeat=1 in its own process: number the repeats per task
        r["repeat"] = sum(1 for p in results[:i] if p["id"] == r["id"]) + 1
    ran = [r for r in results if not r.get("skipped")]
    passed = sum(1 for r in ran if r["passed"])
    first = docs[0]
    summary = {"tasks": len({r["id"] for r in ran}), "runs": len(ran), "repeat": max((r["repeat"] for r in ran), default=0), "passed": passed,
               "pass_rate": round(passed / len(ran), 3) if ran else 0.0, "skipped": sorted({r["id"] for r in results if r.get("skipped")}),
               "retries": sum(r.get("retries") or 0 for r in ran), "tool_calls": sum(r.get("tool_calls") or 0 for r in ran),
               "approvals": sum(r.get("approvals") or 0 for r in ran), "time_s": round(sum(r["time_s"] for r in ran), 1),
               "tokens": sum(r.get("tokens") or 0 for r in ran) or None, "tokens_per_run": evrun.spread([r.get("tokens") for r in ran]),
               "regressions": sum(len(r.get("regressions") or []) for r in ran),
               "retrieval_fallbacks": sum(r.get("retrieval_fallbacks") or 0 for r in ran),
               "index_s": evrun.spread([r.get("index_s") for r in ran]),
               "run_errors": sum(1 for r in ran if r.get("error") or "LLM_QUOTA" in json.dumps(r.get("task_errors") or {}))}
    doc = {k: first[k] for k in ("commit", "fake_llm", "oracle", "tasks_file", "temperature", "coder_model", "planner_model",
                                 "approval_level", "prompt_versions") if k in first}
    retrieval, patch = arm.split("/")
    doc.update(date=datetime.now().isoformat(timespec="seconds"), retrieval_mode=retrieval, patch_mode=patch, summary=summary,
               per_task=evrun.aggregate(results), results=results)
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", default=str(ROOT / "evals" / "tasks_v2.yaml"))
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--arms", default="v1/v1,v2/v1")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--out", default=str(ROOT / "evals" / "results"))
    args = ap.parse_args()
    arms = args.arms.split(",")
    tasks = [t["id"] for t in yaml.safe_load(Path(args.tasks).read_text(encoding="utf-8")) if not t.get("smoke")]
    if args.only:
        tasks = [t for t in tasks if t in set(args.only.split(","))]
    tmp = Path(tempfile.mkdtemp(prefix="nexura-paired-"))
    docs: dict[str, list[dict]] = {a: [] for a in arms}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y-%m-%d_%H%M%S}"

    def save() -> None:       # after every run, so an interrupted eval still leaves its results
        for a in arms:
            if docs[a]:
                (out / f"{stamp}_{a.replace('/', '-')}.json").write_text(json.dumps(merge(docs[a], a, args), indent=2), encoding="utf-8")
    stop = False
    for k in range(1, args.repeat + 1):
        for t in tasks:
            for a in arms:
                print(f"== {t} [{a}] repeat {k}/{args.repeat}", flush=True)
                d = one(t, a, args, tmp)
                if d is None:
                    continue
                docs[a].append(d)
                save()
                r = d["results"][0]
                print(f"   {'PASS' if r['passed'] else 'FAIL'} {r.get('run_status')} {r['time_s']}s tokens={r.get('tokens')} reg={r.get('regressions')}", flush=True)
                if "LLM_QUOTA" in json.dumps(r.get("task_errors") or {}) + str(r.get("run_error")):
                    print("   the model API quota is exhausted: stopping (results so far are saved)", flush=True)
                    stop = True
                    break
            if stop:
                break
        if stop:
            break
    save()
    for a in arms:
        if docs[a]:
            print("wrote", out / f"{stamp}_{a.replace('/', '-')}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
