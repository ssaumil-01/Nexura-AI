import json
import os
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from core.safety import SafetyError, check_writable, resolve_in_root
from core.schemas import ProjectContext
from tools._ctx import err, get_project


def _target(ctx: ProjectContext, target: str, writing: bool):
    """Resolve a workspace-relative target. Returns (full_path, key) or raises SafetyError."""
    full = resolve_in_root(ctx.root, target)
    key = ctx.key(full)
    if writing:
        e = check_writable(key, ctx.protected_paths, ctx.allow_protected)
        if e:
            raise e
    return full, key


def _check_content(ctx: ProjectContext, content: str) -> None:
    if "\x00" in content:
        raise SafetyError("BINARY_CONTENT", "Binary content cannot be written.")
    if len(content.encode("utf-8")) > ctx.limits.max_file_bytes:
        raise SafetyError("TOO_LARGE", f"Content exceeds {ctx.limits.max_file_bytes} bytes.")


def _read_text(full) -> str:
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


# Core operations: return a success message or raise SafetyError. Shared by tools and batch.

def _create(ctx, target, content):
    full, key = _target(ctx, target, True)
    _check_content(ctx, content)
    if os.path.exists(full):
        raise SafetyError("FILE_EXISTS", f"{target} already exists. Read it, then use write_file or patch_file.")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    ctx.files_read.add(key)
    return f"Successfully created file: {target}"


def _write(ctx, target, content):
    full, key = _target(ctx, target, True)
    _check_content(ctx, content)
    if not os.path.isfile(full):
        raise SafetyError("FILE_NOT_FOUND", f"{target} does not exist. Use create_file for new files.")
    if key not in ctx.files_read:
        raise SafetyError("READ_REQUIRED", f"Read {target} with read_file before modifying it.")
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully updated file: {target}"


def _patch(ctx, target, search_text, replace_text):
    full, key = _target(ctx, target, True)
    if not os.path.isfile(full):
        raise SafetyError("FILE_NOT_FOUND", f"File not found: {target}")
    if key not in ctx.files_read:
        raise SafetyError("READ_REQUIRED", f"Read {target} with read_file before modifying it.")
    try:
        content = _read_text(full)
    except UnicodeDecodeError:
        raise SafetyError("BINARY_FILE", f"Cannot read binary file: {target}")
    if search_text not in content:
        raise SafetyError("SEARCH_NOT_FOUND", f"search_text not found in {target}. Make sure it matches exactly.")
    count = content.count(search_text)
    new_content = content.replace(search_text, replace_text)
    _check_content(ctx, new_content)
    with open(full, "w", encoding="utf-8") as f:
        f.write(new_content)
    return f"Successfully patched {target} ({count} occurrence(s) replaced)."


def _as_str(fn, *a):
    try:
        return fn(*a)
    except SafetyError as e:
        return err(e.code, e.message)


@tool
def create_file(target_file: str, content: str, config: RunnableConfig) -> str:
    """Creates a NEW file in the workspace. Fails if the file already exists."""
    return _as_str(_create, get_project(config), target_file, content)


@tool
def write_file(target_file: str, content: str, config: RunnableConfig) -> str:
    """Overwrites an EXISTING file with new content. You must read_file it first in this task."""
    return _as_str(_write, get_project(config), target_file, content)


@tool
def read_file(target_file: str, config: RunnableConfig) -> str:
    """Reads and returns the content of a file in the workspace."""
    ctx = get_project(config)
    try:
        full, key = _target(ctx, target_file, False)
    except SafetyError as e:
        return err(e.code, e.message)
    if not os.path.isfile(full):
        return err("FILE_NOT_FOUND", f"File not found: {target_file}")
    try:
        text = _read_text(full)
    except UnicodeDecodeError:
        return err("BINARY_FILE", f"Cannot read binary file: {target_file}")
    ctx.files_read.add(key)
    return text


@tool
def patch_file(target_file: str, search_text: str, replace_text: str, config: RunnableConfig) -> str:
    """Patches a file by finding search_text and replacing it with replace_text.
    Much more efficient than rewriting the entire file for small changes.
    The search_text must be an exact match of existing content. You must read_file it first in this task."""
    return _as_str(_patch, get_project(config), target_file, search_text, replace_text)


@tool
def batch_file_operations(operations: list[dict], config: RunnableConfig) -> str:
    """Executes multiple file operations in a single tool call and returns a JSON list of per-operation results.
    Supported operations: 'create' (new file), 'write' (existing file, must be read first), 'patch'.
    Format: [{'operation': 'create', 'target_file': '...', 'content': '...'},
             {'operation': 'patch', 'target_file': '...', 'search_text': '...', 'replace_text': '...'}]
    """
    ctx = get_project(config)
    results = []
    for op in operations:
        op_type, target = op.get("operation"), op.get("target_file")
        res = {"operation": op_type, "target_file": target, "ok": False, "code": None, "message": ""}
        try:
            if not target:
                raise SafetyError("BAD_OPERATION", "Missing 'target_file'.")
            if op_type == "create":
                res["message"] = _create(ctx, target, op.get("content", ""))
            elif op_type == "write":
                res["message"] = _write(ctx, target, op.get("content", ""))
            elif op_type == "patch":
                if op.get("search_text") is None or op.get("replace_text") is None:
                    raise SafetyError("BAD_OPERATION", "Missing search_text/replace_text.")
                res["message"] = _patch(ctx, target, op["search_text"], op["replace_text"])
            else:
                raise SafetyError("BAD_OPERATION", f"Unknown operation type '{op_type}'.")
            res["ok"] = True
        except SafetyError as e:
            res["code"], res["message"] = e.code, e.message
        results.append(res)
    return json.dumps(results)
