"""Approval levels and the risk gate: what would an action change (diff preview) and does the user have to approve it?"""
import difflib
import fnmatch
import os
import shutil
from pathlib import Path

import config
from core.safety import SafetyError, check_writable, resolve_in_root
from core.schemas import Action, ProjectContext

MAX_PREVIEW_CHARS = 100_000

MANIFESTS = ["package.json", "requirements*.txt", "pyproject.toml", "Pipfile", "setup.py", "setup.cfg", "Cargo.toml",
             "go.mod", "pom.xml", "build.gradle", "composer.json", "Gemfile"]
CONFIG_FILES = ["tsconfig*.json", "*.config.js", "*.config.ts", "*.config.cjs", "*.config.mjs", "*.ini", "*.toml", "*.cfg",
                "*.yml", "*.yaml", ".eslintrc*", ".prettierrc*", "Dockerfile*", "docker-compose*", "Makefile", ".gitignore",
                ".nexuraignore", ".npmrc", "*.conf"]

# kinds: "edit" (any change), "risky" (asked at ask_risky and above), "hard" (asked at EVERY level, incl. auto_within_limits)


def _name_matches(rel: str, patterns: list[str]) -> bool:
    base = os.path.basename(rel.replace("\\", "/")).lower()
    return any(fnmatch.fnmatch(base, p.lower()) for p in patterns)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _diff(rel: str, before: str | None, after: str | None) -> tuple[str, int]:
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=2))
    changed = sum(1 for l in lines if l[:1] in "+-" and not l.startswith(("+++", "---")))
    return "".join(lines)[:MAX_PREVIEW_CHARS], changed


def preview(action: Action, ctx: ProjectContext) -> list[dict]:
    """Simulate a file action WITHOUT touching disk. Returns [{file, change_type, before, after, diff, lines}]."""
    a = action.action
    if a not in ("create_file", "write_file", "patch_file", "batch_file_operations", "delete_file", "rename_file"):
        return []
    overlay: dict[str, str | None] = {}   # rel -> content after earlier ops of the same action (None = deleted)
    first_before: dict[str, str | None] = {}

    def cur(rel: str) -> str | None:
        if rel in overlay:
            return overlay[rel]
        try:
            return _read(resolve_in_root(ctx.root, rel))
        except SafetyError:
            return None

    def apply(op: str, rel: str, content=None, search=None, replace=None, dest=None):
        before = cur(rel)
        first_before.setdefault(rel, before)
        if op in ("create", "write"):
            overlay[rel] = content or ""
        elif op == "patch":
            overlay[rel] = (before or "").replace(search or "", replace or "") if search else before
        elif op == "delete":
            overlay[rel] = None
        elif op == "rename" and dest:
            first_before.setdefault(dest, cur(dest))
            overlay[dest], overlay[rel] = before, None

    if a == "batch_file_operations":
        for o in action.metadata.get("operations") or []:
            if isinstance(o, dict) and o.get("target_file"):
                apply(o.get("operation", ""), o["target_file"], o.get("content"), o.get("search_text"), o.get("replace_text"))
    elif a == "rename_file":
        apply("rename", action.path, dest=action.metadata.get("destination"))
    else:
        op = {"create_file": "create", "write_file": "write", "patch_file": "patch", "delete_file": "delete"}[a]
        apply(op, action.path, action.content, action.search_text, action.replace_text)

    out = []
    for rel, after in overlay.items():
        before = first_before[rel]
        if before == after:
            continue
        d, n = _diff(rel, before, after)
        kind = "created" if before is None else "deleted" if after is None else "modified"
        out.append({"file": rel, "change_type": kind, "before": (before or "")[:MAX_PREVIEW_CHARS],
                    "after": (after or "")[:MAX_PREVIEW_CHARS], "diff": d, "lines": n})
    return out


def _env_note(action: Action, ctx: ProjectContext) -> str:
    """Which environment a pip install would modify (the user decides knowing this)."""
    if action.action == "install_dependency" and (action.command or "").split()[:1] in (["pip"], ["python"], ["py"]):
        venv = next((d for d in (".venv", "venv") if (ctx.root / d).is_dir()), None)
        note = f"(installs into: {shutil.which('python') or 'the system Python'}"
        return note + (f"; the project's {venv}/ is NOT used automatically)" if venv else ")")
    return ""


def assess(action: Action, previews: list[dict], ctx: ProjectContext, files_in_task: set[str], lines_in_task: int,
           conflicts: list[str] | None = None) -> list[tuple[str, str]]:
    """Reasons this action needs approval, as (kind, text). Empty = nothing to ask about."""
    reasons: list[tuple[str, str]] = []
    a = action.action
    if previews:
        reasons.append(("edit", "file edit"))
    for p in previews:
        rel = p["file"]
        if p["change_type"] == "deleted":
            reasons.append(("risky", f"delete: {rel}"))
        if a == "rename_file":
            pass
        if _name_matches(rel, MANIFESTS):
            reasons.append(("risky", f"dependency manifest: {rel}"))
        elif _name_matches(rel, CONFIG_FILES):
            reasons.append(("risky", f"config file: {rel}"))
        e = check_writable(rel, ctx.protected_paths, allow_protected=False)
        if e and e.code == "APPROVAL_REQUIRED":
            reasons.append(("hard", f"protected path: {rel}"))
    if a == "rename_file":
        reasons.append(("risky", f"rename: {action.path} -> {action.metadata.get('destination')}"))
    if a == "install_dependency" and config.APPROVE_INSTALLS:
        reasons.append(("hard", f"install packages: {action.command} {_env_note(action, ctx)}".strip()))
    touched = files_in_task | {p["file"] for p in previews}
    if previews and len(touched) > config.RISK_MAX_FILES:
        reasons.append(("hard", f"limits: more than {config.RISK_MAX_FILES} files changed in one task"))
    if previews and lines_in_task + sum(p["lines"] for p in previews) > config.RISK_MAX_LINES:
        reasons.append(("hard", f"limits: more than {config.RISK_MAX_LINES} changed lines in one task"))
    for f in conflicts or []:
        reasons.append(("hard", f"{f} was modified outside Nexura since it was last read/written"))
    return reasons


def requires_approval(reasons: list[tuple[str, str]], level: str) -> list[str]:
    """The reasons that apply at this approval level (empty list = run without asking)."""
    kinds = {"ask_every_edit": {"edit", "risky", "hard"}, "ask_risky": {"risky", "hard"}}.get(level, {"hard"})
    return [text for kind, text in reasons if kind in kinds]


def parse_decision(value) -> tuple[bool, str]:
    """Resume value -> (approved, note). Accepts 'yes'/'y'/'approve' strings or {'decision': 'approve'|'reject', 'note': ...}."""
    if isinstance(value, dict):
        return str(value.get("decision", "")).lower() in ("approve", "approved", "yes", "y"), str(value.get("note") or "")
    text = str(value).strip()
    return text.lower() in ("yes", "y", "approve"), "" if text.lower() in ("yes", "y", "approve") else text
