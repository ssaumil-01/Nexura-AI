"""V1-7.5: nothing from V2/V3 (plan section 2) may appear in V1."""
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


def test_rag_files_stay_empty():
    for name in ("retriever.py", "graph_rag.py"):
        assert (ROOT / "rag" / name).stat().st_size == 0, f"rag/{name} must remain empty (V2/V3)"
    assert sorted(p.name for p in (ROOT / "rag").iterdir() if p.is_file() and p.suffix == ".py") == ["graph_rag.py", "retriever.py"]


def test_github_tool_untouched_and_unbound():
    r = subprocess.run(["git", "cat-file", "-t", BASE_COMMIT], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("base commit not available in this clone")
    diff = subprocess.run(["git", "diff", BASE_COMMIT, "--", "tools/github_tool.py"], cwd=ROOT, capture_output=True, text=True)
    assert diff.stdout.strip() == "", "tools/github_tool.py must not change in V1"
    for p in py_files():
        if p.name == "github_tool.py" or p.name == "test_scope_audit.py":
            continue
        assert "github_tool" not in p.read_text(encoding="utf-8"), f"{p.name} references github_tool"


def test_no_docker_or_ci_cd_for_generated_projects():
    names = {p.name.lower() for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS)}
    assert not [n for n in names if n.startswith("dockerfile") or n.startswith("docker-compose") or n.endswith(".dockerfile")]


BANNED = ["faiss", "chroma", "chromadb", "sentence-transformers", "sentence_transformers", "tree-sitter", "tree_sitter", "networkx", "neo4j",
          "gitpython", "pygithub", "docker", "pinecone", "weaviate", "qdrant", "llama-index", "llama_index"]


def test_no_vector_graph_github_or_docker_dependencies():
    for req in ROOT.glob("requirements*.txt"):
        for line in req.read_text(encoding="utf-8").splitlines():
            name = re.split(r"[<>=!\[ ;]", line.strip().lower())[0]
            assert name not in BANNED, f"{req.name}: {name} is out of scope for V1"


def test_no_imports_of_out_of_scope_libraries():
    banned = {b.replace("-", "_") for b in BANNED} | {"git", "github"}
    for p in py_files():
        if p.name == "github_tool.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            mods = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
            for m in mods:
                assert m.split(".")[0].lower() not in banned, f"{p.relative_to(ROOT)} imports {m}"
