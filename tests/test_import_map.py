"""V2-2.7: flat import table (one level), resolved where possible, unresolved imports kept with resolved_path NULL."""
import pytest

pytest.importorskip("tree_sitter")
from rag.import_map import ImportMap, parse_imports  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    files = {
        "pkg/__init__.py": "from .models import Item\n",
        "pkg/models.py": "import os\nfrom dataclasses import dataclass\n",
        "pkg/service.py": "from pkg.models import Item\nfrom . import util\nfrom ..outside import x\nimport pkg.util as u\nimport requests\n",
        "pkg/util.py": "",
        "main.py": "from pkg.service import run\n",
        "src/app.ts": "import { a } from './lib/a';\nimport b from '@/lib/b';\nimport React from 'react';\nexport * from './lib/c';\n",
        "src/lib/a.ts": "export const a = 1;\n",
        "src/lib/b/index.ts": "export default 2;\n",
        "src/lib/c.tsx": "export const C = () => null;\n",
        "web/main.js": "const x = require('./x.js');\nimport('./lazy.mjs');\nimport y from '../missing.js';\n",
        "web/x.js": "module.exports = 1;\n",
        "web/lazy.mjs": "export default 1;\n",
        "tsconfig.json": '{\n  // comment\n  "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}\n}\n',
    }
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def resolved(rows):
    return {r.imported: r.resolved_path for r in rows}


def test_python_imports_absolute_relative_and_unresolved(repo):
    rows = parse_imports(repo, "pkg/service.py", (repo / "pkg/service.py").read_text())
    r = resolved(rows)
    assert r["pkg.models"] == "pkg/models.py"
    assert r[".util"] == "pkg/util.py"                 # from . import util
    assert r["pkg.util"] == "pkg/util.py"
    assert r["requests"] is None and r["..outside"] is None       # third-party / outside: never guessed
    assert {x.line for x in rows if x.imported == "requests"} == {5}


def test_python_package_init_and_stdlib(repo):
    assert resolved(parse_imports(repo, "pkg/__init__.py", "from .models import Item\n"))[".models"] == "pkg/models.py"
    r = resolved(parse_imports(repo, "pkg/models.py", (repo / "pkg/models.py").read_text()))
    assert r == {"os": None, "dataclasses": None}


def test_ts_relative_alias_index_and_reexport(repo):
    r = resolved(parse_imports(repo, "src/app.ts", (repo / "src/app.ts").read_text()))
    assert r["./lib/a"] == "src/lib/a.ts"
    assert r["@/lib/b"] == "src/lib/b/index.ts"        # tsconfig paths alias + index file
    assert r["./lib/c"] == "src/lib/c.tsx"              # export ... from
    assert r["react"] is None


def test_js_require_dynamic_import_and_missing(repo):
    rows = parse_imports(repo, "web/main.js", (repo / "web/main.js").read_text())
    r = resolved(rows)
    assert r["./x.js"] == "web/x.js" and r["./lazy.mjs"] == "web/lazy.mjs" and r["../missing.js"] is None
    assert {x.kind for x in rows} >= {"require", "import"}


def test_map_queries_one_level(repo, tmp_path):
    m = ImportMap(tmp_path / "imports.db")
    for rel in ("pkg/service.py", "main.py", "pkg/__init__.py"):
        m.set_file(rel, parse_imports(repo, rel, (repo / rel).read_text()))
    assert {r.resolved_path for r in m.imports_of("pkg/service.py") if r.resolved_path} == {"pkg/models.py", "pkg/util.py"}
    assert m.importers_of("pkg/models.py") == ["pkg/__init__.py", "pkg/service.py"]
    assert m.importers_of("pkg/service.py") == ["main.py"]           # direct only: main.py does not "import" models
    assert len(m.unresolved()) >= 2
    m.set_file("pkg/service.py", [])
    assert m.importers_of("pkg/models.py") == ["pkg/__init__.py"]
    m.delete_file("pkg/__init__.py")
    assert m.importers_of("pkg/models.py") == []
    m.close()


def test_syntax_errors_do_not_raise(repo):
    assert parse_imports(repo, "bad.py", "import (\n") == []
    assert isinstance(parse_imports(repo, "bad.ts", "import { from;\n"), list)
