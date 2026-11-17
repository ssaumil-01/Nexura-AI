"""V2-2.1, 2.8-2.11, 2.13: discovery, build, incremental updates, manifest versioning, background indexing, and the
security rules (nothing ignored/secret/protected is ever indexed; the workspace is never modified; no network)."""
import hashlib
import json
import shutil
import socket
import threading
from pathlib import Path

import pytest

pytest.importorskip("chromadb")
import config  # noqa: E402
from rag import indexer as ixm  # noqa: E402
from rag.embedder import FakeEmbedder  # noqa: E402
from rag.indexer import Indexer, IndexManager, discover  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "evals" / "fixtures" / "inventory_pkg"
SECRET = "AIza" + "B" * 35
SECRET_MARK = "very-private-value-xyz"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX_DIR", tmp_path / "index")
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__"))
    (root / ".env").write_text(f"API_KEY={SECRET_MARK}\n", encoding="utf-8")
    (root / ".env.local").write_text(f"X={SECRET_MARK}\n", encoding="utf-8")
    (root / "settings_secret.py").write_text(f'GOOGLE = "{SECRET}"\n# {SECRET_MARK}\n', encoding="utf-8")
    (root / "server.pem").write_text(f"{SECRET_MARK}\n", encoding="utf-8")
    (root / "ignored_dir").mkdir()
    (root / "ignored_dir" / "x.py").write_text(f"# {SECRET_MARK}\n", encoding="utf-8")
    (root / "notes.log").write_text(f"{SECRET_MARK}\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored_dir/\n*.log\n", encoding="utf-8")
    (root / ".nexuraignore").write_text("private_notes.md\n", encoding="utf-8")
    (root / "private_notes.md").write_text(f"{SECRET_MARK}\n", encoding="utf-8")
    (root / "node_modules" / "lib").mkdir(parents=True)
    (root / "node_modules" / "lib" / "index.js").write_text(f"// {SECRET_MARK}\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00\x01" + SECRET_MARK.encode())
    (root / "app.min.js").write_text(f"var a=1;// {SECRET_MARK}\n", encoding="utf-8")
    (root / "bundle.js").write_text("var x=" + "1+" * 2000 + "1;\n", encoding="utf-8")
    (root / "package-lock.json").write_text(f'{{"x": "{SECRET_MARK}"}}\n', encoding="utf-8")
    return root


def make(repo, emit=None):
    ix = Indexer(repo, "ws1", FakeEmbedder(dim=32), emit=emit)
    ix.open()
    return ix


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


# ─── discovery ──────────────────────────────────────────────

def test_discovery_keeps_source_and_skips_everything_it_must(repo):
    d = discover(repo)
    assert {"main.py", "inventory/pricing.py", "inventory.json", "data/items.csv", "README.md"} <= set(d.files)
    for bad in (".env", ".env.local", "settings_secret.py", "server.pem", "ignored_dir/x.py", "notes.log", "private_notes.md",
                "node_modules/lib/index.js", "image.bin", "app.min.js", "bundle.js", "package-lock.json"):
        assert bad not in d.files, bad
    assert d.skipped["settings_secret.py"] == "secret" and d.skipped["image.bin"] == "binary"
    assert d.skipped["app.min.js"] == d.skipped["bundle.js"] == "minified" and d.skipped["package-lock.json"] == "protected"


def test_large_files_and_the_file_cap(repo, monkeypatch):
    (repo / "big.py").write_text("x = 1\n" * 200_000, encoding="utf-8")
    assert discover(repo).skipped["big.py"] == "too large"
    d = discover(repo, max_files=3)
    assert len(d.files) == 3 and d.capped


def test_symlink_outside_the_workspace_is_skipped(repo, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text(f"# {SECRET_MARK}\n", encoding="utf-8")
    try:
        (repo / "link.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    assert "link.py" not in discover(repo).files


# ─── build and contents ─────────────────────────────────────

def test_build_indexes_chunks_imports_and_manifest(repo):
    ix = make(repo)
    r = ix.build()
    assert r["files"] >= 10 and r["chunks"] > 10 and ix.ready()
    assert {c.symbol for c in ix.lexical.chunks_of("inventory/pricing.py")} >= {"line_total", "apply_tax", "order_total"}
    assert ix.vectors.count() == ix.lexical.count()
    assert ix.imports.importers_of("inventory/pricing.py") == ["inventory/reports.py", "tests/test_pricing.py"]
    m = json.loads((ix.dir / "manifest.json").read_text())
    assert m["embedding_model"] == "fake" and m["embedding_dim"] == 32 and m["complete"] is True
    assert set(m["files"]["inventory/pricing.py"]) == {"hash", "mtime", "chunk_ids"}


def test_index_lives_outside_the_workspace_and_never_modifies_it(repo):
    before = tree_hash(repo)
    ix = make(repo)
    ix.build()
    assert tree_hash(repo) == before
    assert repo.resolve() not in ix.dir.resolve().parents and Path(config.INDEX_DIR).resolve() in ix.dir.resolve().parents
    ix.delete()
    assert not ix.dir.exists() and tree_hash(repo) == before


def test_no_ignored_or_secret_content_in_any_store(repo):
    ix = make(repo)
    ix.build()
    ix.close()
    blob = b"".join(p.read_bytes() for p in Path(config.INDEX_DIR).rglob("*") if p.is_file())
    for needle in (SECRET_MARK, SECRET, "settings_secret", ".env", "server.pem", "private_notes", "node_modules"):
        assert needle.encode() not in blob, needle


def test_indexing_makes_no_network_calls(repo, monkeypatch):
    def deny(*a, **k):
        raise AssertionError("network access during indexing")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    ix = make(repo)
    assert ix.build()["chunks"] > 0


# ─── incremental updates ────────────────────────────────────

def test_editing_a_file_reindexes_only_that_file(repo):
    ix = make(repo)
    ix.build()
    reports_ids = set(ix.manifest["files"]["inventory/reports.py"]["chunk_ids"])
    calls = []
    real = ix.embedder.inner.embed_documents
    ix.embedder.inner.embed_documents = lambda texts: calls.append(len(texts)) or real(texts)
    p = repo / "inventory" / "pricing.py"
    p.write_text(p.read_text() + "\n\ndef discount(x):\n    return x * 0.9\n", encoding="utf-8")
    r = ix.update(["inventory/pricing.py"])
    assert r["indexed"] == 1 and "discount" in {c.symbol for c in ix.lexical.chunks_of("inventory/pricing.py")}
    assert calls == [1]                                        # unchanged chunks come from the cache
    assert set(ix.manifest["files"]["inventory/reports.py"]["chunk_ids"]) == reports_ids
    assert ix.update(["inventory/pricing.py"])["indexed"] == 0  # unchanged: nothing to do


def test_add_delete_rename_via_scan_and_build(repo):
    ix = make(repo)
    ix.build()
    (repo / "inventory" / "extra.py").write_text("def extra():\n    return 1\n", encoding="utf-8")
    (repo / "inventory" / "storage.py").rename(repo / "inventory" / "store.py")
    (repo / "main.py").unlink()
    todo, gone, _ = ix.scan()
    assert set(todo) == {"inventory/extra.py", "inventory/store.py"} and set(gone) == {"inventory/storage.py", "main.py"}
    ix.build()
    files = ix.manifest["files"]
    assert "inventory/store.py" in files and "inventory/storage.py" not in files and "main.py" not in files
    assert ix.lexical.chunks_of("main.py") == [] and ix.lexical.chunks_of("inventory/store.py")
    assert {c.symbol for c in ix.lexical.chunks_of("inventory/store.py")} == {"load_items", "<module>"}


def test_a_file_that_becomes_secret_or_ignored_is_removed(repo):
    ix = make(repo)
    ix.build()
    p = repo / "inventory" / "config.py"
    p.write_text(p.read_text() + f'\nTOKEN = "{SECRET}"\n', encoding="utf-8")
    assert ix.update(["inventory/config.py"])["removed"] == 1
    assert ix.lexical.chunks_of("inventory/config.py") == [] and "inventory/config.py" not in ix.manifest["files"]


def test_deleted_file_update(repo):
    ix = make(repo)
    ix.build()
    (repo / "main.py").unlink()
    assert ix.update(["main.py"])["removed"] == 1 and ix.lexical.chunks_of("main.py") == []


# ─── manifest versioning ────────────────────────────────────

def test_model_or_chunker_change_triggers_full_rebuild(repo, monkeypatch):
    ix = make(repo)
    ix.build()
    ix.close()
    events = []
    other = Indexer(repo, "ws1", FakeEmbedder(dim=32, model_id="another-model"), emit=lambda t, **d: events.append(t))
    assert "embedding_model" in other.open() and other.lexical.count() == 0 and not other.ready()
    assert "index_rebuild" in events
    other.close()
    monkeypatch.setattr(ixm, "CHUNKER_VERSION", "999")
    again = Indexer(repo, "ws1", FakeEmbedder(dim=32, model_id="another-model"))
    assert "chunker_version" in again.open()
    again.close()


def test_corrupt_manifest_triggers_rebuild(repo):
    ix = make(repo)
    ix.build()
    ix.close()
    (ix.dir / "manifest.json").write_text("{not json", encoding="utf-8")
    again = Indexer(repo, "ws1", FakeEmbedder(dim=32))
    assert "unreadable" in again.open() and again.lexical.count() == 0
    assert again.build()["chunks"] > 0
    again.close()


def test_stale_paths(repo):
    ix = make(repo)
    ix.build()
    assert ix.stale_paths(["main.py", "inventory/pricing.py"]) == []
    (repo / "main.py").write_text("print(1)\n", encoding="utf-8")
    assert ix.stale_paths(["main.py", "inventory/pricing.py"]) == ["main.py"]


# ─── background indexing ────────────────────────────────────

def test_background_build_emits_progress_and_finishes(repo):
    events = []
    mgr = IndexManager(lambda: FakeEmbedder(dim=32), emit=lambda w, t, d: events.append((t, d)))
    assert mgr.start("ws1", repo)["state"] == "indexing"
    st = mgr.wait("ws1", timeout=60)
    assert st["state"] == "ready" and st["files"] > 0 and mgr.ready_indexer("ws1") is not None
    types = [t for t, _ in events]
    assert types[0] == "index_started" and "index_progress" in types and types[-1] == "index_finished"
    last = [d for t, d in events if t == "index_progress"][-1]
    assert last["done"] == last["total"]


def test_cancel_and_resume(repo, monkeypatch):
    for i in range(80):
        (repo / f"gen_{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    slow = threading.Event()

    class SlowFake(FakeEmbedder):
        def embed_documents(self, texts):
            slow.wait(0.05)
            return super().embed_documents(texts)

    first_batch = threading.Event()
    mgr = IndexManager(lambda: SlowFake(dim=32), emit=lambda w, t, d: first_batch.set() if t == "index_progress" else None)
    mgr.start("ws1", repo)
    assert first_batch.wait(60)
    assert mgr.cancel("ws1")
    st = mgr.wait("ws1", timeout=60)
    assert st["state"] == "cancelled" and mgr.ready_indexer("ws1") is None
    partial = len(mgr.indexer("ws1", repo).manifest["files"])
    assert 0 < partial
    mgr.start("ws1", repo)                                        # resumes: already-indexed files are skipped
    assert mgr.wait("ws1", timeout=120)["state"] == "ready"
    assert len(mgr.indexer("ws1", repo).manifest["files"]) > partial


def test_failure_is_reported_not_raised(repo):
    class Broken(FakeEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("model exploded")

    events = []
    mgr = IndexManager(lambda: Broken(dim=8), emit=lambda w, t, d: events.append(t))
    mgr.start("ws1", repo)
    st = mgr.wait("ws1", timeout=60)
    assert st["state"] == "failed" and "model exploded" in st["error"] and "index_failed" in events


def test_delete_removes_the_index_only(repo):
    mgr = IndexManager(lambda: FakeEmbedder(dim=16))
    mgr.start("ws1", repo)
    mgr.wait("ws1", timeout=60)
    before = tree_hash(repo)
    d = mgr.indexer("ws1", repo).dir
    assert mgr.delete("ws1") and not d.exists() and tree_hash(repo) == before
    assert mgr.status("ws1")["state"] == "none"


def test_notify_changes_updates_an_existing_index(repo):
    mgr = IndexManager(lambda: FakeEmbedder(dim=16))
    assert mgr.notify_changes("ws1", ["main.py"]) is None          # no index yet: nothing to do
    mgr.start("ws1", repo)
    mgr.wait("ws1", timeout=60)
    (repo / "main.py").write_text("def brand_new():\n    return 1\n", encoding="utf-8")
    assert mgr.notify_changes("ws1", ["main.py"])["indexed"] == 1
    assert "brand_new" in {c.symbol for c in mgr.indexer("ws1", repo).lexical.chunks_of("main.py")}
