"""Validates every proposed Action BEFORE it can run. Pure checks; no side effects except reading the filesystem."""
import os

import config
from core import patching
from core.safety import SafetyError, check_writable, is_ignored, parse_command, resolve_in_root, NEVER_IN_CONTEXT
from core.schemas import Action, ProjectContext, ValidationResult

OK = ValidationResult(ok=True)

RBW_ACTIONS = {"create_file", "write_file", "patch_file", "apply_patch", "batch_file_operations"}   # read-before-write rules apply
WRITE_ACTIONS = RBW_ACTIONS | {"delete_file", "rename_file"}
MUTATING = WRITE_ACTIONS | {"create_directory", "run_command", "install_dependency", "start_process"}
# action -> required (field | metadata key)
REQUIRED = {
    "create_file": ["path", "content"], "write_file": ["path", "content"],
    "patch_file": ["path", "search_text", "replace_text"], "apply_patch": ["path", "m:hunks"], "read_file": ["path"],
    "delete_file": ["path"], "rename_file": ["path", "m:destination"],
    "create_directory": ["path"], "list_directory": [],
    "batch_file_operations": ["m:operations"],
    "run_command": ["command"], "install_dependency": ["command"], "start_process": ["command"],
    "read_process_output": ["m:handle"], "stop_process": ["m:handle"],
    "verify_code_syntax": ["path"], "verify_webapp_endpoint": ["m:url"],
    "web_search": ["m:query"], "task_complete": [],
}
# What the Debugger may do (fix the failure; no new files, no plan edits, no installs)
DEBUG_ALLOWED = {"read_file", "patch_file", "apply_patch", "write_file", "run_command", "list_directory", "verify_code_syntax"}
PLAN_FILES = {"plan.json", "plan.md"}


def fail(code: str, message: str) -> ValidationResult:
    return ValidationResult(ok=False, code=code, message=message)


def _ops(action: Action) -> list[dict]:
    return action.metadata.get("operations") or []


def validate_schema(action: Action) -> ValidationResult:
    req = REQUIRED.get(action.action)
    if req is None:
        return fail("UNKNOWN_ACTION", f"Unknown action '{action.action}'.")
    for r in req:
        if r.startswith("m:"):
            if not action.metadata.get(r[2:]):
                return fail("BAD_SCHEMA", f"{action.action} requires '{r[2:]}'.")
        elif getattr(action, r) is None:
            return fail("BAD_SCHEMA", f"{action.action} requires '{r}'.")
    if action.action == "batch_file_operations" and not isinstance(action.metadata["operations"], list):
        return fail("BAD_SCHEMA", "operations must be a list.")
    return OK


def validate_allowed_action(action: Action, mode: str = "code") -> ValidationResult:
    if action.action not in config.ALLOWED_ACTIONS or (action.action == "apply_patch" and config.PATCH_MODE != "v2"):
        return fail("NOT_ALLOWED", f"Action '{action.action}' is not allowed.")
    if mode == "debug" and action.action not in DEBUG_ALLOWED:
        return fail("NOT_ALLOWED_IN_DEBUG", f"The debugger may only use: {', '.join(sorted(DEBUG_ALLOWED))}.")
    return OK


def _paths(action: Action) -> list[str]:
    if action.action == "batch_file_operations":
        return [str(o.get("target_file")) for o in _ops(action) if isinstance(o, dict)]
    if action.action == "rename_file":
        return [action.path, str(action.metadata.get("destination"))]
    return [action.path] if action.path is not None else []


def validate_path(action: Action, ctx: ProjectContext) -> ValidationResult:
    for p in _paths(action):
        try:
            resolve_in_root(ctx.root, p)
        except SafetyError as e:
            return fail(e.code, e.message)
    return OK


def validate_single_file(action: Action) -> ValidationResult:
    """One file per write action; batching only through the explicit batch action, capped."""
    if action.action == "batch_file_operations":
        ops = _ops(action)
        if len(ops) > config.MAX_BATCH_OPS:
            return fail("BATCH_TOO_LARGE", f"At most {config.MAX_BATCH_OPS} operations per batch.")
        if not all(isinstance(o, dict) for o in ops):
            return fail("BAD_SCHEMA", "Each batch operation must be an object.")
    return OK


def validate_task_alignment(action: Action, ctx: ProjectContext, mode: str = "code") -> ValidationResult:
    """Agents cannot edit the plan or Nexura's own state; debug mode may only touch existing files."""
    for p in _paths(action):
        if os.path.basename(p.replace("\\", "/")).lower() in PLAN_FILES or p.replace("\\", "/").startswith(".nexura/"):
            return fail("PLAN_IMMUTABLE", f"{p} is managed by the orchestrator; agents cannot change the plan.")
    if mode == "debug" and action.action == "write_file":
        try:
            if not resolve_in_root(ctx.root, action.path).is_file():
                return fail("DEBUG_NEW_FILE", "The debugger may only modify existing files.")
        except SafetyError as e:
            return fail(e.code, e.message)
    return OK


def validate_read_before_write(action: Action, ctx: ProjectContext) -> ValidationResult:
    if action.action not in RBW_ACTIONS:
        return OK
    ops = _ops(action) if action.action == "batch_file_operations" else [
        {"operation": {"create_file": "create", "write_file": "write", "patch_file": "patch", "apply_patch": "patch"}[action.action], "target_file": action.path}]
    known = set(ctx.files_read)  # simulate sequential effects inside a batch
    for o in ops:
        kind, target = o.get("operation"), o.get("target_file")
        try:
            full = resolve_in_root(ctx.root, str(target))
        except SafetyError as e:
            return fail(e.code, e.message)
        key = ctx.key(full)
        exists = full.is_file() or key in known
        if kind == "create":
            if exists:
                return fail("FILE_EXISTS", f"{target} already exists. Read it, then use write_file or patch_file.")
            known.add(key)
        elif kind in ("write", "patch"):
            if not exists:
                return fail("FILE_NOT_FOUND", f"{target} does not exist. Use create_file for new files.")
            if key not in known:
                return fail("READ_REQUIRED", f"Read {target} with read_file before modifying it.")
        else:
            return fail("BAD_OPERATION", f"Unknown operation type '{kind}'.")
    return OK


def validate_patch(action: Action, ctx: ProjectContext) -> ValidationResult:
    """V2: a patch that cannot apply is rejected BEFORE the user is asked to approve it (dry run against the file on disk)."""
    if config.PATCH_MODE != "v2" or action.action not in ("apply_patch", "patch_file"):
        return OK
    hunks = action.metadata.get("hunks") if action.action == "apply_patch" else [
        {"search_text": action.search_text, "replace_text": action.replace_text}]
    try:
        full = resolve_in_root(ctx.root, action.path)
        patching.check_hunks(hunks)
        if full.is_file():
            patching.patch_bytes(full.read_bytes(), hunks, ctx.limits.max_file_bytes)
    except SafetyError as e:
        return fail(e.code, e.message)
    except OSError as e:
        return fail("FILE_NOT_FOUND", str(e))
    return OK


def validate_command(action: Action, ctx: ProjectContext) -> ValidationResult:
    if action.action not in ("run_command", "install_dependency", "start_process"):
        return OK
    try:
        parse_command(action.command, ctx.root, allow_install=(action.action == "install_dependency"), extra_allowed=ctx.extra_commands)
    except SafetyError as e:
        return fail(e.code, e.message)
    return OK


def validate_protected_path(action: Action, ctx: ProjectContext) -> ValidationResult:
    if action.action not in WRITE_ACTIONS | {"create_directory"}:
        return OK
    for p in _paths(action):
        try:
            key = ctx.key(resolve_in_root(ctx.root, p))
        except SafetyError as e:
            return fail(e.code, e.message)
        e = check_writable(key, ctx.protected_paths, ctx.allow_protected)
        if e:
            return fail(e.code, e.message)
    return OK


def validate_trusted(action: Action, ctx: ProjectContext) -> ValidationResult:
    if not ctx.trusted and action.action in MUTATING:
        return fail("UNTRUSTED_WORKSPACE", "This workspace is not trusted; Nexura may only read files.")
    return OK


def validate_dirty_files(action: Action, ctx: ProjectContext) -> ValidationResult:
    """Never touch files the user has uncommitted/unsaved changes in."""
    if action.action not in WRITE_ACTIONS or not ctx.dirty_files:
        return OK
    dirty = {d.replace("\\", "/").lower() for d in ctx.dirty_files}
    for p in _paths(action):
        try:
            key = ctx.key(resolve_in_root(ctx.root, p))
        except SafetyError:
            continue
        if key.lower() in dirty:
            return fail("DIRTY_FILE", f"{p} has unsaved or uncommitted changes of yours; Nexura will not touch it.")
    return OK


def validate_ignored(action: Action, ctx: ProjectContext) -> ValidationResult:
    """Secrets/ignored files are never read into the model context, and ignored files are not edited."""
    if action.action not in ("read_file", "verify_code_syntax") and action.action not in WRITE_ACTIONS:
        return OK
    for p in _paths(action):
        try:
            key = ctx.key(resolve_in_root(ctx.root, p))
        except SafetyError:
            continue
        if is_ignored(key, NEVER_IN_CONTEXT) and action.action in ("read_file", "verify_code_syntax"):
            return fail("SECRET_FILE", f"{p} may contain secrets and cannot be read.")
        if ctx.ignore_patterns and is_ignored(key, ctx.ignore_patterns):
            return fail("IGNORED_PATH", f"{p} is excluded by .gitignore/.nexuraignore.")
    return OK


def validate(action: Action, ctx: ProjectContext, mode: str = "code") -> ValidationResult:
    """Run every check; the first failure wins."""
    checks = [
        lambda: validate_schema(action),
        lambda: validate_allowed_action(action, mode),
        lambda: validate_trusted(action, ctx),
        lambda: validate_single_file(action),
        lambda: validate_path(action, ctx),
        lambda: validate_task_alignment(action, ctx, mode),
        lambda: validate_dirty_files(action, ctx),
        lambda: validate_ignored(action, ctx),
        lambda: validate_read_before_write(action, ctx),
        lambda: validate_command(action, ctx),
        lambda: validate_patch(action, ctx),
        lambda: validate_protected_path(action, ctx),
    ]
    for check in checks:
        r = check()
        if not r.ok:
            return r
    return OK
