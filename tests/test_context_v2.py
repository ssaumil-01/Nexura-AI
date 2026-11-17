"""V2-3.2..3.6: context builder v2 (named files, retrieved chunks, import hints, budget), the V1 fallback, read-time safety,
freshness, retrieval logging, and the guarantee that RETRIEVAL_MODE=v1 is exactly V1."""
import shutil
from pathlib import Path

import pytest

pytest.importorskip("chromadb")
import config  # noqa: E402
from core import context_builder as cb  # noqa: E402
from core.schemas import ProjectContext  # noqa: E402
from core.safety import redact_secrets  # noqa: E402
from rag.embedder import FakeEmbedder  # noqa: E402
from rag.indexer import Indexer  # noqa: E402

FIX = Path(__file__).resolve().parent.parent / "evals" / "fixtures"
TASK = {"step_id": 1, "title": "Rename line_total", "description": "Rename line_total to item_total and update reports.py and every caller."}


class EventSink:
    def __init__(self, events):
        self.events = events

    def add_event(self, run_id, type, task_id=None, **data):
        self.events.append((type, data))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX_DIR", tmp_path / "index")
    root = tmp_path / "inv"
    shutil.copytree(FIX / "inventory_pkg", root, ignore=shutil.ignore_patterns("__pycache__"))
    ix = Indexer(root, "inv", FakeEmbedder(dim=64))
    ix.open()
    ix.build()
    events = []
    ctx = ProjectContext(run_id="r", root=root, indexer=ix, state_manager=EventSink(events))
    yield root, ix, ctx, events
    ix.close()


def v2(monkeypatch):
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v2")


# ─── v1 stays exactly V1 ────────────────────────────────────

class Explodes:
    def __getattr__(self, name):
        raise AssertionError(f"v1 mode touched the index ({name})")


def test_v1_mode_is_byte_for_byte_the_v1_builder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RETRIEVAL_MODE", "v1")
    root = tmp_path / "inv"
    shutil.copytree(FIX / "inventory_pkg", root, ignore=shutil.ignore_patterns("__pycache__"))
    ctx = ProjectContext(run_id="r", root=root, indexer=Explodes())
    for changed in ([], ["main.py"]):
        assert cb.build_task_context(TASK, ctx, changed) == cb.build_context(TASK, root, changed, redact=redact_secrets)


# ─── v2 contents ────────────────────────────────────────────

def test_v2_has_named_files_retrieved_chunks_and_import_hints(setup, monkeypatch):
    root, ix, ctx, events = setup
    v2(monkeypatch)
    text, bd = cb.build_task_context(TASK, ctx, [])
    assert bd["mode"] == "v2" and bd["named"] >= 1 and bd["retrieved"] >= 1
    assert "--- inventory/reports.py ---" in text                                 # named in the task: full file
    assert "inventory/pricing.py lines" in text and "reference only" in text       # retrieved chunk, labelled
    assert "IMPORT HINTS" in text
    hint = next(line for line in text.splitlines() if line.startswith("inventory/pricing.py: imports"))
    assert "imported by" in hint and "inventory/reports.py" in hint
    assert len(text) <= config.RETRIEVAL_CONTEXT_BUDGET
    assert set(bd["sections"]) == {"files", "named", "retrieved", "imports"}


def test_v2_respects_the_budget(setup, monkeypatch):
    root, ix, ctx, events = setup
    v2(monkeypatch)
    monkeypatch.setattr(config, "RETRIEVAL_CONTEXT_BUDGET", 1500)
    text, bd = cb.build_task_context(TASK, ctx, [])
    assert len(text) <= 1500 and bd["dropped_for_budget"] >= 1


def test_retrieval_is_logged(setup, monkeypatch):
    root, ix, ctx, events = setup
    v2(monkeypatch)
    cb.build_task_context(TASK, ctx, [])
    logged = [d for t, d in events if t == "retrieval"]
    assert logged and logged[0]["query"].startswith("Rename line_total")
    r0 = logged[0]["results"][0]
    assert {"path", "lines", "symbol", "score", "sources", "reason", "included"} <= set(r0)


def test_retrieved_snippets_do_not_count_as_read(setup, monkeypatch):
    root, ix, ctx, events = setup
    v2(monkeypatch)
    cb.build_task_context(TASK, ctx, [])
    assert ctx.files_read == set()


# ─── fallback ───────────────────────────────────────────────

@pytest.mark.parametrize("problem", ["none", "not_ready", "raises"])
def test_fallback_to_v1_with_a_logged_reason(setup, monkeypatch, problem):
    root, ix, ctx, events = setup
    v2(monkeypatch)
    if problem == "none":
        ctx.indexer = None
    elif problem == "not_ready":
        ix.manifest["complete"] = False
    else:
        monkeypatch.setattr(ix, "retriever", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    text, bd = cb.build_task_context(TASK, ctx, [])
    v1_text, _ = cb.build_context(TASK, root, [], redact=redact_secrets)
    assert text == v1_text and bd["fallback"]
    reasons = [d["reason"] for t, d in events if t == "retrieval_fallback"]
    assert reasons and {"none": "no index", "not_ready": "not ready", "raises": "boom"}[problem] in reasons[0]


# ─── read-time safety and freshness ─────────────────────────

def test_stale_secret_and_newly_ignored_content_never_reach_the_prompt(setup, monkeypatch):
    root, ix, ctx, events = setup
    v2(monkeypatch)
    secret = "AIza" + "Q" * 35
    p = root / "inventory" / "pricing.py"
    p.write_text(p.read_text() + f'\nKEY = "{secret}"\n', encoding="utf-8")          # changed after indexing, not re-indexed
    (root / ".gitignore").write_text("inventory/storage.py\n", encoding="utf-8")       # ignored after indexing
    text, bd = cb.build_task_context(TASK, ctx, [])
    assert secret not in text and "def load_items" not in text
    assert bd["dropped_unsafe"] >= 1


def test_named_files_are_refreshed_before_building(setup, monkeypatch):
    root, ix, ctx, events = setup
    v2(monkeypatch)
    p = root / "inventory" / "reports.py"
    p.write_text(p.read_text() + "\n\ndef fresh_function():\n    return 1\n", encoding="utf-8")
    cb.build_task_context(TASK, ctx, [])
    assert "fresh_function" in {c.symbol for c in ix.lexical.chunks_of("inventory/reports.py")}
    assert any(t == "index_refreshed" for t, _ in events)
