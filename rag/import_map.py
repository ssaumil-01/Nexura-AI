"""Flat import table for Python and JS/TS: imports(file, imported, kind, resolved_path, line).
One level only ("imports X", "imported by Y"); no graph traversal. Unresolved imports keep resolved_path NULL (never guessed)."""
import ast
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Parser

JS_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")
_grammar = {".ts": tree_sitter_typescript.language_typescript, ".mts": tree_sitter_typescript.language_typescript,
            ".cts": tree_sitter_typescript.language_typescript, ".tsx": tree_sitter_typescript.language_tsx}
_parsers: dict = {}


@dataclass
class ImportRow:
    file: str
    imported: str
    kind: str                     # import | from | require | export
    resolved_path: str | None
    line: int


def _exists(root: Path, rel: str) -> bool:
    p = (root / rel)
    return p.is_file() and root.resolve() in p.resolve().parents


def _norm(rel: str) -> str | None:
    parts: list[str] = []
    for part in rel.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None          # escapes the workspace
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


# ─── Python ─────────────────────────────────────────────────

def _py_module_file(root: Path, dotted_parts: list[str]) -> str | None:
    base = "/".join(dotted_parts)
    for cand in (f"{base}.py", f"{base}/__init__.py"):
        if base and _exists(root, cand):
            return cand
    return None


def _python(root: Path, file: str, text: str) -> list[ImportRow]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    pkg = file.split("/")[:-1]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append(ImportRow(file, a.name, "import", _py_module_file(root, a.name.split(".")), node.lineno))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                up = node.level - 1
                if up > len(pkg):
                    for a in node.names:
                        out.append(ImportRow(file, "." * node.level + (mod or a.name), "from", None, node.lineno))
                    continue
                base = pkg[:len(pkg) - up] if up else list(pkg)
                if mod:
                    out.append(ImportRow(file, "." * node.level + mod, "from", _py_module_file(root, base + mod.split(".")), node.lineno))
                else:   # from . import util  -> each name may be a module
                    for a in node.names:
                        out.append(ImportRow(file, "." * node.level + a.name, "from", _py_module_file(root, base + [a.name]), node.lineno))
            else:
                out.append(ImportRow(file, mod, "from", _py_module_file(root, mod.split(".")), node.lineno))
    return out


# ─── JavaScript / TypeScript ────────────────────────────────

def _ts_paths(root: Path) -> tuple[str, dict]:
    """Best effort tsconfig/jsconfig compilerOptions.baseUrl + paths."""
    for name in ("tsconfig.json", "jsconfig.json"):
        p = root / name
        if p.is_file():
            raw = p.read_text(encoding="utf-8", errors="ignore")
            # tsconfig allows comments; strip only whole-line ones ("@/*" inside strings must survive)
            for text in (raw, re.sub(r"(?m)^\s*//[^\n]*$", "", raw), re.sub(r"(?ms)^\s*/\*.*?\*/", "", re.sub(r"(?m)^\s*//[^\n]*$", "", raw))):
                try:
                    opts = json.loads(text).get("compilerOptions", {})
                    return opts.get("baseUrl", "."), opts.get("paths", {}) or {}
                except (ValueError, AttributeError):
                    continue
            return ".", {}
    return ".", {}


def _js_resolve(root: Path, file: str, spec: str, aliases) -> str | None:
    candidates = []
    if spec.startswith("."):
        base = _norm("/".join(file.split("/")[:-1] + [spec]))
        if base is None:
            return None
        candidates.append(base)
    else:
        base_url, paths = aliases
        for pattern, targets in paths.items():
            prefix = pattern.rstrip("*")
            if pattern.endswith("*") and spec.startswith(prefix) or spec == pattern:
                rest = spec[len(prefix):] if pattern.endswith("*") else ""
                for t in targets:
                    cand = _norm(f"{base_url}/{t.replace('*', rest)}")
                    if cand:
                        candidates.append(cand)
        if not candidates:
            return None      # a package (react, lodash): not part of this workspace
    for base in candidates:
        tries = [base] + [base + e for e in JS_EXTS] + [f"{base}/index{e}" for e in JS_EXTS]
        if base.endswith(".js"):     # TS projects import "./x.js" for x.ts
            tries += [base[:-3] + e for e in (".ts", ".tsx")]
        for t in tries:
            if _exists(root, t):
                return t
    return None


def _js(root: Path, file: str, text: str) -> list[ImportRow]:
    ext = "." + file.rsplit(".", 1)[-1].lower()
    key = ext if ext in _grammar else ".js"
    if key not in _parsers:
        _parsers[key] = Parser(Language(_grammar.get(key, tree_sitter_javascript.language)()))
    src = text.encode("utf-8")
    try:
        tree = _parsers[key].parse(src)
    except Exception:
        return []
    aliases = _ts_paths(root)
    out = []

    def string_of(node):
        s = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
        return s[1:-1] if len(s) >= 2 and s[0] in "'\"`" else None

    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        kind, spec_node = None, None
        if n.type in ("import_statement", "export_statement"):
            spec_node = n.child_by_field_name("source")
            kind = "import" if n.type == "import_statement" else "export"
        elif n.type == "call_expression":
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            name = src[fn.start_byte:fn.end_byte].decode() if fn is not None else ""
            if name in ("require", "import") and args is not None and args.named_child_count >= 1:
                spec_node, kind = args.named_children[0], ("require" if name == "require" else "import")
        if spec_node is not None and spec_node.type == "string":
            spec = string_of(spec_node)
            if spec:
                out.append(ImportRow(file, spec, kind, _js_resolve(root, file, spec, aliases), n.start_point[0] + 1))
        stack.extend(reversed(n.children))
    return out


def parse_imports(root: Path | str, file: str, text: str) -> list[ImportRow]:
    root = Path(root)
    if file.endswith(".py"):
        return _python(root, file, text)
    if file.lower().endswith(JS_EXTS):
        return _js(root, file, text)
    return []


# ─── storage ────────────────────────────────────────────────

class ImportMap:
    def __init__(self, db_path: Path | str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript("""CREATE TABLE IF NOT EXISTS imports (file TEXT NOT NULL, imported TEXT NOT NULL, kind TEXT,
            resolved_path TEXT, line INTEGER);
            CREATE INDEX IF NOT EXISTS imports_file ON imports(file);
            CREATE INDEX IF NOT EXISTS imports_resolved ON imports(resolved_path);""")
        self._lock = threading.Lock()

    def set_file(self, file: str, rows: list[ImportRow]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM imports WHERE file=?", (file,))
            self._db.executemany("INSERT INTO imports VALUES (?,?,?,?,?)",
                                 [(r.file, r.imported, r.kind, r.resolved_path, r.line) for r in rows])

    def delete_file(self, file: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM imports WHERE file=?", (file,))

    def imports_of(self, file: str) -> list[ImportRow]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM imports WHERE file=? ORDER BY line", (file,)).fetchall()
        return [ImportRow(*r) for r in rows]

    def importers_of(self, file: str) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT file FROM imports WHERE resolved_path=? ORDER BY file", (file,)).fetchall()
        return [r[0] for r in rows]

    def unresolved(self) -> list[ImportRow]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM imports WHERE resolved_path IS NULL").fetchall()
        return [ImportRow(*r) for r in rows]

    def reset(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM imports")

    def close(self) -> None:
        self._db.close()
