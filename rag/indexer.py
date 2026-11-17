"""Workspace indexing: discovery (V1 path/ignore/protected/secret rules), chunks -> local embeddings -> Chroma + FTS5 + import
map, a versioned manifest, incremental updates, and a background build with progress and cancel.

The index lives under config.INDEX_DIR/<workspace_id> (never inside the workspace) and can be deleted and rebuilt at any time.
Ignored files, .env*, protected paths, binary/minified/oversized files and anything the secret scan flags are never indexed."""
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
from core.safety import SafetyError, contains_secret, is_ignored, is_protected, load_ignore_patterns, resolve_in_root
from rag.chunker import CHUNKER_VERSION, chunk_file
from rag.embedder import CachedEmbedder
from rag.import_map import ImportMap, parse_imports
from rag.lexical_index import LexicalIndex
from rag.vector_store import ChromaStore

SCHEMA_VERSION = 1
MAX_LINE_CHARS = 1000          # longer lines mean minified/generated code
MINIFIED_SUFFIXES = (".min.js", ".min.css", ".min.mjs", ".bundle.js", ".map")
BATCH = 32                     # files per batch between progress/cancel checks
NEXURA_FILES = {".nexuraignore"}   # Nexura's own config (it lists private file names): never indexed


def _norm_rel(p: str) -> str:
    p = p.replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def index_dir_for(workspace_id: str) -> Path:
    return Path(config.INDEX_DIR) / workspace_id


@dataclass
class FileInfo:
    rel: str
    hash: str
    mtime: float
    text: str = field(repr=False, default="")


@dataclass
class Discovery:
    files: dict[str, FileInfo]
    skipped: dict[str, str]    # rel -> reason (ignored files are not listed)
    capped: bool = False


def check_file(root: Path, rel: str, patterns: list[str], max_bytes: int) -> tuple[FileInfo | None, str | None]:
    """(info, None) if the file may be indexed, else (None, reason). reason None means silently ignored."""
    if is_ignored(rel, patterns) or rel.split("/")[-1] in NEXURA_FILES:
        return None, None
    if is_protected(rel, config.PROTECTED_PATHS):
        return None, "protected"
    try:
        full = resolve_in_root(root, rel)             # rejects symlinks that lead outside the workspace
    except SafetyError:
        return None, "outside workspace"
    if not full.is_file():
        return None, "missing"
    if rel.lower().endswith(MINIFIED_SUFFIXES):
        return None, "minified"
    try:
        st = full.stat()
        if st.st_size > max_bytes:
            return None, "too large"
        data = full.read_bytes()
    except OSError:
        return None, "unreadable"
    if b"\0" in data[:8192]:
        return None, "binary"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not utf-8"
    if any(len(line) > MAX_LINE_CHARS for line in text.splitlines()):
        return None, "minified"
    if contains_secret(text):
        return None, "secret"
    return FileInfo(rel, hashlib.sha256(data).hexdigest(), st.st_mtime, text), None


def discover(root: Path | str, max_files: int | None = None, max_bytes: int | None = None, read_text: bool = False) -> Discovery:
    root = Path(root)
    max_files, max_bytes = max_files or config.INDEX_MAX_FILES, max_bytes or config.INDEX_MAX_FILE_BYTES
    patterns = load_ignore_patterns(root)
    files: dict[str, FileInfo] = {}
    skipped: dict[str, str] = {}
    capped = False
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(d for d in dirnames
                             if not is_ignored(rel_dir + d + "/", patterns) and not is_protected(rel_dir + d + "/", config.PROTECTED_PATHS)
                             and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            rel = rel_dir + name
            info, reason = check_file(root, rel, patterns, max_bytes)
            if info is None:
                if reason:
                    skipped[rel] = reason
                continue
            if len(files) >= max_files:
                capped = True
                continue
            if not read_text:
                info.text = ""
            files[rel] = info
    return Discovery(files, skipped, capped)


class Cancelled(Exception):
    pass


class Indexer:
    """One workspace's index. Thread-safe: update() may run while a background build is in progress."""

    def __init__(self, root: Path | str, workspace_id: str, embedder, index_dir: Path | str | None = None, emit=None):
        self.root = Path(root).resolve()
        self.workspace_id = workspace_id
        self.dir = Path(index_dir) if index_dir else index_dir_for(workspace_id)
        self.embedder = CachedEmbedder(embedder, Path(config.INDEX_DIR) / "embed_cache.db") \
            if not isinstance(embedder, CachedEmbedder) else embedder
        self.emit = emit or (lambda type, **data: None)
        self._lock = threading.RLock()
        self._open = False
        self.manifest: dict = {}
        self.notice = ""

    # ── storage ──
    def _stores(self):
        if not self._open:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.vectors = ChromaStore(self.dir / "chroma")
            self.lexical = LexicalIndex(self.dir / "lexical.db")
            self.imports = ImportMap(self.dir / "lexical.db")
            self._open = True

    def _empty_manifest(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "chunker_version": CHUNKER_VERSION, "embedding_model": self.embedder.model_id,
                "embedding_dim": None, "complete": False, "files": {}}

    def _save_manifest(self) -> None:
        tmp = self.dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(self.manifest), encoding="utf-8")
        os.replace(tmp, self.dir / "manifest.json")

    def open(self) -> str:
        """Load the manifest. A missing/corrupt manifest or a model/chunker/schema change wipes the index (full rebuild).
        Returns a human-readable notice ('' when the existing index is reused)."""
        with self._lock:
            path = self.dir / "manifest.json"
            notice = ""
            m = None
            if path.exists():
                try:
                    m = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(m.get("files"), dict):
                        raise ValueError("bad manifest")
                except (ValueError, OSError):
                    m, notice = None, "index manifest was unreadable; rebuilding the index"
            if m is not None:
                for key, want in (("schema_version", SCHEMA_VERSION), ("chunker_version", CHUNKER_VERSION),
                                  ("embedding_model", self.embedder.model_id)):
                    if m.get(key) != want:
                        m, notice = None, f"index {key} changed ({m.get(key)} -> {want}); rebuilding the index"
                        break
            if m is None:
                self._wipe()
                self.manifest = self._empty_manifest()
            else:
                self.manifest = m
                try:
                    self._stores()
                except Exception:
                    self._wipe()
                    self.manifest, notice = self._empty_manifest(), "index store was unreadable; rebuilding the index"
            self._stores()
            self._save_manifest()
            self.notice = notice
            if notice:
                self.emit("index_rebuild", reason=notice)
            return notice

    def _wipe(self) -> None:
        self.close()
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)

    def close(self) -> None:
        if self._open:
            for s in (self.vectors, self.lexical, self.imports):
                try:
                    s.close()
                except Exception:
                    pass
            self._open = False

    def delete(self) -> None:
        """Remove the whole index (the workspace is untouched)."""
        with self._lock:
            self._wipe()
            self.manifest = {}

    # ── updates ──
    def scan(self) -> tuple[list[str], list[str], Discovery]:
        """Compare disk with the manifest: (paths to (re)index, paths to remove, discovery). Renames = remove + add."""
        d = discover(self.root)
        known = self.manifest.get("files", {})
        todo = [rel for rel, info in d.files.items() if known.get(rel, {}).get("hash") != info.hash]
        gone = [rel for rel in known if rel not in d.files]
        return todo, gone, d

    def _remove(self, rel: str) -> None:
        self.vectors.delete_by_path(rel)
        self.lexical.delete_by_path(rel)
        self.imports.delete_file(rel)
        self.manifest["files"].pop(rel, None)

    def _index_file(self, info: FileInfo) -> int:
        chunks = chunk_file(info.rel, info.text)
        vectors = self.embedder.embed_documents([c.enriched_text for c in chunks]) if chunks else []
        if vectors and not self.manifest.get("embedding_dim"):
            self.manifest["embedding_dim"] = len(vectors[0])
        self._remove(info.rel)
        if chunks:
            self.vectors.upsert(chunks, vectors)
            self.lexical.upsert(chunks)
        self.imports.set_file(info.rel, parse_imports(self.root, info.rel, info.text))
        self.manifest["files"][info.rel] = {"hash": info.hash, "mtime": info.mtime, "chunk_ids": [c.id for c in chunks]}
        return len(chunks)

    def update(self, paths: list[str]) -> dict:
        """Re-index these workspace-relative paths now (changed, added or deleted). Ineligible/deleted files are removed."""
        with self._lock:
            if not self.manifest:
                self.open()
            patterns = load_ignore_patterns(self.root)
            done = {"indexed": 0, "removed": 0, "chunks": 0}
            for rel in dict.fromkeys(_norm_rel(p) for p in paths):
                info, _ = check_file(self.root, rel, patterns, config.INDEX_MAX_FILE_BYTES)
                if info is None:
                    if rel in self.manifest["files"]:
                        self._remove(rel)
                        done["removed"] += 1
                    continue
                if self.manifest["files"].get(rel, {}).get("hash") == info.hash:
                    continue
                done["chunks"] += self._index_file(info)
                done["indexed"] += 1
            self._save_manifest()
            return done

    def build(self, cancel: threading.Event | None = None, progress=None) -> dict:
        """Bring the index in line with the disk (resumes from the manifest). Raises Cancelled if `cancel` is set."""
        t0 = time.time()
        with self._lock:
            if not self.manifest:
                self.open()
            todo, gone, d = self.scan()
            self.manifest["complete"] = False
            for rel in gone:
                self._remove(rel)
            self._save_manifest()
        total = len(todo)
        self.emit("index_started", total=total, skipped=len(d.skipped), capped=d.capped)
        patterns = load_ignore_patterns(self.root)
        for i in range(0, total, BATCH):
            if cancel is not None and cancel.is_set():
                self.emit("index_cancelled", done=i, total=total)
                raise Cancelled()
            with self._lock:
                for rel in todo[i:i + BATCH]:
                    info, _ = check_file(self.root, rel, patterns, config.INDEX_MAX_FILE_BYTES)
                    if info is not None:
                        self._index_file(info)
                    elif rel in self.manifest["files"]:
                        self._remove(rel)
                self._save_manifest()
            done = min(i + BATCH, total)
            self.emit("index_progress", done=done, total=total)
            if progress:
                progress(done, total)
        with self._lock:
            self.manifest["complete"] = True
            self._save_manifest()
        result = {"files": len(self.manifest["files"]), "chunks": self.lexical.count(), "seconds": round(time.time() - t0, 2),
                  "skipped": len(d.skipped), "capped": d.capped}
        if d.capped:
            result["notice"] = f"only the first {config.INDEX_MAX_FILES} files were indexed (NEXURA_INDEX_MAX_FILES)"
        self.emit("index_finished", **result)
        return result

    # ── state ──
    def retriever(self):
        from rag.retriever import Retriever
        return Retriever(self)

    def ready(self) -> bool:
        return bool(self.manifest.get("complete")) and self.manifest.get("embedding_model") == self.embedder.model_id

    def stale_paths(self, paths: list[str]) -> list[str]:
        """Of these paths, the ones whose disk content differs from what the index holds."""
        out = []
        for rel in paths:
            try:
                data = resolve_in_root(self.root, rel).read_bytes()
                h = hashlib.sha256(data).hexdigest()
            except (SafetyError, OSError):
                h = None
            if self.manifest.get("files", {}).get(rel, {}).get("hash") != h:
                out.append(rel)
        return out

    def status(self) -> dict:
        files = self.manifest.get("files", {})
        return {"ready": self.ready(), "files": len(files), "chunks": sum(len(f["chunk_ids"]) for f in files.values()),
                "embedding_model": self.manifest.get("embedding_model"), "embedding_dim": self.manifest.get("embedding_dim"),
                "notice": self.notice}


class IndexManager:
    """Background indexing per workspace, with progress events and cancel. A run can start while indexing is in progress:
    until the index is ready it uses the V1 context builder."""

    def __init__(self, embedder_factory, emit=None):
        self.embedder_factory = embedder_factory
        self.emit = emit or (lambda workspace_id, type, data: None)
        self._lock = threading.Lock()
        self._ix: dict[str, Indexer] = {}
        self._jobs: dict[str, dict] = {}

    def indexer(self, workspace_id: str, root: Path | str) -> Indexer:
        with self._lock:
            if workspace_id not in self._ix:
                self._ix[workspace_id] = Indexer(root, workspace_id, self.embedder_factory(),
                                                 emit=lambda type, **data: self.emit(workspace_id, type, data))
            return self._ix[workspace_id]

    def start(self, workspace_id: str, root: Path | str, rebuild: bool = False) -> dict:
        with self._lock:
            job = self._jobs.get(workspace_id)
            if job and job["thread"].is_alive():
                return self.status(workspace_id, _locked=True)
        ix = self.indexer(workspace_id, root)
        cancel = threading.Event()

        def work():
            job = self._jobs[workspace_id]
            try:
                if rebuild:
                    ix.delete()
                ix.open()
                job["result"] = ix.build(cancel=cancel, progress=lambda d, t: job.update(done=d, total=t))
                job["state"] = "ready"
            except Cancelled:
                job["state"] = "cancelled"
            except Exception as e:   # indexing must never take anything else down
                job["state"], job["error"] = "failed", f"{type(e).__name__}: {str(e)[:300]}"
                self.emit(workspace_id, "index_failed", {"reason": job["error"]})

        with self._lock:
            self._jobs[workspace_id] = {"state": "indexing", "done": 0, "total": None, "cancel": cancel, "error": None,
                                        "thread": threading.Thread(target=work, daemon=True, name=f"index-{workspace_id[:8]}")}
            self._jobs[workspace_id]["thread"].start()
            return self.status(workspace_id, _locked=True)

    def wait(self, workspace_id: str, timeout: float | None = None) -> dict:
        job = self._jobs.get(workspace_id)
        if job:
            job["thread"].join(timeout)
        return self.status(workspace_id)

    def cancel(self, workspace_id: str) -> bool:
        job = self._jobs.get(workspace_id)
        if not job or not job["thread"].is_alive():
            return False
        job["cancel"].set()
        return True

    def delete(self, workspace_id: str) -> bool:
        self.cancel(workspace_id)
        self.wait(workspace_id, timeout=30)
        with self._lock:
            ix = self._ix.pop(workspace_id, None)
            self._jobs.pop(workspace_id, None)
        target = ix.dir if ix else index_dir_for(workspace_id)
        if ix:
            ix.delete()
        elif target.exists():
            shutil.rmtree(target, ignore_errors=True)
        return True

    def ready_indexer(self, workspace_id: str) -> Indexer | None:
        ix = self._ix.get(workspace_id)
        job = self._jobs.get(workspace_id)
        if ix is None or (job and job["thread"].is_alive()) or not ix.ready():
            return None
        return ix

    def existing(self, workspace_id: str) -> Indexer | None:
        """The workspace's opened index (even while it is still building), so a run's edits keep it fresh."""
        ix = self._ix.get(workspace_id)
        return ix if ix is not None and ix.manifest else None

    def notify_changes(self, workspace_id: str, paths: list[str]) -> dict | None:
        """External edits (file watcher, undo): re-index those files now if this workspace has an index."""
        ix = self._ix.get(workspace_id)
        return ix.update(paths) if ix and ix.manifest else None

    def status(self, workspace_id: str, _locked: bool = False) -> dict:
        job = self._jobs.get(workspace_id)
        ix = self._ix.get(workspace_id)
        out = {"workspace_id": workspace_id, "state": "none"}
        if ix is not None and ix.manifest:
            out.update(ix.status())
            out["state"] = "ready" if ix.ready() else "incomplete"
        if job:
            out.update(state=job["state"] if not job["thread"].is_alive() else "indexing", done=job["done"], total=job["total"],
                       error=job["error"], **({"result": job["result"]} if job.get("result") else {}))
        return out
