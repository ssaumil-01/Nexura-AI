"""Builds the workspace context for the Coder: budgeted, prioritized, ignore-aware. (No retrieval/embeddings: that is V2.)

Priority: 1) files named in the task  2) files changed this run  3) everything else as path + first lines.
"""
import os
from pathlib import Path

import config
from core.safety import is_ignored, load_ignore_patterns

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
