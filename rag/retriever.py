"""Hybrid retrieval over the code index: vector top-N (meaning) + FTS5 top-N (words), fused with Reciprocal Rank Fusion, plus
boosts for exact path/symbol matches, files named in the task and files changed in this run. Overlapping chunks of the same
file are de-duplicated; every result carries its sources and a human-readable reason."""
import re
from dataclasses import dataclass, field

import config
from rag.lexical_index import StoredChunk

PATH_LIKE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,5}\b")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
NAMED_WEIGHT = 0.5     # boosts, in units of "rank 1 in one list" (1 / (RRF_K + 1))
RECENT_WEIGHT = 0.25
EXACT_WEIGHT = 1.0
MAX_PER_FILE = 1       # at most this many chunks of one file in the results (0 = no cap). Chosen on the tuning half of the retrieval labels: more distinct files, no loss of labelled symbols (docs/V2_EVAL.md)


@dataclass
class RetrievedChunk:
    chunk: StoredChunk
    score: float
    sources: list[str] = field(default_factory=list)   # vector | lexical | path | symbol | named | recent
    reason: str = ""


class Retriever:
    def __init__(self, indexer):
        self.ix = indexer

    def _symbol_hits(self, names: list[str]) -> list[StoredChunk]:
        if not names:
            return []
        db = self.ix.lexical.db
        out = []
        for n in names:
            rows = db.execute("SELECT * FROM chunks WHERE symbol = ? OR symbol LIKE ? OR symbol LIKE ?",
                              (n, f"%.{n}", f"{n}#%")).fetchall()
            out += [StoredChunk(*r) for r in rows]
        return out

    def _path_hits(self, query: str) -> list[StoredChunk]:
        known = self.ix.manifest.get("files", {})
        out = []
        for m in PATH_LIKE.findall(query):
            rel = m.replace("\\", "/")
            rel = rel[2:] if rel.startswith("./") else rel
            if rel in known:
                out += self.ix.lexical.chunks_of(rel)
        return out

    def retrieve(self, query: str, k: int | None = None, filters: dict | None = None, named_paths=(), recent_paths=(),
                 mode: str = "hybrid") -> list[RetrievedChunk]:
        k = k or config.TOP_K
        query = (query or "").strip()
        if not query:
            return []
        n = max(4 * k, 20)
        unit = 1.0 / (config.RRF_K + 1)
        scores: dict[str, float] = {}
        sources: dict[str, list[str]] = {}
        why: dict[str, list[str]] = {}
        chunks: dict[str, StoredChunk] = {}

        def add(cid: str, amount: float, source: str, reason: str, chunk: StoredChunk | None = None):
            scores[cid] = scores.get(cid, 0.0) + amount
            if source not in sources.setdefault(cid, []):
                sources[cid].append(source)
            why.setdefault(cid, []).append(reason)
            if chunk is not None:
                chunks[cid] = chunk

        if mode in ("hybrid", "vector"):
            vf = {"language": filters["language"]} if filters and filters.get("language") else None
            for rank, hit in enumerate(self.ix.vectors.query(self.ix.embedder.embed_query(query), n, vf), 1):
                add(hit.id, 1.0 / (config.RRF_K + rank), "vector", f"meaning match #{rank}")
        if mode in ("hybrid", "lexical"):
            for rank, c in enumerate(self.ix.lexical.search(query, n), 1):
                add(c.id, 1.0 / (config.RRF_K + rank), "lexical", f"keyword match #{rank}", c)
            for c in self._path_hits(query):
                add(c.id, EXACT_WEIGHT * unit, "path", f"path {c.path} is in the request", c)
            idents = [w for w in dict.fromkeys(IDENT.findall(query)) if "_" in w or any(ch.isupper() for ch in w[1:])
                      or w.lower() not in _STOP]
            for c in self._symbol_hits(idents):
                name = c.symbol.split(".")[-1].split("#")[0]
                add(c.id, EXACT_WEIGHT * unit, "symbol", f"symbol '{name}' is in the request", c)
        if not scores:
            return []
        for cid in list(scores):
            if cid not in chunks:
                c = self.ix.lexical.get(cid)
                if c is None:          # in the vector store but no longer in the chunk store: stale, skip
                    scores.pop(cid)
                    continue
                chunks[cid] = c
            c = chunks[cid]
            if c.path in named_paths:
                add(cid, NAMED_WEIGHT * unit, "named", "file named in the task")
            if c.path in recent_paths:
                add(cid, RECENT_WEIGHT * unit, "recent", "file changed in this run")
        ranked = sorted(scores, key=lambda cid: (-scores[cid], chunks[cid].path, chunks[cid].start_line))
        if filters:
            if filters.get("path_prefix"):
                ranked = [cid for cid in ranked if chunks[cid].path.startswith(filters["path_prefix"])]
            if filters.get("language"):
                ranked = [cid for cid in ranked if chunks[cid].language == filters["language"]]
        out: list[RetrievedChunk] = []
        for cid in ranked:
            c = chunks[cid]
            if any(o.chunk.path == c.path and not (o.chunk.end_line < c.start_line or c.end_line < o.chunk.start_line) for o in out):
                continue   # overlaps a better-ranked chunk of the same file (split windows)
            if MAX_PER_FILE and sum(o.chunk.path == c.path for o in out) >= MAX_PER_FILE:
                continue
            out.append(RetrievedChunk(c, round(scores[cid], 6), sources[cid], "; ".join(dict.fromkeys(why[cid]))))
            if len(out) >= k:
                break
        return out


# common English words that are not worth a symbol lookup
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "add", "use", "make", "new", "all", "every", "must", "should",
         "when", "not", "are", "its", "has", "have", "can", "will", "file", "files", "function", "class", "method", "code",
         "update", "change", "create", "remove", "delete", "return", "returns", "test", "tests", "keep", "existing", "list", "show"}
