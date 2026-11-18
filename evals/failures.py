"""Failure categories of eval result files, as Markdown (used for docs/V2_EVAL.md).

    python evals/failures.py evals/results/<v1>.json evals/results/<v2>.json
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path


def category(run: dict) -> str:
    """One label per failed run: the first thing that went wrong."""
    if run.get("passed") and not run.get("regressions"):
        return "ok"
    if run.get("error"):
        return "harness error"
    for code in (run.get("task_errors") or {}).values():
        m = re.match(r"([A-Z_]+)", str(code))
        if m:
            return m.group(1).lower()
    bad = [c for c in run.get("checks", []) if not c["ok"]]
    if bad:
        return f"final check failed: {bad[0]['type']}"
    if run.get("regressions"):
        return "regression introduced"
    return "other"


def table(docs: list[dict]) -> str:
    labels = [f"retrieval {d.get('retrieval_mode')}, patch {d.get('patch_mode', 'v1')}" for d in docs]
    counters = [Counter(category(r) for r in d["results"] if not r.get("skipped")) for d in docs]
    cats = sorted({c for ctr in counters for c in ctr}, key=lambda c: (c != "ok", c))
    lines = ["| category | " + " | ".join(labels) + " |", "|---|" + "---|" * len(docs)]
    lines += [f"| {c} | " + " | ".join(str(ctr.get(c, 0)) for ctr in counters) + " |" for c in cats]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    print(table([json.loads(Path(p).read_text(encoding="utf-8")) for p in argv[1:]]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
