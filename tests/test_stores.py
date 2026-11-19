"""V2-2.5/2.6: vector store (Chroma behind an interface) and SQLite FTS5 keyword index."""
import pytest

pytest.importorskip("chromadb")
from rag.chunker import chunk_file  # noqa: E402
from rag.embedder import FakeEmbedder  # noqa: E402
from rag.lexical_index import LexicalIndex, split_identifiers  # noqa: E402
from rag.vector_store import ChromaStore, VectorStore  # noqa: E402

SRC = {
    "app/config.py": "def load_config(path):\n    return read_json(path)\n\n\ndef parse_port(value):\n    return int(value)\n",
    "app/users.py": "class UserStore:\n    def add_user(self, name):\n        return name\n\n    def removeUser(self, name):\n        return None\n",
    "web/draw.js": "export function drawCircle(ctx, r) {\n  ctx.arc(0, 0, r, 0, 7);\n}\n",
}


@pytest.fixture
def chunks():
    return [c for p, t in SRC.items() for c in chunk_file(p, t)]


@pytest.fixture
def store(tmp_path):
    s = ChromaStore(tmp_path / "chroma")
    yield s
    s.close()


def test_chroma_is_a_vector_store(store):
    assert isinstance(store, VectorStore)


def test_upsert_query_delete(store, chunks):
    e = FakeEmbedder(dim=32)
    store.upsert(chunks, e.embed_documents([c.enriched_text for c in chunks]))
    assert store.count() == len(chunks)
    hits = store.query(e.embed_query("load configuration file"), k=3)
    assert hits[0].metadata["path"] == "app/config.py" and hits[0].metadata["symbol"] == "load_config"
    assert set(hits[0].metadata) >= {"path", "language", "kind", "symbol", "start_line", "end_line", "content_hash"}
    only_js = store.query(e.embed_query("circle"), k=5, filters={"language": "javascript"})
    assert {h.metadata["path"] for h in only_js} == {"web/draw.js"}
    store.delete_by_path("app/config.py")
    assert store.count() == len(chunks) - 2 and all(h.metadata["path"] != "app/config.py" for h in store.query(e.embed_query("config"), 10))
    store.delete_ids([chunks[-1].id])
    assert store.count() == len(chunks) - 3


def test_upsert_is_idempotent_and_persistent(tmp_path, chunks):
    e = FakeEmbedder(dim=16)
    s = ChromaStore(tmp_path / "c")
    s.upsert(chunks, e.embed_documents([c.enriched_text for c in chunks]))
    s.upsert(chunks, e.embed_documents([c.enriched_text for c in chunks]))
    assert s.count() == len(chunks)
    s.close()
    again = ChromaStore(tmp_path / "c")
    assert again.count() == len(chunks)
    again.reset()
    assert again.count() == 0
    again.close()


def test_chroma_stores_no_code_text(tmp_path, chunks):
    e = FakeEmbedder(dim=16)
    s = ChromaStore(tmp_path / "c")
    s.upsert(chunks, e.embed_documents([c.enriched_text for c in chunks]))
    s.close()
    blob = b"".join(p.read_bytes() for p in (tmp_path / "c").rglob("*") if p.is_file())
    assert b"read_json(path)" not in blob


def test_identifier_splitting():
    assert split_identifiers("load_config removeUser HTTPServer parse2int") == \
        ["load", "config", "remove", "user", "http", "server", "parse", "2", "int"]


def test_lexical_search_matches_identifier_parts(tmp_path, chunks):
    lx = LexicalIndex(tmp_path / "lexical.db")
    lx.upsert(chunks)
    assert lx.search("removeUser", 3)[0].symbol == "UserStore.removeUser"            # exact identifier
    assert lx.search("remove user", 3)[0].symbol == "UserStore.removeUser"           # its parts
    assert lx.search("config", 3)[0].path == "app/config.py"
    assert lx.search("load_config", 1)[0].symbol == "load_config"
    assert lx.get(chunks[0].id).text == chunks[0].text
    assert lx.search("", 5) == [] and lx.search("\"'*(:)", 5) == []                   # odd input never raises
    lx.delete_by_path("app/users.py")
    assert all(h.path != "app/users.py" for h in lx.search("user", 10))
    assert lx.count() == len([c for c in chunks if c.path != "app/users.py"])
    lx.close()


def test_lexical_upsert_replaces(tmp_path, chunks):
    lx = LexicalIndex(tmp_path / "l.db")
    lx.upsert(chunks)
    lx.upsert(chunks)
    assert lx.count() == len(chunks)
    lx.reset()
    assert lx.count() == 0
    lx.close()
