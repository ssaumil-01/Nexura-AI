"""Scope audit. V2 adds local code indexing (tree-sitter, chromadb, fastembed). Docker, graph-RAG, GitHub integration and cloud
embeddings (V3 / out of scope) must still not appear."""
import ast
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE_COMMIT = "6483046"   # main before the V1 work started
SKIP_DIRS = {".venv", "node_modules", "App", "extension", "ui", "workspace", "memory", ".git", ".vscode-test", "out-test"}


def py_files():
    for p in ROOT.rglob("*.py"):
        if not (set(p.relative_to(ROOT).parts) & SKIP_DIRS):
            yield p


V2_RAG_MODULES = {"chunker.py", "embedder.py", "vector_store.py", "lexical_index.py", "import_map.py", "indexer.py", "retriever.py",
                  "graph_rag.py", "__init__.py"}


def test_graph_rag_stays_empty_and_rag_has_only_planned_modules():
    assert (ROOT / "rag" / "graph_rag.py").stat().st_size == 0, "rag/graph_rag.py must remain empty (V3)"
    present = {p.name for p in (ROOT / "rag").iterdir() if p.is_file() and p.suffix == ".py"}
    assert present <= V2_RAG_MODULES, f"unplanned modules in rag/: {present - V2_RAG_MODULES}"


def test_github_tool_untouched_and_unbound():
    r = subprocess.run(["git", "cat-file", "-t", BASE_COMMIT], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("base commit not available in this clone")
    diff = subprocess.run(["git", "diff", BASE_COMMIT, "--", "tools/github_tool.py"], cwd=ROOT, capture_output=True, text=True)
    assert diff.stdout.strip() == "", "tools/github_tool.py must not change"
    for p in py_files():
        if p.name == "github_tool.py" or p.name == "test_scope_audit.py":
            continue
        assert "github_tool" not in p.read_text(encoding="utf-8"), f"{p.name} references github_tool"


def test_no_docker_or_ci_cd_for_generated_projects():
    names = {p.name.lower() for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS)}
    assert not [n for n in names if n.startswith("dockerfile") or n.startswith("docker-compose") or n.endswith(".dockerfile")]


BANNED = ["faiss", "networkx", "neo4j", "kuzu", "gitpython", "pygithub", "docker", "pinecone", "weaviate", "qdrant", "llama-index",
          "llama_index", "openai", "cohere", "voyageai", "sentence-transformers", "sentence_transformers", "torch"]
# V2 packages, each importable only from the module that owns it (nothing else may depend on Chroma directly, etc.).
OWNERS = {"chromadb": {"vector_store.py"}, "fastembed": {"embedder.py"}, "tree_sitter": {"chunker.py", "import_map.py"},
          "tree_sitter_python": {"chunker.py", "import_map.py"}, "tree_sitter_javascript": {"chunker.py", "import_map.py"},
          "tree_sitter_typescript": {"chunker.py", "import_map.py"}}


def test_no_vector_graph_github_or_docker_dependencies():
    for req in ROOT.glob("requirements*.txt"):
        for line in req.read_text(encoding="utf-8").splitlines():
            name = re.split(r"[<>=!\[ ;]", line.strip().lower())[0]
            assert name not in BANNED, f"{req.name}: {name} is out of scope"


def test_no_imports_of_out_of_scope_libraries():
    banned = {b.replace("-", "_") for b in BANNED} | {"git", "github"}
    for p in py_files():
        if p.name == "github_tool.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            mods = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
            for m in mods:
                assert m.split(".")[0].lower() not in banned, f"{p.relative_to(ROOT)} imports {m}"


def test_v2_libraries_are_imported_only_by_their_owner_module():
    for p in py_files():
        if p.parts[-2:-1] == ("tests",) or "evals" in p.relative_to(ROOT).parts:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            mods = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
            for m in mods:
                top = m.split(".")[0]
                if top in OWNERS:
                    assert p.name in OWNERS[top] and p.parent.name == "rag", f"{p.relative_to(ROOT)} must not import {top}"
