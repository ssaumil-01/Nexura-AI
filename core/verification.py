"""Objective task verification. A task is `done` only if this passes; the agent's task_complete is just a request."""
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import config
from core import execution_manager as em
from core.safety import SafetyError, resolve_in_root
from core.schemas import Action, CheckResult, ProjectContext, VerificationResult

SYNTAX_OK = re.compile(r"^Syntax check passed|not strictly supported")


def _run(action: str, ctx: ProjectContext, **kw):
    return em.run(Action(action=action, **kw), ctx)


def _syntax(ctx: ProjectContext, path: str) -> CheckResult:
    r = _run("verify_code_syntax", ctx, path=path)
    ok = r.ok and bool(SYNTAX_OK.search(r.stdout))
    return CheckResult(type="syntax_ok", ok=ok, detail=f"{path}: " + ("ok" if ok else (r.stderr or r.stdout)[:400]))


def _command(ctx: ProjectContext, a: dict, kind: str) -> CheckResult:
    r = _run("run_command", ctx, command=a["command"])
    if kind == "output_contains":
        ok = r.ok and a["text"] in r.stdout
        return CheckResult(type=kind, ok=ok, detail=f"`{a['command']}` " + ("output contains text" if ok else
                           f"expected output containing {a['text']!r}; exit={r.exit_code if r.exit_code is not None else r.code}; got: {(r.stdout or r.stderr)[-400:]}"))
    return CheckResult(type=kind, ok=r.ok, detail=f"`{a['command']}` " + ("exited 0" if r.ok else
                       f"failed ({r.code}, exit={r.exit_code}): {(r.stdout or r.stderr)[-600:]}"))


def _http(ctx: ProjectContext, a: dict, sleep) -> CheckResult:
    url = a["url"]
    if (urlparse(url).hostname or "").lower() not in ("localhost", "127.0.0.1", "::1"):
        return CheckResult(type="http_status", ok=False, detail=f"only localhost URLs are allowed: {url}")
    expect, wait_s = a.get("expect") or 200, a.get("wait_s") or 10
    handle = None
    try:
        if a.get("start_command"):
            r = _run("start_process", ctx, command=a["start_command"])
            if not r.ok:
                return CheckResult(type="http_status", ok=False, detail=f"could not start `{a['start_command']}`: {r.stderr}")
            handle = r.stdout.split()[-1].rstrip(".")
        deadline, last = time.monotonic() + wait_s, ""
        while True:
            r = _run("verify_webapp_endpoint", ctx, metadata={"url": url})
            m = re.search(r"Status code: (\d+)", r.stdout)
            if m and int(m.group(1)) == expect:
                return CheckResult(type="http_status", ok=True, detail=f"{url} -> {expect}")
            last = f"status {m.group(1)}" if m else (r.stderr or r.stdout)[:200]
            if time.monotonic() >= deadline:
                break
            sleep(0.5)
        out = ""
        if handle:
            out = _run("read_process_output", ctx, metadata={"handle": handle}).stdout[-400:]
        return CheckResult(type="http_status", ok=False, detail=f"{url} expected {expect}, got {last}. Server output: {out}")
    finally:
        if handle:
            _run("stop_process", ctx, metadata={"handle": handle})


def verify(task: dict, ctx: ProjectContext, changed_files: list[str], command_results: list[dict],
           sleep=time.sleep) -> VerificationResult:
    """Run the step's acceptance checks. With none, fall back to: syntax-check files changed in this task
    and require the LAST run of every distinct command to have exited 0."""
    checks: list[CheckResult] = []
    acceptance = task.get("acceptance") or []
    for a in acceptance:
        t = a["type"]
        if t == "file_exists":
            r = _run("file_exists", ctx, path=a["path"])
            checks.append(CheckResult(type=t, ok=r.ok, detail=f"{a['path']}: " + ("exists" if r.ok else "missing")))
        elif t == "syntax_ok":
            checks += [_syntax(ctx, p) for p in a["paths"]]
        elif t in ("command_ok", "output_contains"):
            checks.append(_command(ctx, a, t))
        elif t == "http_status":
            checks.append(_http(ctx, a, sleep))
        else:
            checks.append(CheckResult(type=t, ok=False, detail="unknown acceptance type"))
    if not acceptance:
        checks += [_syntax(ctx, f) for f in changed_files]
        last = {}
        for c in command_results:
            last[c["command"]] = c["exit_code"]
        checks += [CheckResult(type="command_ok", ok=code == 0, detail=f"`{cmd}` " + ("exited 0" if code == 0 else f"last run failed (exit={code})"))
                   for cmd, code in last.items()]
        if not checks:   # no checks, nothing changed, nothing run: "done" would be an unverified claim
            checks.append(CheckResult(type="nothing_verified", ok=False,
                                      detail="This step has no acceptance checks and no file was changed or command run. Make the change, then finish."))
    if config.RETRIEVAL_MODE == "v2":
        checks += [_streamlit_smoke(ctx, f) for f in changed_files if _imports_streamlit(ctx, f)]
    return VerificationResult(ok=all(c.ok for c in checks), checks=checks)


STREAMLIT_IMPORT = re.compile(r"^\s*(?:import streamlit|from streamlit\b)", re.M)


def _imports_streamlit(ctx: ProjectContext, rel: str) -> bool:
    if not rel.endswith(".py") or Path(rel).name.startswith("test_"):
        return False
    try:
        return bool(STREAMLIT_IMPORT.search(resolve_in_root(ctx.root, rel).read_text(encoding="utf-8", errors="replace")))
    except (OSError, SafetyError):
        return False


def _streamlit_smoke(ctx: ProjectContext, rel: str) -> CheckResult:
    """V2: syntax_ok cannot see a Streamlit page that crashes when it renders; run it once headless (Streamlit's own AppTest)."""
    path = rel.replace("\\", "/")
    cmd = (f'python -c "from streamlit.testing.v1 import AppTest; at = AppTest.from_file(\'{path}\', default_timeout=30).run(); '
           f'assert not at.exception, [e.message for e in at.exception]; print(\'page rendered\')"')
    r = _run("run_command", ctx, command=cmd)
    return CheckResult(type="streamlit_smoke", ok=r.ok, detail=f"{path}: " + ("page renders without errors" if r.ok else
                       f"the page crashed when rendered: {(r.stderr or r.stdout)[-600:]}"))
