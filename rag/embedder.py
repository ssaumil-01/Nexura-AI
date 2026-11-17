"""Local text embeddings (code never leaves the machine). The model is downloaded on first use ONLY with explicit consent
(NEXURA_ALLOW_MODEL_DOWNLOAD=1, or the extension's consent prompt); otherwise a clear error explains the offline install.
CachedEmbedder stores vectors by (model, content hash) in SQLite so unchanged text is never embedded twice."""
import hashlib
import math
import os
import re
import sqlite3
import struct
import threading
from pathlib import Path

import config

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")   # the model hub client must not phone home

OFFLINE_HELP = ("The local embedding model '{model}' is not installed. To allow a one-time download (about 70 MB), set "
                "NEXURA_ALLOW_MODEL_DOWNLOAD=1 or accept the prompt in VS Code. Offline install: on a machine with internet run "
                "`python -c \"from fastembed import TextEmbedding; TextEmbedding('{model}', cache_dir='models')\"` and copy the "
                "'models' folder to {cache_dir}.")


class ModelNotAvailable(RuntimeError):
    pass


class FakeEmbedder:
    """Deterministic, tiny, offline: hashed bag of word pieces (identifiers split). Tests only."""

    def __init__(self, dim: int = 64, model_id: str = "fake"):
        self.dim, self.model_id = dim, model_id

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        words = re.findall(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+", text)
        for w in words:
            w = w.lower()
            stem = w[:5]   # crude stemming so "config"/"configuration" and "load"/"loading" meet
            i = int(hashlib.md5(stem.encode()).hexdigest(), 16) % self.dim
            v[i] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def _text_embedding_class():
    from fastembed import TextEmbedding
    return TextEmbedding


class LocalEmbedder:
    """fastembed (ONNX, CPU). Lazily loaded; never downloads without consent."""

    def __init__(self, model_id: str, cache_dir: Path | str | None = None):
        self.model_id = model_id
        self.cache_dir = Path(cache_dir or config.DATA_DIR / "models")
        self._model = None
        self._lock = threading.Lock()
        self.dim = None

    def _load(self):
        with self._lock:
            if self._model is not None:
                return self._model
            cls = _text_embedding_class()
            try:
                self._model = cls(self.model_id, cache_dir=str(self.cache_dir), local_files_only=True)
            except Exception:
                if not config.env_bool("NEXURA_ALLOW_MODEL_DOWNLOAD", False):
                    raise ModelNotAvailable(OFFLINE_HELP.format(model=self.model_id, cache_dir=self.cache_dir)) from None
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._model = cls(self.model_id, cache_dir=str(self.cache_dir))
            return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vecs = [list(map(float, v)) for v in self._load().passage_embed(list(texts))]
        if vecs:
            self.dim = len(vecs[0])
        return vecs

    def embed_query(self, text: str) -> list[float]:
        v = list(map(float, next(iter(self._load().query_embed([text])))))
        self.dim = len(v)
        return v


class CachedEmbedder:
    """Wraps an embedder with a persistent (model, sha256(text)) -> vector cache. Stores vectors only, never the text."""

    def __init__(self, inner, db_path: Path | str):
        self.inner, self.model_id = inner, inner.model_id
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS vectors (model TEXT, hash TEXT, dim INTEGER, vec BLOB, PRIMARY KEY (model, hash))")
        self._lock = threading.Lock()

    @property
    def dim(self):
        return getattr(self.inner, "dim", None)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
        found: dict[str, list[float]] = {}
        with self._lock:
            for i in range(0, len(hashes), 500):
                part = hashes[i:i + 500]
                q = f"SELECT hash, dim, vec FROM vectors WHERE model=? AND hash IN ({','.join('?' * len(part))})"
                for h, dim, blob in self._db.execute(q, [self.model_id, *part]):
                    found[h] = list(struct.unpack(f"{dim}f", blob))
        todo = list(dict.fromkeys(h for h in hashes if h not in found))
        if todo:
            text_of = dict(zip(hashes, texts, strict=True))
            new = self.inner.embed_documents([text_of[h] for h in todo])
            with self._lock, self._db:
                self._db.executemany("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
                                     [(self.model_id, h, len(v), struct.pack(f"{len(v)}f", *v)) for h, v in zip(todo, new, strict=True)])
            found.update(zip(todo, new, strict=True))
        return [found[h] for h in hashes]

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(text)

    def stats(self) -> dict:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM vectors WHERE model=?", (self.model_id,)).fetchone()[0]
        return {"entries": n}

    def close(self) -> None:
        self._db.close()


def make_embedder(cache_dir: Path | str | None = None):
    """The configured local model (not loaded until first use)."""
    return LocalEmbedder(config.EMBEDDING_MODEL, cache_dir=cache_dir)
