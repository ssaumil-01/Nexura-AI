"""V2-2.4: local embeddings with a content-hash cache; models are only downloaded after explicit consent."""
import math

import pytest

import config
from rag import embedder as emb


def test_fake_embedder_is_deterministic_normalised_and_similarity_aware():
    f = emb.FakeEmbedder(dim=64)
    a, a2, b = f.embed_documents(["load the user config file", "load the user config file", "draw a red circle"])
    assert a == a2 and len(a) == 64 and math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-6)
    q = f.embed_query("config file loading")
    dot = lambda u, v: sum(x * y for x, y in zip(u, v, strict=True))   # noqa: E731
    assert dot(q, a) > dot(q, b)


def test_cache_embeds_each_text_once(tmp_path):
    inner = emb.FakeEmbedder(dim=16)
    calls = []
    real = inner.embed_documents
    inner.embed_documents = lambda texts: calls.append(list(texts)) or real(texts)
    cached = emb.CachedEmbedder(inner, tmp_path / "cache.db")
    v1 = cached.embed_documents(["a", "b"])
    v2 = cached.embed_documents(["b", "c", "a"])
    assert calls == [["a", "b"], ["c"]]
    assert v2[0] == v1[1] and v2[2] == v1[0]
    again = emb.CachedEmbedder(emb.FakeEmbedder(dim=16), tmp_path / "cache.db")   # persists across instances
    assert again.embed_documents(["a"]) == [v1[0]]


def test_cache_is_per_model(tmp_path):
    a = emb.CachedEmbedder(emb.FakeEmbedder(dim=8, model_id="m1"), tmp_path / "c.db")
    b = emb.CachedEmbedder(emb.FakeEmbedder(dim=8, model_id="m2"), tmp_path / "c.db")
    a.embed_documents(["x"])
    assert b.stats()["entries"] == 0 and a.stats()["entries"] == 1


def test_cache_stores_vectors_not_text(tmp_path):
    c = emb.CachedEmbedder(emb.FakeEmbedder(dim=8), tmp_path / "c.db")
    c.embed_documents(["SECRET_LOOKING_TEXT_12345"])
    assert b"SECRET_LOOKING_TEXT_12345" not in (tmp_path / "c.db").read_bytes()


def test_local_model_needs_consent_when_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXURA_ALLOW_MODEL_DOWNLOAD", raising=False)
    tried = []

    class FakeTextEmbedding:
        def __init__(self, model_name, cache_dir=None, local_files_only=False, **kw):
            tried.append(local_files_only)
            if local_files_only:
                raise ValueError("not found locally")

    monkeypatch.setattr(emb, "_text_embedding_class", lambda: FakeTextEmbedding)
    with pytest.raises(emb.ModelNotAvailable) as e:
        emb.LocalEmbedder("BAAI/bge-small-en-v1.5", cache_dir=tmp_path).embed_query("x")
    assert tried == [True]                                                     # never tried to download
    assert "NEXURA_ALLOW_MODEL_DOWNLOAD" in str(e.value) and "offline" in str(e.value).lower()


def test_local_model_downloads_only_with_consent(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXURA_ALLOW_MODEL_DOWNLOAD", "1")
    tried = []

    class FakeTextEmbedding:
        def __init__(self, model_name, cache_dir=None, local_files_only=False, **kw):
            tried.append(local_files_only)
            if local_files_only:
                raise ValueError("not found locally")

        def query_embed(self, texts):
            return iter([[0.0, 1.0]])

    monkeypatch.setattr(emb, "_text_embedding_class", lambda: FakeTextEmbedding)
    assert emb.LocalEmbedder("m", cache_dir=tmp_path).embed_query("x") == [0.0, 1.0]
    assert tried == [True, False]


def test_make_embedder_uses_the_configured_model(monkeypatch, tmp_path):
    e = emb.make_embedder(cache_dir=tmp_path)
    assert e.model_id == config.EMBEDDING_MODEL


def test_huggingface_telemetry_is_disabled():
    import os
    assert os.environ.get("HF_HUB_DISABLE_TELEMETRY") == "1"
