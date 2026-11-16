# Nexura V1: threat model and controls

Nexura lets a language model propose file edits and commands against **your real project folder**. The model is not trusted:
it can be wrong, manipulated by text in your files or in web search results (prompt injection), or simply confused. Controls are
therefore enforced in code by the orchestrator, never by asking the model to behave.

## Assets

1. Files outside the opened folder, and secrets anywhere (`.env`, keys, tokens).
2. The integrity of your git history, branches and staging area.
3. Your unsaved and uncommitted work.
4. Your machine (processes, global Python/Node environments).
5. The local API and the model API key.

## What we defend against, and how

| Threat | Control | Where / test |
|---|---|---|
| Path traversal, absolute paths, drive switches, symlink escapes, `proj` vs `proj_evil` prefix tricks | `resolve_in_root` uses real paths + `commonpath`, rejects `..`, absolute, UNC and drive paths; used by every tool and the validator | `core/safety.py`, `tests/test_safety_paths.py` |
| Command injection / destructive commands | Single allowlisted executable, `shell=False`, shell operators (`| & ; < >`, backtick, `$(`, newlines) rejected **outside quotes** (inside quotes they are plain data, e.g. `python -c "a; b"`; for `.cmd` launchers on Windows they are rejected everywhere), no executable paths, blocklist bypasses gone; installs only via `install_dependency` (flags like `--index-url`, `--user`, `-g`, URLs rejected) | `parse_command`, `tests/test_commands.py` |
| Runaway or orphan processes | Bounded runner with timeouts; servers are managed processes killed as a tree at task end/shutdown; output truncated | `core/processes.py`, `tests/test_processes.py` |
| Model overwriting files it never read, or huge/binary writes | Read-before-write, create-vs-write rules, size and binary limits, one batch cap | validator + tools |
| Writing secrets or git internals | `.env*` and `.git/` are **never** writable (not even with approval); lockfiles, keys, `.github/workflows/`, `.vscode/` need explicit approval, at every approval level | `check_writable`, `tests/test_safeguards.py` |
| Leaking secrets to the model/logs | `.env*`, key files and ignored files are not read or added to context; secret patterns are redacted from file context, tool output and logs; the planner/coder never see `.env` | `redact_secrets`, `validate_ignored` |
| Losing or clobbering the user's work | Per-task git checkpoints from a temporary index (HEAD, branches and the user's index are never touched); exact undo that warns before overwriting later user edits; files with unsaved editor changes are untouchable; external modification during a run pauses for approval; one active run per workspace | `core/checkpoints.py`, `core/approvals.py` |
| Silent, unreviewed risky changes | Approval gate for deletes, renames, manifests/config, protected paths, installs, size limits; diffs are shown before anything is written; 3 rejections fail the task | `core/approvals.py` |
| Agent claiming success it did not achieve | `task_complete` is only a request; acceptance checks (written by the planner, run by the orchestrator) decide; all loops are bounded; failures set a non-success run status and CLI exit code | `core/verification.py`, `core/orchestrator.py` |
| Agent rewriting its own plan or state | Validator rejects writes to plan files and `.nexura/`; agents have no access to `state_manager`; plan files live outside the workspace | `validate_task_alignment`, `tests/test_persistence.py` |
| Untrusted workspace | `trusted=false` makes the validator and the execution manager reject every mutating action; the extension also declares `untrustedWorkspaces.supported: false` | `validate_trusted` |
| Another website or process using the local API | Binds to `127.0.0.1`; every request needs a per-session bearer token (constant-time compare); `Host` must be loopback (DNS-rebinding guard); no CORS headers; workspace roots are registered once and never read from later requests; ids are validated | `server.py`, `tests/test_api.py` |
| Webview abuse | Strict CSP with nonce and no inline styles, `localResourceRoots` limited to the bundle, every webview message validated, the webview never holds the token | `extension/src/sidebar.ts`, `protocol.ts` |
| API key exposure | Keys come from environment variables or VS Code SecretStorage; they are never logged, written to settings, or committed (`.env` is git-ignored) | |

## Known limits (be honest about these)

1. **No sandbox.** `python` and `node` can execute arbitrary code, so an allowlisted command such as `python -c` or a script the
   model wrote can do anything your user account can (read other folders, use the network). V1 reduces risk (approval for
   installs, no shell, tree-kill, confinement of the file tools) but does not contain it. A container sandbox is V2.
2. **Prompt injection** from file contents or web search is mitigated by the controls above, not eliminated: a malicious file
   could still persuade the model to propose harmful but *allowed* actions; risky ones need your approval.
3. **pip installs** target the first Python on PATH (the approval message names it), not an automatically created project venv.
4. **Redaction is pattern-based** (AWS, Google, GitHub, OpenAI-style, Groq, LangSmith keys, private key blocks, `key = value`
   assignments). Unusual secret formats may pass through; keep real secrets in `.env*`, which is never read.
5. **Checkpoints use git.** Files ignored by `.gitignore` are not checkpointed and therefore not undoable.
6. **A leaked key exists in this repository's old history** (committed `.env`, April 2026, present in `origin/main`). Rotate it;
   history was not rewritten.

## Reporting

This is a private project; contact the owner directly.
