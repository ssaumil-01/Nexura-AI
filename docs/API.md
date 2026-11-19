# Nexura local API

The backend (`python server.py`) is meant to be started by the VS Code extension. It binds to **127.0.0.1 only**, rejects any
`Host` header that is not `127.0.0.1`, `localhost` or `[::1]` (DNS-rebinding guard), sends **no CORS headers**, and requires
`Authorization: Bearer <NEXURA_TOKEN>` on **every** request (including `/health`). `NEXURA_TOKEN` must be at least 16 characters.

Environment: `NEXURA_TOKEN` (required), `NEXURA_PORT` (default 8000), `NEXURA_DATA_DIR` (state DB, checkpoints, run logs),
plus the model keys (`GOOGLE_API_KEY`, `GROQ_API_KEY`) and `CODER_MODEL_NAME` / `PLANNER_MODEL_NAME`.

IDs (`workspace_id`, `run_id`) must match `^[a-f0-9]{8,64}$`, otherwise the request is rejected with 422/404.
The workspace root is stored server-side when the workspace is registered and is never read from a request body afterwards.

## Endpoints

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/health` | | `{"ok": true, "active_runs": 0}` |
| POST | `/workspaces` | `{"path": "/abs/dir", "trusted": true}` | `{"workspace_id": "<32 hex>"}` (id = hash of the real path) |
| POST | `/runs` | `{"workspace_id", "prompt", "approval_level", "dirty_policy", "dirty_files": [], "git_init": false, "baseline": false, "subproject": ""}` | `{"run_id"}` |
| GET | `/runs/{id}` | | run, tasks, `state`, `waiting` (current question), `summary`, `plan` |
| GET | `/runs/{id}/events` | | Server-Sent Events; reconnect with `Last-Event-ID` |
| POST | `/runs/{id}/plan` | `{"decision": "approve" \| "feedback", "text"?}` | `{"ok": true}` |
| POST | `/runs/{id}/approve` | `{"decision": "approve" \| "reject", "note"?, "rejected_files"?: []}` | `{"ok": true}` (V2: approve the rest, reject changes to the listed files) |
| POST | `/runs/{id}/cancel` | | `{"ok": true}` |
| POST | `/runs/{id}/resume` | | `{"run_id"}` (continues from the first unfinished task) |
| POST | `/runs/{id}/undo` | `{"scope": "task" \| "run", "task_id"?, "confirm_conflicts"?}` | `{"applied", "restored", "deleted", "conflicts", "error"}` |
| POST | `/runs/{id}/edits` | `{"files": ["a.py"]}` | `{"marked": n}` - the user edited these files during the run |
| GET | `/metrics/{run_id}` | | success rate, retries per task, error types, tokens, wall time |

V2 endpoints (all need the token; all paths go through the workspace path checks):

| Method | Path | Body / query | Result |
|---|---|---|---|
| GET | `/runs/{id}/plans` | | `{"run_id", "versions": [{"version", "reason": "initial" \| "replan", "feedback", "created_at"}]}` |
| GET | `/runs/{id}/plans/{version}` | | one stored plan (`plan`, `reason`, `feedback`); 404 for an unknown version |
| POST | `/workspaces/{id}/index` | `{"rebuild": false}` | 202; starts a background index build (409 `INDEXING_DISABLED`, 503 `INDEX_DEPS_MISSING`) |
| GET | `/workspaces/{id}/index` | | `{"state": "none" \| "indexing" \| "ready" \| "incomplete" \| "cancelled" \| "failed", "done", "total", "files", "chunks", "notice", "error"}` |
| POST | `/workspaces/{id}/index/cancel` | | cancels the running build (resumable) |
| DELETE | `/workspaces/{id}/index` | | deletes the stored index |
| POST | `/workspaces/{id}/index/changes` | `{"paths": ["a.py"]}` | re-indexes files changed outside a run (422 for a path outside the workspace) |
| GET | `/workspaces/{id}/retrieve` | `?q=...&k=8` | read-only debug view of retrieval: `results` with path, lines, symbol, score, sources, reason, snippet (409 `INDEX_NOT_READY`) |
| GET | `/workspaces/{id}/events` | | SSE for index progress: `index_started`, `index_progress`, `index_finished`, `index_failed`, `index_cancelled`, `index_update_failed`; ends when no build is running |
| GET | `/workspaces/{id}/profile` | | detected project profile with the user's overrides applied: `profile`, `detected`, `overrides`, `granted_commands`, `approvable_commands`, `needs_approval`, `summary` |
| POST | `/workspaces/{id}/profile/detect` | `{"subproject": ""}` | re-detect (monorepo subfolder optional, 422 if it escapes the workspace); overrides are kept |
| PUT | `/workspaces/{id}/profile` | `{"overrides": {"test_commands": [...], "run_commands": [...], "lint_command": "..."}}` | stores the overrides (unknown fields and wrong types are dropped) |
| GET / POST | `/workspaces/{id}/commands` | POST `{"exe": "poetry"}` | the executables the user approved for this workspace; POST approves one name from `approvable` only (422 otherwise) |
| DELETE | `/workspaces/{id}/commands/{exe}` | | revokes an approval |
| GET / POST | `/workspaces/{id}/summary` | | cached project summary / (re)generate it with the planner's model (cached by a source hash; 503 `SUMMARY_UNAVAILABLE` without a model) |

`baseline: true` on `POST /runs` is the user's approval to run the project's test command (from the profile) once before the first
task and once after the last; new failing tests make the run `completed_with_failures` (event `regression_detected`).
`subproject` picks the monorepo folder whose test command is used.

`approval_level`: `ask_every_edit` \| `ask_risky` (default) \| `auto_within_limits`. `dirty_policy`: `checkpoint` (default) \| `abort`.
`dirty_files` are files with unsaved editor changes; the agent will not touch them.

Start-up conflicts return **409** with `{"detail": {"code", "message"}}`: `NOT_A_REPO` (offer `git init`, then repeat with
`"git_init": true`), `DIRTY_WORKSPACE`, `WORKSPACE_BUSY` (one active run per workspace), `CANNOT_RESUME`.
Calling `/plan` or `/approve` when the run is not waiting for that answer returns 409.

`state` in `GET /runs/{id}`: `running`, `awaiting_plan`, `awaiting_approval`, `finished`, or `idle` (not active in this process).
`status`: `running`, `completed`, `completed_with_failures`, `failed`, `cancelled`.

## SSE events

Each event has an `id` (1, 2, 3, ...), an `event` name and a JSON `data` payload.

```
event: plan_ready
data: {"plan": {"project_id": "p", "goal": "...", "steps": [...]}, "tasks": [...]}

event: task_started
data: {"task_id": 1, "title": "Build the API"}

event: tool_call
data: {"task_id": 1, "tool": "create_file", "args": "{'target_file': 'app.py', ...}"}

event: tool_result
data: {"task_id": 1, "tool": "create_file", "ok": true, "code": null, "summary": "Successfully created file: app.py"}
data: {"task_id": 1, "tool": "create_file", "ok": false, "code": "READ_REQUIRED", "rejected": true}

event: approval_required
data: {"type": "approval", "task_id": 1, "reason": "delete: old.txt", "files": ["old.txt"],
       "diffs": [{"file": "old.txt", "change_type": "deleted", "before": "...", "after": "", "diff": "--- a/old.txt\n...", "lines": 3}],
       "calls": [{"name": "delete_file", "args": {"target_file": "old.txt"}}]}

event: verification
data: {"task_id": 1, "ok": false, "error_type": "test_failure", "attempt": 1, "detail": "..."}

event: retrieval            data: {"task_id", "query", "results": [{"path", "lines", "score", "sources", "reason", "included"}], "included", "dropped_for_budget", "dropped_unsafe"}
event: retrieval_fallback   data: {"task_id", "reason"}      (v2 fell back to the basic context)
event: index_updated        data: {"task_id", "files"}       (also index_update_failed, index_refreshed)
event: baseline_recorded    data: {"command", "ran", "exit_code", "passed", "failed", "failing": [...]}   (or baseline_skipped {"reason"})
event: regression_detected  data: {"regressed", "new_failures", "fixed", "message"}   (or regression_check when nothing new failed)
event: planner_context      data: {"files", "hits", "has_summary"}                  (also summary_generated, summary_failed)

event: task_done      data: {"task_id": 1, "reason": null, "attempts": 0}
event: task_failed    data: {"task_id": 2, "reason": "VERIFICATION_FAILED: ...", "attempts": 4}
event: error          data: {"message": "RuntimeError: ..."}
event: run_finished   data: {"run_status": "completed_with_failures", "summary": {"done": [...], "failed": [...], "skipped": [...]}}
```

`approval_required` is sent when a change needs the user: deletes, renames, dependency manifests, config files, protected paths
(`yarn.lock`, `*.pem`, `.github/workflows/`, ...), package installs, changes over the size limits, or files modified outside
Nexura since it last read them. `.env*` and `.git/` are never writable and never reach the user as a question.
Answer with `POST /runs/{id}/approve`.

For a run that is not active in this server process, `GET /runs/{id}/events` replays the stored lifecycle events instead.
