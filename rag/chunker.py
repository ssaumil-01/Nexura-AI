"""Split source files into meaningful chunks: one per top-level function/class and per method (Python, JavaScript, TypeScript,
via tree-sitter), module-level code as "module" chunks, and line windows for everything else or when parsing fails."""
import hashlib
import os
from dataclasses import dataclass, field

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

import config

CHUNKER_VERSION = "1"
OVERLAP = 10   # lines shared by consecutive windows of a split chunk

LANGUAGES = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
             ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx"}
_GRAMMARS = {"python": tree_sitter_python.language, "javascript": tree_sitter_javascript.language,
             "typescript": tree_sitter_typescript.language_typescript, "tsx": tree_sitter_typescript.language_tsx}
_parsers: dict[str, Parser] = {}


@dataclass
class Chunk:
    id: str
    path: str
    language: str
    kind: str            # module | class | function | method | block
    symbol: str
    start_line: int      # 1-based, inclusive
    end_line: int
    text: str
    content_hash: str
    enriched_text: str = field(repr=False, default="")


def language_of(path: str) -> str:
    return LANGUAGES.get(os.path.splitext(path)[1].lower(), "text")


def _parser(lang: str) -> Parser:
    if lang not in _parsers:
        _parsers[lang] = Parser(Language(_GRAMMARS[lang]()))
    return _parsers[lang]


def _name(node, src: bytes) -> str | None:
    n = node.child_by_field_name("name")
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace") if n is not None else None


# ─── per-language definition finders: yield (kind, symbol, start_row, end_row) with 0-based rows ───

def _python_defs(root, src):
    for node in root.children:
        outer = node
        if node.type == "decorated_definition":
            node = node.child_by_field_name("definition") or node.children[-1]
        if node.type == "function_definition":
            yield "function", _name(node, src), outer.start_point[0], outer.end_point[0]
        elif node.type == "class_definition":
            cname = _name(node, src)
            methods = []
            body = node.child_by_field_name("body")
            for m in (body.children if body is not None else []):
                mouter = m
                if m.type == "decorated_definition":
                    m = m.child_by_field_name("definition") or m.children[-1]
                if m.type == "function_definition":
                    methods.append(("method", f"{cname}.{_name(m, src)}", mouter.start_point[0], mouter.end_point[0]))
            head_end = (methods[0][2] - 1) if methods else outer.end_point[0]
            yield "class", cname, outer.start_point[0], head_end
            yield from methods


_JS_FUNCS = {"function_declaration", "generator_function_declaration"}
_JS_CLASSES = {"class_declaration", "abstract_class_declaration"}
_TS_BLOCKS = {"interface_declaration", "type_alias_declaration", "enum_declaration"}


def _js_defs(root, src):
    for node in root.children:
        outer = node
        if node.type == "export_statement":
            inner = node.child_by_field_name("declaration")
            if inner is None:
                inner = next((c for c in node.children if c.type in _JS_FUNCS | _JS_CLASSES | _TS_BLOCKS
                              | {"lexical_declaration", "variable_declaration"}), None)
            if inner is None:
                continue
            node = inner
        if node.type in _JS_FUNCS:
            yield "function", _name(node, src), outer.start_point[0], outer.end_point[0]
        elif node.type in _TS_BLOCKS:
            yield "block", _name(node, src), outer.start_point[0], outer.end_point[0]
        elif node.type in _JS_CLASSES:
            cname = _name(node, src) or "default"
            methods = []
            body = node.child_by_field_name("body")
            for m in (body.children if body is not None else []):
                if m.type == "method_definition":
                    methods.append(("method", f"{cname}.{_name(m, src)}", m.start_point[0], m.end_point[0]))
            head_end = (methods[0][2] - 1) if methods else outer.end_point[0]
            yield "class", cname, outer.start_point[0], head_end
            yield from methods
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for d in node.children:
                if d.type == "variable_declarator":
                    value = d.child_by_field_name("value")
                    if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                        yield "function", _name(d, src), outer.start_point[0], outer.end_point[0]
                        break


# ─── assembly ───────────────────────────────────────────────

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _windows(start: int, end: int, max_lines: int) -> list[tuple[int, int]]:
    """Split [start, end] (inclusive rows) into windows of at most max_lines with OVERLAP lines shared."""
    if end - start + 1 <= max_lines:
        return [(start, end)]
    step = max(1, max_lines - OVERLAP)
    out, s = [], start
    while True:
        e = min(s + max_lines - 1, end)
        out.append((s, e))
        if e >= end:
            return out
        s += step


def _make(path, lang, kind, symbol, rows, lines, seen) -> Chunk | None:
    s, e = rows
    text = "\n".join(lines[s:e + 1])
    if not text.strip():
        return None
    h = _hash(text)
    base = f"{path}\0{symbol}\0{h}"
    n = seen.get(base, 0)
    seen[base] = n + 1
    cid = hashlib.sha1(f"{base}\0{n}".encode("utf-8")).hexdigest()[:20]
    header = f"File: {path}\n" + (f"Symbol: {symbol} ({kind})\n" if symbol else f"Lines {s + 1}-{e + 1}\n")
    return Chunk(cid, path, lang, kind, symbol, s + 1, e + 1, text, h, header + text)


def chunk_file(path: str, text: str, max_lines: int | None = None, max_chars: int | None = None) -> list[Chunk]:
    """Chunks for one file. `path` is workspace-relative with forward slashes."""
    max_lines = max_lines or config.CHUNK_MAX_LINES
    max_chars = max_chars or config.CHUNK_MAX_TOKENS * 4
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not any(line.strip() for line in lines):
        return []
    lang = language_of(path)
    defs = None
    if lang != "text":
        try:
            tree = _parser(lang).parse(text.encode("utf-8"))
            if not tree.root_node.has_error:
                finder = _python_defs if lang == "python" else _js_defs
                defs = [d for d in finder(tree.root_node, text.encode("utf-8")) if d[1]]
        except Exception:   # parser failure: fall back to line windows
            defs = None
    seen: dict = {}
    out: list[Chunk] = []

    def emit(kind, symbol, s, e):
        size = sum(len(x) + 1 for x in lines[s:e + 1])
        windows = _windows(s, e, max_lines) if (e - s + 1 > max_lines or size > max_chars) else [(s, e)]
        if len(windows) > 1 and size > max_chars and e - s + 1 <= max_lines:   # long lines: split by size instead
            per = max(1, (e - s + 1) * max_chars // size)
            windows = _windows(s, e, max(per, OVERLAP + 1))
        for i, w in enumerate(windows, 1):
            name = f"{symbol}#{i}" if symbol and len(windows) > 1 else symbol
            c = _make(path, lang, kind, name, w, lines, seen)
            if c:
                out.append(c)

    if defs is None:
        for s, e in _windows(0, len(lines) - 1, max_lines):
            c = _make(path, lang, "block", "", (s, e), lines, seen)
            if c:
                out.append(c)
        return out

    covered = [False] * len(lines)
    for kind, symbol, s, e in defs:
        for i in range(s, e + 1):
            covered[i] = True
        emit(kind, symbol, s, e)
    # module-level code: contiguous runs of uncovered lines
    i = 0
    while i < len(lines):
        if covered[i] or not lines[i].strip():
            i += 1
            continue
        j = i
        while j + 1 < len(lines) and not covered[j + 1]:
            j += 1
        while j > i and not lines[j].strip():
            j -= 1
        emit("module", "<module>", i, j)
        i = j + 1
    out.sort(key=lambda c: c.start_line)
    return out
