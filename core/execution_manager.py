"""The ONLY module that imports or calls tools. Everything else proposes Actions; this runs them.
Never raises: failures come back as ExecResult(ok=False, code=...)."""
import hashlib
import json
import os
import re
import time

from core.safety import SafetyError, rel_path, resolve_in_root
from core.schemas import Action, ExecResult, FileChange, ProjectContext
from core.validator import MUTATING

from tools.directory_tools import create_directory, list_directory
from tools.file_tools import batch_file_operations, create_file, delete_file, patch_file, read_file, rename_file, write_file
from tools.search_tools import web_search
from tools.signals import task_complete
from tools.system_tools import install_dependency, read_process_output, run_command, start_process, stop_process
from tools.verification_tools import verify_code_syntax, verify_webapp_endpoint

TOOL_SCHEMAS = [create_file, write_file, read_file, patch_file, batch_file_operations, delete_file, rename_file,
                create_directory, list_directory,
                run_command, install_dependency, start_process, read_process_output, stop_process, task_complete,
                web_search, verify_code_syntax, verify_webapp_endpoint]
TOOLS_BY_NAME = {t.name: t for t in TOOL_SCHEMAS}

INTERNAL_ACTIONS = {"file_exists"}  # orchestrator-only (verification); never accepted from agents


# ─── tool call <-> Action ───────────────────────────────────

def action_from_tool_call(name: str, args: dict) -> Action:
    """Convert an LLM tool call into an Action (unknown/odd args end up in metadata so the validator can judge them)."""
    a = dict(args or {})
    get = a.get
    if name in ("create_file", "write_file"):
        return Action(action=name, path=get("target_file"), content=get("content"))
    if name == "patch_file":
        return Action(action=name, path=get("target_file"), search_text=get("search_text"), replace_text=get("replace_text"))
    if name in ("read_file",):
        return Action(action=name, path=get("target_file"))
    if name == "create_directory":
        return Action(action=name, path=get("target_dir"))
    if name == "list_directory":
        return Action(action=name, path=get("target_dir", "."))
    if name == "batch_file_operations":
        return Action(action=name, metadata={"operations": get("operations")})
    if name == "delete_file":
        return Action(action=name, path=get("target_file"))
    if name == "rename_file":
        return Action(action=name, path=get("source_file"), metadata={"destination": get("destination_file")})
    if name in ("run_command", "install_dependency", "start_process"):
        return Action(action=name, command=get("command"))
    if name in ("read_process_output", "stop_process"):
        return Action(action=name, metadata={"handle": get("handle")})
    if name == "verify_code_syntax":
        return Action(action=name, path=get("file_path"))
    if name == "verify_webapp_endpoint":
        return Action(action=name, metadata={"url": get("url")})
    if name == "web_search":
        return Action(action=name, metadata={"query": get("query")})
    if name == "task_complete":
        return Action(action=name, metadata={"summary": get("summary", "")})
    return Action(action=name, metadata=a)


def _tool_args(a: Action) -> dict:
    m = a.metadata
    return {
        "create_file": lambda: {"target_file": a.path, "content": a.content},
        "write_file": lambda: {"target_file": a.path, "content": a.content},
        "patch_file": lambda: {"target_file": a.path, "search_text": a.search_text, "replace_text": a.replace_text},
        "read_file": lambda: {"target_file": a.path},
        "create_directory": lambda: {"target_dir": a.path},
        "list_directory": lambda: {"target_dir": a.path or "."},
        "batch_file_operations": lambda: {"operations": m.get("operations")},
        "delete_file": lambda: {"target_file": a.path},
        "rename_file": lambda: {"source_file": a.path, "destination_file": m.get("destination")},
        "run_command": lambda: {"command": a.command},
        "install_dependency": lambda: {"command": a.command},
        "start_process": lambda: {"command": a.command},
        "read_process_output": lambda: {"handle": m.get("handle")},
        "stop_process": lambda: {"handle": m.get("handle")},
        "verify_code_syntax": lambda: {"file_path": a.path},
        "verify_webapp_endpoint": lambda: {"url": m.get("url")},
        "web_search": lambda: {"query": m.get("query")},
        "task_complete": lambda: {"summary": m.get("summary", "")},
    }[a.action]()


# ─── helpers ────────────────────────────────────────────────

def _sha(path) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _write_targets(a: Action, ctx: ProjectContext) -> list[str]:
    if a.action == "batch_file_operations":
        paths = [o.get("target_file") for o in (a.metadata.get("operations") or []) if isinstance(o, dict)]
    elif a.action in ("create_file", "write_file", "patch_file", "delete_file"):
        paths = [a.path]
    elif a.action == "rename_file":
        paths = [a.path, a.metadata.get("destination")]
    else:
        return []
    out = []
    for p in dict.fromkeys(p for p in paths if p):
        try:
            out.append(str(resolve_in_root(ctx.root, p)))
        except SafetyError:
            pass
    return out


def _parse(name: str, out: str) -> dict:
    """Turn a tool's string result into ok/code/exit_code/stdout/stderr."""
    m = re.match(r"ERROR: ([A-Z_]+): (.*)", out, re.DOTALL)
    if m:
        code = m.group(1)
        # a timed-out command: the part after the message is partial output
        return {"ok": False, "code": code, "stderr": m.group(2), "stdout": ""}
    m = re.match(r"Command failed \(exit code (-?\d+)\):\n?(.*)", out, re.DOTALL)
    if m:
        return {"ok": False, "code": "COMMAND_FAILED", "exit_code": int(m.group(1)), "stdout": m.group(2)}
    if name == "batch_file_operations":
        try:
            ops = json.loads(out)
            bad = [o for o in ops if not o["ok"]]
            return {"ok": not bad, "code": bad[0]["code"] if bad else None, "stdout": out,
                    "stderr": "; ".join(f"{o['target_file']}: {o['code']} {o['message']}" for o in bad)}
        except (ValueError, KeyError, TypeError):
            return {"ok": False, "code": "BAD_TOOL_OUTPUT", "stdout": out}
    if name == "verify_code_syntax" and out.startswith(("SyntaxError", "Verification error")):
        return {"ok": False, "code": "SYNTAX_ERROR", "stderr": out}
    if name == "verify_webapp_endpoint" and out.startswith("Endpoint check failed"):
        return {"ok": False, "code": "ENDPOINT_FAILED", "stderr": out}
    if name in ("web_search",) and out.startswith(("ERROR", "Web search failed")):
        return {"ok": False, "code": "SEARCH_FAILED", "stderr": out}
    res = {"ok": True, "stdout": out}
    if name in ("run_command", "install_dependency"):
        res["exit_code"] = 0
    return res


# ─── public API ─────────────────────────────────────────────

def run(action: Action, ctx: ProjectContext, task_id: int = 0) -> ExecResult:
    """Execute one (already validated) Action. Never raises."""
    t0 = time.monotonic()
    try:
        if not ctx.trusted and action.action in MUTATING:   # defence in depth: the validator already rejects these
            return ExecResult(ok=False, code="UNTRUSTED_WORKSPACE", stderr="This workspace is not trusted.")
        if action.action == "file_exists":
            ok = resolve_in_root(ctx.root, action.path or "").is_file()
            return ExecResult(ok=ok, code=None if ok else "FILE_NOT_FOUND", stdout=action.path or "",
                              duration_ms=int((time.monotonic() - t0) * 1000))
        tool = TOOLS_BY_NAME.get(action.action)
        if tool is None:
            return ExecResult(ok=False, code="UNKNOWN_ACTION", stderr=f"No tool for action '{action.action}'.")

        targets = _write_targets(action, ctx)
        before = {t: _sha(t) for t in targets}
        out = tool.invoke(_tool_args(action), {"configurable": {"project": ctx}})
        out = out if isinstance(out, str) else str(out)

        res = ExecResult(**_parse(action.action, out))
        res.truncated = "[truncated]" in out
        for t in targets:
            after = _sha(t)
            if after != before[t]:
                res.file_changes.append(FileChange(
                    file=rel_path(ctx.root, t), change_type="deleted" if after is None else "created" if before[t] is None else "modified",
                    task_id=task_id, sha256_before=before[t], sha256_after=after))
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        return res
    except SafetyError as e:
        return ExecResult(ok=False, code=e.code, stderr=e.message)
    except Exception as e:  # tools must not take the run down
        return ExecResult(ok=False, code="TOOL_EXCEPTION", stderr=str(e)[:500],
                          duration_ms=int((time.monotonic() - t0) * 1000))
