"""Vector store behind a small interface. ChromaStore is the only implementation; nothing else imports chromadb.
Only vectors and metadata are stored here (the chunk text lives in the lexical index)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VectorHit:
    id: str
    score: float          # cosine similarity, higher is better
    metadata: dict


class VectorStore(ABC):
    @abstractmethod
    def upsert(self, chunks: list, vectors: list[list[float]]) -> None: ...

    @abstractmethod
    def delete_by_path(self, path: str) -> None: ...

    @abstractmethod
    def delete_ids(self, ids: list[str]) -> None: ...

    @abstractmethod
    def query(self, vector: list[float], k: int, filters: dict | None = None) -> list[VectorHit]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def reset(self) -> None: ...

    def close(self) -> None:   # noqa: B027  (optional hook: release file handles)
        return None


def _meta(c) -> dict:
    return {"path": c.path, "language": c.language, "kind": c.kind, "symbol": c.symbol or "", "start_line": c.start_line,
            "end_line": c.end_line, "content_hash": c.content_hash}


class ChromaStore(VectorStore):
    COLLECTION = "chunks"

    def __init__(self, path: Path | str):
        import chromadb
        from chromadb.config import Settings
        Path(path).mkdir(parents=True, exist_ok=True)
        # telemetry off: indexing must make no network calls
        self._client = chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False, allow_reset=True))
        self._col = self._client.get_or_create_collection(self.COLLECTION, metadata={"hnsw:space": "cosine"})

    def upsert(self, chunks, vectors):
        for i in range(0, len(chunks), 1000):
            part = chunks[i:i + 1000]
            self._col.upsert(ids=[c.id for c in part], embeddings=vectors[i:i + 1000], metadatas=[_meta(c) for c in part])

    def delete_by_path(self, path):
        self._col.delete(where={"path": path})

    def delete_ids(self, ids):
        if ids:
            self._col.delete(ids=list(ids))

    def query(self, vector, k, filters=None):
        n = self.count()
        if n == 0:
            return []
        where = None
        if filters:
            conds = [{key: value} for key, value in filters.items()]
            where = conds[0] if len(conds) == 1 else {"$and": conds}
        r = self._col.query(query_embeddings=[vector], n_results=min(k, n), where=where)
        return [VectorHit(i, 1.0 - d, m) for i, d, m in zip(r["ids"][0], r["distances"][0], r["metadatas"][0], strict=True)]

    def count(self):
        return self._col.count()

    def reset(self):
        self._client.delete_collection(self.COLLECTION)
        self._col = self._client.get_or_create_collection(self.COLLECTION, metadata={"hnsw:space": "cosine"})

    def close(self):
        # release file handles (Windows cannot delete an open index otherwise)
        import gc
        if self._client is None:
            return
        for release in (lambda: self._client._system.stop(), lambda: self._client.clear_system_cache()):
            try:
                release()
            except Exception:
                pass
        self._col = None
        self._client = None
        gc.collect()
