"""Render eval result files as Markdown tables.   python evals/report.py evals/results/<a>.json [<b>.json ...]"""
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def table(docs: list[dict]) -> str:
    ids = [r["id"] for r in docs[-1]["results"]]
    head = "| task | " + " | ".join(f"run {i + 1}" for i in range(len(docs))) + " | last run: status | retries | tool calls | approvals | time (s) |\n"
    head += "|---|" + "---|" * len(docs) + "---|---|---|---|---|\n"
    rows = []
    for tid in ids:
        cells, last = [], None
        for d in docs:
            r = next((x for x in d["results"] if x["id"] == tid), None)
            cells.append("n/a" if r is None else "PASS" if r["passed"] else "FAIL")
            last = r or last
        rows.append(f"| {tid} | " + " | ".join(cells) +
                    f" | {last.get('run_status') or last.get('error', '')[:40]} | {last.get('retries', '')} | {last.get('tool_calls', '')} | "
                    f"{last.get('approvals', '')} | {last.get('time_s', '')} |")
    return head + "\n".join(rows)


def summary(d: dict) -> str:
    s = d["summary"]
    return (f"{s['passed']}/{s['tasks']} passed ({s['pass_rate']:.0%}), {s['retries']} retries, {s['tool_calls']} tool calls, "
            f"{s['approvals']} approvals, {s['time_s']} s, tokens {s['tokens']}; model {d['coder_model']} / planner {d['planner_model']}, "
            f"prompts {d['prompt_versions']}, commit {d['commit']}")


if __name__ == "__main__":
    docs = [load(p) for p in sys.argv[1:]]
    for i, d in enumerate(docs, 1):
        print(f"- run {i} ({d['date']}): {summary(d)}")
    print()
    print(table(docs))
