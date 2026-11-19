"""Retrieval quality against hand labels (evals/retrieval_labels.yaml): recall@k and MRR at FILE level, for vector-only,
keyword-only and hybrid retrieval. Tasks are split into a tuning half and a held-out half (alternating by id) so any tuning
of RRF_K can be checked on tasks it was not tuned on.

    python evals/retrieval_eval.py                          # real local model (must be installed or consented)
    python evals/retrieval_eval.py --fake                   # FakeEmbedder (no model; used by tests)
    python evals/retrieval_eval.py --rrf-k 10,30,60,100     # also compare RRF_K values
"""
import argparse
import json
import shutil
import statistics
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import config  # noqa: E402

MODES = ("vector", "lexical", "hybrid")


def recall_at_k(labelled: list[str], retrieved_files: list[str]) -> float:
    return len(set(labelled) & set(retrieved_files)) / len(labelled) if labelled else 0.0


def reciprocal_rank(labelled: list[str], retrieved_files: list[str]) -> float:
    for i, f in enumerate(retrieved_files, 1):
        if f in labelled:
            return 1.0 / i
    return 0.0


def symbol_recall(symbols: list[str], texts: list[str]) -> float | None:
    """Share of labelled symbols that appear in the retrieved chunks (None when a task has no labelled symbols)."""
    if not symbols:
        return None
    return sum(any(s in t for t in texts) for s in symbols) / len(symbols)


def split(task_ids: list[str]) -> tuple[list[str], list[str]]:
    ids = sorted(task_ids)
    return ids[0::2], ids[1::2]


def evaluate(embedder_factory, k: int, rrf_values: list[int], work: Path) -> dict:
    from rag.indexer import Indexer
    tasks = yaml.safe_load((ROOT / "evals" / "tasks_v2.yaml").read_text(encoding="utf-8"))
    labels = yaml.safe_load((ROOT / "evals" / "retrieval_labels.yaml").read_text(encoding="utf-8"))
    tune, held = split([t["id"] for t in tasks])
    config.INDEX_DIR = work / "index"
    indexers = {}
    for fx in sorted({t["fixture"] for t in tasks}):
        root = work / fx
        shutil.copytree(ROOT / "evals" / "fixtures" / fx, root, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
        ix = Indexer(root, fx, embedder_factory())
        ix.open()
        ix.build()
        indexers[fx] = ix
    original_rrf = config.RRF_K
    per_task: dict = {}
    summary: dict = {}
    try:
        for rrf in rrf_values:
            config.RRF_K = rrf
            for mode in MODES:
                if mode != "hybrid" and rrf != rrf_values[0]:
                    continue        # RRF_K only matters for fusion
                key = mode if mode != "hybrid" else f"hybrid(rrf_k={rrf})"
                for t in tasks:
                    lab = labels[t["id"]]
                    res = indexers[t["fixture"]].retriever().retrieve(lab["query"], k=k, mode=mode)
                    files = list(dict.fromkeys(r.chunk.path for r in res))
                    sym = symbol_recall(lab.get("symbols", []), [r.chunk.text for r in res])
                    per_task.setdefault(t["id"], {})[key] = {"recall": round(recall_at_k(lab["files"], files), 3),
                                                             "rr": round(reciprocal_rank(lab["files"], files), 3),
                                                             "symbol_recall": None if sym is None else round(sym, 3), "files": files[:5]}
                for part, ids in (("tune", tune), ("held_out", held), ("all", tune + held)):
                    summary.setdefault(key, {})[part] = {
                        "recall@k": round(statistics.mean(per_task[i][key]["recall"] for i in ids), 3),
                        "mrr": round(statistics.mean(per_task[i][key]["rr"] for i in ids), 3),
                        "symbol_recall": round(statistics.mean([per_task[i][key]["symbol_recall"] for i in ids
                                                                if per_task[i][key]["symbol_recall"] is not None] or [0]), 3),
                        "tasks": len(ids)}
    finally:
        config.RRF_K = original_rrf
        for ix in indexers.values():
            ix.close()
    return {"k": k, "tune_tasks": tune, "held_out_tasks": held, "summary": summary, "per_task": per_task}


def table(result: dict) -> str:
    lines = [f"| retrieval (k={result['k']}) | recall@k tune | recall@k held-out | recall@k all | MRR tune | MRR held-out | MRR all "
             "| symbol recall all |", "|---|---|---|---|---|---|---|---|"]
    for key, s in result["summary"].items():
        lines.append(f"| {key} | {s['tune']['recall@k']} | {s['held_out']['recall@k']} | {s['all']['recall@k']} | "
                     f"{s['tune']['mrr']} | {s['held_out']['mrr']} | {s['all']['mrr']} | {s['all']['symbol_recall']} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="FakeEmbedder instead of the local model")
    ap.add_argument("--k", type=int, default=config.TOP_K)
    ap.add_argument("--rrf-k", default=str(config.RRF_K), help="comma-separated RRF_K values to compare")
    ap.add_argument("--model-dir", default=None, help="where the local model is installed (default: <data dir>/models)")
    ap.add_argument("--out", default=str(ROOT / "evals" / "results"))
    args = ap.parse_args()
    from rag.embedder import FakeEmbedder, LocalEmbedder
    model_dir = args.model_dir
    factory = (lambda: FakeEmbedder(dim=64)) if args.fake else (lambda: LocalEmbedder(config.EMBEDDING_MODEL, cache_dir=model_dir))
    work = Path(tempfile.mkdtemp(prefix="nexura-retrieval-"))
    try:
        result = evaluate(factory, args.k, [int(x) for x in args.rrf_k.split(",")], work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result.update(date=datetime.now().isoformat(timespec="seconds"), embedding_model="fake" if args.fake else config.EMBEDDING_MODEL)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"retrieval_{datetime.now():%Y-%m-%d_%H%M%S}{'_fake' if args.fake else ''}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(table(result))
    print(f"\nResults: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
