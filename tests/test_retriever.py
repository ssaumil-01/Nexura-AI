"""V2-3.1: hybrid retrieval (vector + keyword, RRF), path/symbol lookups, boosts, de-duplication, reasons."""
import shutil
from pathlib import Path

import pytest

pytest.importorskip("chromadb")
import config  # noqa: E402
from rag.embedder import FakeEmbedder  # noqa: E402
from rag.indexer import Indexer  # noqa: E402
from rag.retriever import Retriever  # noqa: E402

FIX = Path(__file__).resolve().parent.parent / "evals" / "fixtures"


@pytest.fixture
def ix(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX_DIR", tmp_path / "index")
    root = tmp_path / "inv"
    shutil.copytree(FIX / "inventory_pkg", root, ignore=shutil.ignore_patterns("__pycache__"))
    i = Indexer(root, "inv", FakeEmbedder(dim=64))
    i.open()
    i.build()
    yield i
    i.close()


def files_of(results):
    return list(dict.fromkeys(r.chunk.path for r in results))


def test_hybrid_finds_the_right_code_with_reasons_and_sources(ix):
    res = Retriever(ix).retrieve("rename line_total to item_total and update its callers", k=6)
    assert res[0].chunk.symbol == "line_total" and res[0].chunk.path == "inventory/pricing.py"
    assert "symbol" in res[0].sources and "line_total" in res[0].reason
    assert {"vector", "lexical"} & set(res[0].sources)
    assert all(r.reason for r in res) and res == sorted(res, key=lambda r: -r.score)


def test_explicit_path_lookup_needs_no_embedding(ix):
    r = Retriever(ix)
    r.ix.embedder.embed_query = lambda q: (_ for _ in ()).throw(AssertionError("must not embed"))
    res = r.retrieve("look at inventory/storage.py", k=5, mode="lexical")
    assert res[0].chunk.path == "inventory/storage.py" and "path" in res[0].sources


def test_named_files_and_recent_changes_are_boosted(ix):
    r = Retriever(ix)
    base = r.retrieve("report totals", k=8)
    boosted = r.retrieve("report totals", k=8, named_paths=["main.py"])
    rank = lambda res: files_of(res).index("main.py") if "main.py" in files_of(res) else 99   # noqa: E731
    assert rank(boosted) <= rank(base) and rank(boosted) < 99
    assert next(x for x in boosted if x.chunk.path == "main.py").score > next((x.score for x in base if x.chunk.path == "main.py"), 0)
    assert "named in the task" in next(x for x in boosted if x.chunk.path == "main.py").reason
    recent = r.retrieve("report totals", k=8, recent_paths=["inventory/storage.py"])
    assert "changed in this run" in next(x for x in recent if x.chunk.path == "inventory/storage.py").reason


def test_modes_and_filters(ix):
    r = Retriever(ix)
    v = r.retrieve("tax on the order total", k=5, mode="vector")
    lx = r.retrieve("tax on the order total", k=5, mode="lexical")
    assert all("vector" in x.sources for x in v) and all(set(x.sources) <= {"lexical", "path", "symbol"} for x in lx)
    only_tests = r.retrieve("pricing", k=10, filters={"path_prefix": "tests/"})
    assert only_tests and all(x.chunk.path.startswith("tests/") for x in only_tests)


def test_overlapping_chunks_are_deduplicated(ix, tmp_path):
    big = ix.root / "inventory" / "big.py"
    big.write_text("def big_fn():\n" + "\n".join(f"    v{i} = {i}  # widget" for i in range(200)) + "\n", encoding="utf-8")
    ix.update(["inventory/big.py"])
    res = Retriever(ix).retrieve("big_fn widget", k=10)
    parts = [x for x in res if x.chunk.path == "inventory/big.py"]
    for a in parts:
        for b in parts:
            if a is not b:
                assert a.chunk.end_line < b.chunk.start_line or b.chunk.end_line < a.chunk.start_line


def test_empty_query_and_empty_index(ix, tmp_path, monkeypatch):
    assert Retriever(ix).retrieve("", k=5) == []
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    e = Indexer(empty_root, "empty", FakeEmbedder(dim=16))
    e.open()
    e.build()
    assert Retriever(e).retrieve("anything", k=5) == []
    e.close()


def test_k_is_respected(ix):
    assert len(Retriever(ix).retrieve("item price quantity tax report", k=3)) == 3
