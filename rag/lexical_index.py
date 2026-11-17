"""SQLite FTS5 keyword index over chunks. It is also the chunk store (text, lines, symbol) used to build context.
Identifiers are split (load_config -> load config, removeUser -> remove user) so both whole names and their parts match."""
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_WORDS = re.compile(r"[A-Za-z0-9_]+")


def split_identifiers(text: str) -> list[str]:
    out = []
    for word in _WORDS.findall(text):
        for piece in word.split("_"):
            out += [p.lower() for p in _PARTS.findall(piece)]
    return out


@dataclass
class StoredChunk:
    id: str
    path: str
    language: str
    kind: str
    symbol: str
    start_line: int
    end_line: int
    content_hash: str
    text: str
    score: float = 0.0     # bm25-derived, higher is better (search only)


SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, path TEXT NOT NULL, language TEXT, kind TEXT, symbol TEXT,
    start_line INTEGER, end_line INTEGER, content_hash TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(id UNINDEXED, path, symbol, body, tokenize='unicode61');
"""


class LexicalIndex:
    def __init__(self, db_path: Path | str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    @property
    def db(self) -> sqlite3.Connection:
        return self._db

    def upsert(self, chunks) -> None:
        with self._lock, self._db:
            ids = [c.id for c in chunks]
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                marks = ",".join("?" * len(part))
                self._db.execute(f"DELETE FROM fts WHERE id IN ({marks})", part)
                self._db.execute(f"DELETE FROM chunks WHERE id IN ({marks})", part)
            self._db.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)",
                                 [(c.id, c.path, c.language, c.kind, c.symbol, c.start_line, c.end_line, c.content_hash, c.text)
                                  for c in chunks])
            self._db.executemany("INSERT INTO fts (id, path, symbol, body) VALUES (?,?,?,?)",
                                 [(c.id, f"{c.path} {' '.join(split_identifiers(c.path))}",
                                   f"{c.symbol} {' '.join(split_identifiers(c.symbol or ''))}",
                                   f"{c.enriched_text}\n{' '.join(split_identifiers(c.text))}") for c in chunks])

    def delete_by_path(self, path: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM fts WHERE id IN (SELECT id FROM chunks WHERE path=?)", (path,))
            self._db.execute("DELETE FROM chunks WHERE path=?", (path,))

    def search(self, query: str, k: int) -> list[StoredChunk]:
        terms = list(dict.fromkeys([w.lower() for w in _WORDS.findall(query)] + split_identifiers(query)))
        if not terms:
            return []
        match = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
        sql = ("SELECT c.*, bm25(fts, 0.0, 2.0, 4.0, 1.0) AS rank FROM fts JOIN chunks c ON c.id = fts.id "
               "WHERE fts MATCH ? ORDER BY rank LIMIT ?")
        with self._lock:
            try:
                rows = self._db.execute(sql, (match, k)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [StoredChunk(*r[:9], score=-r[9]) for r in rows]

    def get(self, chunk_id: str) -> StoredChunk | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        return StoredChunk(*r) if r else None

    def chunks_of(self, path: str) -> list[StoredChunk]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM chunks WHERE path=? ORDER BY start_line", (path,)).fetchall()
        return [StoredChunk(*r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def reset(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM fts")
            self._db.execute("DELETE FROM chunks")

    def close(self) -> None:
        self._db.close()
