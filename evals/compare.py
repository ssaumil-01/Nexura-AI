"""Compare eval result files (e.g. RETRIEVAL_MODE=v1 vs v2) as Markdown tables.

    python evals/compare.py evals/results/<v1>.json evals/results/<v2>.json
"""
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    d["label"] = f"{d.get('retrieval_mode', '?')} ({Path(path).stem})"
    return d


def mean(stat) -> str:
    return "-" if not stat else f"{stat['mean']:g} [{stat['min']:g}-{stat['max']:g}]"


def table(docs: list[dict]) -> str:
    lines = ["| | " + " | ".join(d["label"] for d in docs) + " |", "|---|" + "---|" * len(docs)]
    rows = [
        ("tasks file", lambda d: d.get("tasks_file", "-")),
        ("model", lambda d: d.get("coder_model") or "-"),
        ("runs (tasks x repeat)", lambda d: f"{d['summary'].get('runs', d['summary']['tasks'])} "
                                            f"({d['summary']['tasks']} x {d['summary'].get('repeat', 1)})"),
        ("success rate", lambda d: f"{d['summary']['pass_rate']:.0%}"),
        ("tokens per run, mean [min-max]", lambda d: mean(d["summary"].get("tokens_per_run"))),
        ("retries (total)", lambda d: str(d["summary"].get("retries", "-"))),
        ("tool calls (total)", lambda d: str(d["summary"].get("tool_calls", "-"))),
        ("regressions introduced", lambda d: str(d["summary"].get("regressions", "-"))),
        ("time (s)", lambda d: str(d["summary"].get("time_s", "-"))),
        ("skipped tasks", lambda d: ", ".join(d["summary"].get("skipped") or []) or "-"),
    ]
    lines += [f"| {name} | " + " | ".join(f(d) for d in docs) + " |" for name, f in rows]
    ids = sorted({t for d in docs for t in d.get("per_task", {})})
    lines += ["", "| task | " + " | ".join(f"{d['label']}: pass / tokens / regressions" for d in docs) + " |",
              "|---|" + "---|" * len(docs)]
    for t in ids:
        cells = []
        for d in docs:
            p = d.get("per_task", {}).get(t)
            cells.append("-" if not p else f"{p['passed']}/{p['runs']} / {mean(p.get('tokens'))} / {mean(p.get('regressions'))}")
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    print(table([load(p) for p in argv[1:]]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
