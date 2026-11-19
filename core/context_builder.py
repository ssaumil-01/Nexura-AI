"""Builds the workspace context for the Coder: budgeted, prioritized, ignore-aware. build_context is the V1 builder;
build_task_context picks it or the V2 retrieval builder (RETRIEVAL_MODE).

Priority: 1) files named in the task  2) files changed this run  3) everything else as path + first lines.
"""
import os
from pathlib import Path

import config
from core.safety import contains_secret, is_ignored, is_protected, load_ignore_patterns, redact_secrets

MAX_FILES_SCANNED = 2000
FULL_FILE_CAP = 12_000   # chars of one file shown in full
HEAD_LINES = 30          # lines shown for lower-priority files
TREE_SHARE = 0.2         # at most this share of the budget goes to the tree


def _list_files(root: Path, patterns: list[str]) -> list[str]:
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        dirs[:] = sorted(d for d in dirs if not is_ignored(d if rel_dir == "." else f"{rel_dir}/{d}", patterns))
        for f in sorted(files):
            rel = f if rel_dir == "." else f"{rel_dir}/{f}"
            if not is_ignored(rel, patterns):
                out.append(rel)
        if len(out) >= MAX_FILES_SCANNED:
            break
    return out


def _read(root: Path, rel: str) -> str | None:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def build_context(task: dict, root: Path | str, changed_files: list[str] | None = None,
                  budget: int | None = None, redact=None) -> tuple[str, dict]:
    """Returns (context_text, breakdown). `redact(text) -> text` is applied to every file body (secret scrubbing)."""
    root = Path(root)
    budget = budget or config.CONTEXT_CHAR_BUDGET
    redact = redact or (lambda t: t)
    if not root.exists():
        return "Workspace is currently empty.", {"files": 0}
    files = _list_files(root, load_ignore_patterns(root))
    if not files:
        return "Workspace is currently empty.", {"files": 0}

    changed = [f for f in dict.fromkeys(changed_files or []) if f in files]
    text = f"{task.get('title', '')}\n{task.get('description', '')}".lower()
    named = [f for f in files if f.lower() in text or (len(os.path.basename(f)) > 3 and os.path.basename(f).lower() in text)]

    tree_cap = int(budget * TREE_SHARE)
    tree = "\n".join(files)
    tree_note = ""
    if len(tree) > tree_cap:
        tree, tree_note = tree[:tree_cap], f"\n...[tree truncated: {len(files)} files total]"
    parts = ["========================\nFILES\n========================\n" + tree + tree_note]
    used = len(parts[0])
    shown = {"named": [], "changed": [], "head": []}
    omitted = 0

    def add(rel: str, group: str, full: bool) -> None:
        nonlocal used, omitted
        body = _read(root, rel)
        if body is None:
            return
        body = redact(body)
        if full:
            body = body if len(body) <= FULL_FILE_CAP else body[:FULL_FILE_CAP] + "\n...[file truncated]"
        else:
            lines = body.splitlines()
            body = "\n".join(lines[:HEAD_LINES]) + (f"\n...[{len(lines) - HEAD_LINES} more lines]" if len(lines) > HEAD_LINES else "")
        block = f"\n--- {rel} ---\n```\n{body}\n```"
        if used + len(block) > budget:
            omitted += 1
            return
        parts.append(block)
        used += len(block)
        shown[group].append(rel)

    seen = set()
    for group, names, full in (("named", named, True), ("changed", changed, True), ("head", files, False)):
        for rel in names:
            if rel not in seen:
                seen.add(rel)
                add(rel, group, full)

    breakdown = {"files": len(files), "chars": used, "budget": budget, "omitted_for_budget": omitted,
                 **{k: len(v) for k, v in shown.items()}}
    return "\n".join(parts), breakdown


# ─── V2: retrieval-based context (RETRIEVAL_MODE=v2), with the V1 builder above as the fallback ───

V2_TREE_SHARE = 0.1          # file list: at most this share of the budget
V2_IMPORT_SHARE = 0.1        # import hints: at most this share
REFERENCE = "reference only: use read_file before editing"


def build_task_context(task: dict, ctx, changed_files: list[str] | None = None) -> tuple[str, dict]:
    """Context for the Coder. RETRIEVAL_MODE=v1 is exactly the V1 builder. In v2, a missing/not-ready/broken index falls back
    to the V1 builder and logs `retrieval_fallback` with the reason; a run never fails because of retrieval."""
    if config.RETRIEVAL_MODE != "v2":
        return build_context(task, ctx.root, changed_files, redact=redact_secrets)
    ix = ctx.indexer
    reason = "no index for this workspace" if ix is None else None
    if reason is None:
        try:
            reason = None if ix.ready() else "index not ready (missing, stale or still building)"
        except Exception as e:
            reason = f"index unavailable: {type(e).__name__}: {e}"
    if reason is None:
        try:
            return build_context_v2(task, ctx, changed_files or [], ix)
        except Exception as e:
            reason = f"retrieval error: {type(e).__name__}: {e}"
    reason = redact_secrets(str(reason))[:300]
    ctx.emit("retrieval_fallback", task.get("step_id"), reason=reason)
    text, breakdown = build_context(task, ctx.root, changed_files, redact=redact_secrets)
    return text, {**breakdown, "mode": "v1", "fallback": reason}


def _named_files(task: dict, files: list[str]) -> list[str]:
    text = f"{task.get('title', '')}\n{task.get('description', '')}".lower()
    return [f for f in files if f.lower() in text or (len(os.path.basename(f)) > 3 and os.path.basename(f).lower() in text)]


def _safe_text(root: Path, rel: str, patterns: list[str]) -> str | None:
    """Current file text if it may be shown to a model right now (the index can be stale): not ignored, not protected,
    no secret-looking content."""
    if is_ignored(rel, patterns) or is_protected(rel, config.PROTECTED_PATHS):
        return None
    body = _read(root, rel)
    if body is None or contains_secret(body):
        return None
    return body


def safe_snippet(root: Path, chunk, patterns: list[str], cache: dict) -> str | None:
    """A retrieved chunk's text, redacted, if it may be shown right now: its file is still allowed (not ignored/protected,
    no secrets) and the chunk still matches the file (not stale). None otherwise. `cache` holds file texts per call."""
    if chunk.path not in cache:
        cache[chunk.path] = _safe_text(root, chunk.path, patterns)
    body = cache[chunk.path]
    if body is None or chunk.text not in body.replace("\r\n", "\n"):
        return None
    return redact_secrets(chunk.text)


def build_context_v2(task: dict, ctx, changed_files: list[str], ix, budget: int | None = None) -> tuple[str, dict]:
    root = Path(ctx.root)
    budget = budget or config.RETRIEVAL_CONTEXT_BUDGET
    patterns = load_ignore_patterns(root)
    files = _list_files(root, patterns)
    named = list(dict.fromkeys(_named_files(task, files) + [f for f in changed_files if f in files]))

    stale = ix.stale_paths(named)                 # freshness: the task's own files are re-indexed on the spot
    if stale:
        ix.update(stale)
        ctx.emit("index_refreshed", task.get("step_id"), files=stale[:20])

    query = f"{task.get('title', '')}\n{task.get('description', '')}".strip()
    results = ix.retriever().retrieve(query, k=config.TOP_K * 2, named_paths=named, recent_paths=changed_files)

    parts, used, sections = [], 0, {"files": 0, "named": 0, "retrieved": 0, "imports": 0}
    dropped_budget = dropped_unsafe = 0

    def put(block: str, section: str) -> bool:
        nonlocal used
        if used + len(block) > budget:
            return False
        parts.append(block)
        used += len(block)
        sections[section] += len(block)
        return True

    tree = "\n".join(files)
    cap = int(budget * V2_TREE_SHARE)
    if len(tree) > cap:
        tree = tree[:cap] + f"\n...[{len(files)} files total]"
    put("========================\nFILES\n========================\n" + tree, "files")

    shown_full: list[str] = []
    for rel in named:
        body = _safe_text(root, rel, patterns)
        if body is None:
            dropped_unsafe += 1
            continue
        body = redact_secrets(body)
        body = body if len(body) <= FULL_FILE_CAP else body[:FULL_FILE_CAP] + "\n...[file truncated]"
        if put(f"\n--- {rel} ---\n```\n{body}\n```", "named"):
            shown_full.append(rel)
        else:
            dropped_budget += 1

    log, included = [], []
    current: dict[str, str | None] = {}
    if results:
        put(f"\n========================\nRELEVANT CODE (retrieved; {REFERENCE})\n========================", "retrieved")
    for r in results:
        c = r.chunk
        entry = {"path": c.path, "lines": f"{c.start_line}-{c.end_line}", "symbol": c.symbol, "score": r.score,
                 "sources": r.sources, "reason": r.reason[:200], "included": False}
        log.append(entry)
        if c.path in shown_full:
            entry["reason"] += "; already shown in full"
            continue
        snippet = safe_snippet(root, c, patterns, current)
        if snippet is None:
            dropped_unsafe += 1
            entry["reason"] += "; dropped (stale or no longer allowed)"
            continue
        label = f" ({c.symbol})" if c.symbol and c.symbol != "<module>" else ""
        if put(f"\n--- {c.path} lines {c.start_line}-{c.end_line}{label} [{REFERENCE}] ---\n```\n{snippet}\n```", "retrieved"):
            entry["included"] = True
            included.append(c.path)
        else:
            dropped_budget += 1

    hints = []
    for rel in dict.fromkeys(shown_full + included):
        imports = sorted({r.resolved_path for r in ix.imports.imports_of(rel) if r.resolved_path})
        importers = ix.imports.importers_of(rel)
        if imports or importers:
            hints.append(f"{rel}: imports {', '.join(imports) or '-'}; imported by {', '.join(importers) or '-'}")
    if hints:
        block = "\n========================\nIMPORT HINTS (one level)\n========================\n" + "\n".join(hints)
        cap = int(budget * V2_IMPORT_SHARE)
        put(block if len(block) <= cap else block[:cap] + "\n...", "imports")

    ctx.emit("retrieval", task.get("step_id"), query=redact_secrets(query)[:200], results=log[:20],
             included=len(included), dropped_for_budget=dropped_budget, dropped_unsafe=dropped_unsafe)
    breakdown = {"mode": "v2", "files": len(files), "named": len(shown_full), "retrieved": len(included),
                 "dropped_for_budget": dropped_budget, "dropped_unsafe": dropped_unsafe, "import_hints": len(hints),
                 "chars": used, "budget": budget, "sections": sections}
    return "\n".join(parts), breakdown
