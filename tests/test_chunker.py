"""V2-2.2/2.3: tree-sitter chunks for Python and JS/TS, line windows for everything else."""
import pytest

pytest.importorskip("tree_sitter")
from rag.chunker import CHUNKER_VERSION, chunk_file, language_of  # noqa: E402

PY = '''"""Module doc."""
import os

LIMIT = 3


def top(a, b):
    """Add two numbers."""
    return a + b


@decorator
def decorated():
    return 1


class Store:
    """Keeps things."""
    kind = "s"

    def add(self, x):
        return x

    async def fetch(self):
        return None


if __name__ == "__main__":
    print(top(1, 2))
'''

JS = '''import { x } from "./x.js";

export function render(todo) {
  return todo.title;
}

const helper = (a) => a * 2;

export class List {
  add(item) {
    return item;
  }

  static empty() {
    return new List();
  }
}

module.exports = { helper };
'''

TS = '''export interface Todo {
  id: number;
  title: string;
}

type Id = number;

export function make(title: string): Todo {
  return { id: 1, title };
}

export default class Repo<T> {
  private items: T[] = [];

  get(i: number): T {
    return this.items[i];
  }
}

enum Color { Red, Green }
'''


def by_symbol(chunks):
    return {c.symbol: c for c in chunks}


def test_language_detection():
    assert [language_of(p) for p in ("a.py", "b.js", "c.mjs", "d.jsx", "e.ts", "f.tsx", "g.md", "h.json")] == \
        ["python", "javascript", "javascript", "javascript", "typescript", "tsx", "text", "text"]


def test_python_functions_classes_methods_and_module_code():
    chunks = chunk_file("pkg/store.py", PY)
    s = by_symbol(chunks)
    assert {"top", "decorated", "Store", "Store.add", "Store.fetch"} <= set(s)
    assert s["top"].kind == "function" and s["top"].start_line == 7 and s["top"].end_line == 9
    assert s["decorated"].text.startswith("@decorator")              # decorator stays with its function
    assert s["Store"].kind == "class" and "kind = \"s\"" in s["Store"].text and "def add" not in s["Store"].text
    assert s["Store.add"].kind == "method" and s["Store.fetch"].kind == "method"
    module = [c for c in chunks if c.kind == "module"]
    assert module and any("import os" in c.text for c in module) and any("__main__" in c.text for c in module)
    assert all(c.language == "python" and c.path == "pkg/store.py" for c in chunks)


def test_every_line_with_code_is_covered_exactly_once_for_python():
    chunks = chunk_file("a.py", PY)
    covered = [n for c in chunks for n in range(c.start_line, c.end_line + 1)]
    assert len(covered) == len(set(covered))
    code_lines = {i + 1 for i, line in enumerate(PY.splitlines()) if line.strip()}
    assert code_lines <= set(covered)


def test_javascript_functions_arrows_classes_methods():
    s = by_symbol(chunk_file("src/todo.js", JS))
    assert s["render"].kind == "function" and s["render"].text.startswith("export function render")
    assert s["helper"].kind == "function"
    assert s["List"].kind == "class" and s["List.add"].kind == "method" and s["List.empty"].kind == "method"


def test_typescript_interfaces_types_enums_and_generic_classes():
    s = by_symbol(chunk_file("src/repo.ts", TS))
    assert s["Todo"].kind == "block" and s["Id"].kind == "block" and s["Color"].kind == "block"
    assert s["make"].kind == "function" and s["Repo"].kind == "class" and s["Repo.get"].kind == "method"
    assert all(c.language == "typescript" for c in s.values())


def test_tsx_parses():
    s = by_symbol(chunk_file("src/App.tsx", "export function App() {\n  return <div>hi</div>;\n}\n"))
    assert s["App"].kind == "function" and s["App"].language == "tsx"


def test_other_text_files_use_line_windows():
    text = "\n".join(f"line {i}" for i in range(1, 201)) + "\n"
    chunks = chunk_file("docs/notes.md", text, max_lines=80)
    assert all(c.kind == "block" and c.language == "text" for c in chunks)
    assert chunks[0].start_line == 1 and chunks[-1].end_line == 200 and len(chunks) >= 3
    assert all(c.end_line - c.start_line + 1 <= 80 for c in chunks)
    assert all(chunks[i].start_line <= chunks[i - 1].end_line + 1 for i in range(1, len(chunks)))   # no gaps


def test_syntax_error_falls_back_to_line_windows():
    chunks = chunk_file("broken.py", "def ok():\n    return 1\n\ndef broken(:\n    pass\n")
    assert chunks and all(c.kind == "block" for c in chunks)
    assert "".join(c.text for c in chunks).count("def broken") >= 1


def test_oversized_function_is_split_with_overlap():
    body = "\n".join(f"    x{i} = {i}" for i in range(300))
    chunks = chunk_file("big.py", f"def big():\n{body}\n    return 0\n", max_lines=80)
    parts = [c for c in chunks if c.symbol.startswith("big")]
    assert len(parts) >= 4 and all(c.end_line - c.start_line + 1 <= 80 for c in parts)
    assert parts[0].symbol == "big#1" and parts[1].start_line < parts[0].end_line + 1   # overlap
    assert parts[-1].end_line == 302


def test_ids_are_stable_when_unrelated_code_moves():
    a = by_symbol(chunk_file("m.py", PY))
    b = by_symbol(chunk_file("m.py", "# a new comment line\n\n" + PY))
    assert a["top"].id == b["top"].id and a["top"].start_line != b["top"].start_line
    c = by_symbol(chunk_file("m.py", PY.replace("return a + b", "return a - b")))
    assert c["top"].id != a["top"].id and c["Store"].id == a["Store"].id
    assert by_symbol(chunk_file("other.py", PY))["top"].id != a["top"].id


def test_ids_are_unique_within_a_file():
    chunks = chunk_file("dup.py", "def f():\n    return 1\n\n\ndef f():\n    return 1\n")
    assert len({c.id for c in chunks}) == len(chunks)


def test_enriched_text_starts_with_path_and_symbol():
    c = by_symbol(chunk_file("pkg/store.py", PY))["Store.add"]
    assert c.enriched_text.splitlines()[0] == "File: pkg/store.py" and "Store.add" in c.enriched_text and c.text in c.enriched_text


def test_crlf_and_bom_are_handled():
    text = "﻿" + PY.replace("\n", "\r\n")
    s = by_symbol(chunk_file("w.py", text))
    assert s["top"].start_line == 7 and "\r" not in s["top"].text


def test_empty_file_gives_no_chunks():
    assert chunk_file("empty.py", "") == [] and chunk_file("blank.md", "\n\n  \n") == []


def test_version_is_a_string():
    assert isinstance(CHUNKER_VERSION, str) and CHUNKER_VERSION
