"""V2-5.4 / 5.5: what the Planner gets to know about an EXISTING project: a cached repo summary, the project profile, the file
list and retrieval hits for the request. Everything is read-time safe (ignore rules, protected paths, secret filter) and
size-capped. Only used with RETRIEVAL_MODE=v2 on a workspace that already has code; otherwise the planner prompt is V1's."""
import hashlib
import json
import math
from pathlib import Path

import config
from core.context_builder import _list_files, _read, safe_snippet
from core.project_profile import ProjectProfile, apply_overrides, detect_profile
from core.safety import contains_secret, is_ignored, is_protected, load_ignore_patterns, redact_secrets, resolve_in_root, SafetyError
from core.schemas import ProjectContext

SUMMARY_INPUT_CHARS = 12_000       # budget for the material the summary is generated from
SUMMARY_MAX_CHARS = 2_500          # the stored summary is cut to this
PLANNER_FILES_CAP = 300            # files listed to the planner
PLANNER_HITS = 6                   # retrieval hits shown to the planner
HIT_CHARS = 700
TOP_FILES = ("README.md", "README.rst", "README.txt", "README", "package.json", "pyproject.toml")
SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".java", ".rs", ".rb", ".php", ".cs", ".c", ".cpp", ".h")

SUMMARY_PROMPT = """You are summarising an existing software project so that a planner can plan changes to it.
Write at most 15 short lines of plain text covering: purpose, structure (key folders/modules), how to run it, how to test it,
and conventions you can see. Use ONLY the material below; if something is unknown say "unknown". No code blocks, no secrets.

{material}
"""


def has_code(files: list[str]) -> bool:
    return any(f.lower().endswith(SOURCE_SUFFIXES) for f in files)


def _safe_read(root: Path, rel: str, patterns: list[str], limit: int) -> str:
    if is_ignored(rel, patterns) or is_protected(rel, config.PROTECTED_PATHS):
        return ""
    body = _read(root, rel)
    if body is None or contains_secret(body):
        return ""
    return redact_secrets(body)[:limit]


def summary_material(root: Path, profile: ProjectProfile, files: list[str], patterns: list[str]) -> tuple[str, str]:
    """(text the summary is written from, source hash). The hash changes when the PROFILE, the folder structure (two levels),
    the top-level files, the README/manifests or the order of magnitude of the file count change: not on every new file."""
    tops = sorted({f.split("/")[0] for f in files if "/" not in f})
    dirs = sorted({"/".join(f.split("/")[:-1][:2]) for f in files if "/" in f})
    heads = {n: _safe_read(root, n, patterns, 2500) for n in TOP_FILES}
    bucket = int(math.log2(len(files) + 1))
    payload = json.dumps({"profile": profile.model_dump(), "tops": tops, "dirs": dirs, "heads": heads, "bucket": bucket}, sort_keys=True)
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    parts = [f"PROFILE: {json.dumps(profile.model_dump(exclude_defaults=True))}",
             f"FILES ({len(files)} total; folders): " + ", ".join(dirs[:60]),
             "TOP-LEVEL FILES: " + ", ".join(tops[:60])]
    parts += [f"--- {n} ---\n{t}" for n, t in heads.items() if t]
    return "\n".join(parts)[:SUMMARY_INPUT_CHARS], h


def get_summary(ctx: ProjectContext, profile: ProjectProfile, files: list[str], patterns: list[str], call_text) -> str:
    """The cached summary for this project, regenerated through `call_text(prompt) -> str` (the Planner's model) when the
    source hash changed. A model failure keeps the old summary (or returns '') and is logged; it never fails planning."""
    sm, pid = ctx.state_manager, ctx.project_id
    material, source_hash = summary_material(Path(ctx.root), profile, files, patterns)
    cached = sm.get_repo_summary(pid) if sm is not None and pid else None
    if cached and cached["source_hash"] == source_hash:
        return cached["summary"]
    try:
        text = redact_secrets(call_text(SUMMARY_PROMPT.format(material=material))).strip()[:SUMMARY_MAX_CHARS]
    except Exception as e:
        ctx.emit("summary_failed", error=redact_secrets(str(e))[:200])
        return cached["summary"] if cached else ""
    if text and sm is not None and pid:
        version = sm.save_repo_summary(pid, text, source_hash)
        ctx.emit("summary_generated", version=version, chars=len(text))
    return text


def effective_profile(ctx: ProjectContext) -> ProjectProfile:
    """Detected profile with the user's overrides applied (detection is stored, re-run when nothing is stored yet)."""
    sm, pid = ctx.state_manager, ctx.project_id
    stored = sm.get_profile(pid) if sm is not None and pid else None
    if stored is None:
        detected = detect_profile(ctx.root)
        if sm is not None and pid:
            sm.save_profile(pid, detected.model_dump())
        return detected
    return apply_overrides(ProjectProfile(**stored["detected"]), stored.get("overrides"))


def path_exists_fn(root: Path | str):
    def exists(rel: str) -> bool:
        try:
            return resolve_in_root(root, rel).is_file()
        except SafetyError:
            return False
    return exists


def build_planner_context(ctx: ProjectContext, prompt: str, call_text) -> dict | None:
    """None for a new/empty project or with RETRIEVAL_MODE=v1 (the planner then behaves exactly like V1)."""
    if config.RETRIEVAL_MODE != "v2":
        return None
    root = Path(ctx.root)
    patterns = load_ignore_patterns(root)
    files = _list_files(root, patterns)
    if not has_code(files):
        return None
    profile = effective_profile(ctx)
    hits: list[dict] = []
    ix = ctx.indexer
    try:
        if ix is not None and ix.ready():
            cache: dict = {}
            for r in ix.retriever().retrieve(prompt, k=PLANNER_HITS * 2):
                snippet = safe_snippet(root, r.chunk, patterns, cache)
                if snippet is not None and len(hits) < PLANNER_HITS:
                    hits.append({"path": r.chunk.path, "lines": f"{r.chunk.start_line}-{r.chunk.end_line}", "symbol": r.chunk.symbol,
                                 "snippet": snippet[:HIT_CHARS]})
    except Exception as e:
        ctx.emit("retrieval_fallback", reason=f"planner retrieval: {redact_secrets(str(e))[:200]}")
    summary = get_summary(ctx, profile, files, patterns, call_text)
    ctx.emit("planner_context", files=len(files), hits=len(hits), has_summary=bool(summary))
    return {"profile": profile.model_dump(exclude_defaults=True), "summary": summary,
            "files": files[:PLANNER_FILES_CAP], "files_total": len(files), "hits": hits,
            "extra_commands": list(ctx.extra_commands), "exists": path_exists_fn(root)}
