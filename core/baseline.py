"""V2-5.3 / 5.8: record the project's own test results before a run (baseline) and compare after it. Pre-existing failures are
never blamed on the agent: only failures that are NEW compared with the baseline count as a regression.

Tests run through validator -> execution_manager (-> ExecutionBackend) like any other command, so the allowlist, workspace
extensions and output rules apply. Parsing is best effort: pytest gives test ids, other runners give exit code and counts."""
import re

from core import execution_manager as em
from core import validator
from core.safety import redact_secrets
from core.schemas import Action

BASELINE_TIMEOUT_S = 300
BASELINE_OUTPUT_BYTES = 40_000
KEEP_OUTPUT_CHARS = 4000
PYTEST_ID = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s.*)?$", re.M)
JEST_ID = re.compile(r"^\s*(?:FAIL\s+(\S+)|[✕×]\s+(.+?)(?:\s+\(\d+\s*ms\))?)\s*$", re.M)
COUNT = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped)")


def parse_results(stdout: str, exit_code: int) -> dict:
    """{'exit_code', 'passed', 'failed', 'failing': [ids]}; ids are test node ids when the runner prints them, else []."""
    counts = {"passed": 0, "failed": 0}
    for n, word in COUNT.findall(stdout):
        if word == "passed":
            counts["passed"] = int(n)
        elif word in ("failed", "error", "errors"):
            counts["failed"] += int(n)
    ids = list(dict.fromkeys(PYTEST_ID.findall(stdout)))
    if not ids:
        ids = list(dict.fromkeys(a or b for a, b in JEST_ID.findall(stdout)))
    return {"exit_code": exit_code, "passed": counts["passed"], "failed": max(counts["failed"], len(ids)), "failing": ids}


def run_tests(ctx, command: str, timeout_s: int = BASELINE_TIMEOUT_S) -> dict:
    """Run the test command once. Never raises. Result keys: command, ran (False = could not run), plus parse_results()."""
    action = Action(action="run_command", command=command)
    v = validator.validate(action, ctx)
    if not v.ok:
        return {"command": command, "ran": False, "reason": f"{v.code}: {v.message}"}
    limits = ctx.limits
    saved = (limits.command_timeout_s, limits.max_tool_output_bytes)
    limits.command_timeout_s, limits.max_tool_output_bytes = timeout_s, BASELINE_OUTPUT_BYTES
    try:
        res = em.run(action, ctx, task_id=0)
    finally:
        limits.command_timeout_s, limits.max_tool_output_bytes = saved
    if res.code == "TIMEOUT":
        return {"command": command, "ran": False, "reason": f"timed out after {timeout_s}s"}
    if not res.ok and res.code != "COMMAND_FAILED":
        return {"command": command, "ran": False, "reason": f"{res.code}: {(res.stderr or res.stdout)[:200]}"}
    out = res.stdout
    code = 0 if res.ok else (res.exit_code if res.exit_code is not None else 1)
    return {"command": command, "ran": True, **parse_results(out, code), "output": redact_secrets(out)[-KEEP_OUTPUT_CHARS:]}


def compare(baseline: dict, after: dict) -> dict:
    """{'regressed', 'new_failures', 'fixed', 'message'}. New failures = failing now, not failing in the baseline."""
    if not baseline.get("ran") or not after.get("ran"):
        why = after.get("reason") or baseline.get("reason") or "tests did not run"
        return {"regressed": False, "new_failures": [], "fixed": [], "message": f"Regression check skipped: {why}", "checked": False}
    b_ids, a_ids = set(baseline["failing"]), set(after["failing"])
    if b_ids or a_ids:
        new, fixed = sorted(a_ids - b_ids), sorted(b_ids - a_ids)
        regressed = bool(new)
    else:    # no test ids available: fall back to exit code and failure counts
        new, fixed = [], []
        regressed = (baseline["exit_code"] == 0 and after["exit_code"] != 0) or after["failed"] > baseline["failed"]
    if regressed:
        msg = "Regression: " + (f"new failing tests: {', '.join(new[:10])}" if new else
                                f"the test command now fails (exit code {after['exit_code']}, {after['failed']} failing; baseline {baseline['failed']})")
        msg += f". {len(b_ids)} test(s) were already failing before the run and are not counted." if b_ids else "."
    else:
        msg = "No new test failures compared with the baseline" + (f" ({len(fixed)} previously failing test(s) now pass)." if fixed else ".")
    return {"regressed": regressed, "new_failures": new, "fixed": fixed, "message": msg, "checked": True}
