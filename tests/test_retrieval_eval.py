"""V2-3.8: retrieval metrics (recall@k, MRR, symbol recall) and the tuning/held-out split."""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("chromadb")
EV = Path(__file__).resolve().parent.parent / "evals"
spec = importlib.util.spec_from_file_location("retrieval_eval", EV / "retrieval_eval.py")
reval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reval)


def test_metrics():
    assert reval.recall_at_k(["a", "b"], ["b", "c"]) == 0.5
    assert reval.reciprocal_rank(["a", "b"], ["c", "b", "a"]) == 0.5 and reval.reciprocal_rank(["a"], ["c"]) == 0.0
    assert reval.symbol_recall(["load"], ["def load():"]) == 1.0 and reval.symbol_recall([], ["x"]) is None


def test_split_is_disjoint_and_complete():
    tune, held = reval.split(["d", "a", "c", "b", "e"])
    assert set(tune) | set(held) == {"a", "b", "c", "d", "e"} and not set(tune) & set(held)


def test_evaluate_with_the_fake_embedder(tmp_path, monkeypatch):
    import config
    from rag.embedder import FakeEmbedder
    monkeypatch.setattr(config, "INDEX_DIR", tmp_path / "unused")
    r = reval.evaluate(lambda: FakeEmbedder(dim=64), 8, [60], tmp_path)
    assert set(r["summary"]) == {"vector", "lexical", "hybrid(rrf_k=60)"}
    for s in r["summary"].values():
        assert 0 <= s["all"]["recall@k"] <= 1 and 0 <= s["all"]["mrr"] <= 1 and s["all"]["tasks"] == 18
    assert r["summary"]["hybrid(rrf_k=60)"]["all"]["recall@k"] >= 0.7          # sanity, even with the toy embedder
    assert "| hybrid(rrf_k=60) |" in reval.table(r)
