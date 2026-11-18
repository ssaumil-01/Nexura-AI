# Nexura: threat model and controls (V1 controls, V2 additions at the end)

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
   installs, no shell, tree-kill, confinement of the file tools) but does not contain it. A container sandbox is planned for a later version.
2. **Prompt injection** from file contents or web search is mitigated by the controls above, not eliminated: a malicious file
   could still persuade the model to propose harmful but *allowed* actions; risky ones need your approval.
3. **pip installs** target the first Python on PATH (the approval message names it), not an automatically created project venv.
4. **Redaction is pattern-based** (AWS, Google, GitHub, OpenAI-style, Groq, LangSmith keys, private key blocks, `key = value`
   assignments). Unusual secret formats may pass through; keep real secrets in `.env*`, which is never read.
5. **Checkpoints use git.** Files ignored by `.gitignore` are not checkpointed and therefore not undoable.
6. **A leaked key exists in this repository's old history** (committed `.env`, April 2026, present in `origin/main`). Rotate it;
   history was not rewritten.

## V2 additions: code index, retrieval, existing projects

Everything V2 adds is behind flags that default to V1 behaviour (`NEXURA_RETRIEVAL_MODE=v1`, `NEXURA_INDEXING_ENABLED=false`,
`NEXURA_PATCH_MODE=v1`). The V1 controls above are unchanged; V2 adds these.

**What is indexed, where, and what never is.** With indexing on, source files of the opened folder are split into chunks and stored
in `<data dir>/index/<workspace id>/` (outside your project; a test compares the project tree before and after indexing):
a SQLite FTS5 file (chunk text, path, line range, symbol), a Chroma store (vectors and metadata only, never code text, telemetry
off), a manifest (file hashes, no code) and an import map. Never indexed: anything matched by `.gitignore`/`.nexuraignore`, the
default ignore list (`node_modules`, `.git`, `dist`, binaries, ...), `.env*`, key files, lockfiles and other protected paths,
files over the size cap, binary files, and every file in which the secret scan finds a secret-looking value (skipped entirely,
not stored redacted). `.nexuraignore` itself is not indexed. Embeddings are computed **locally** (ONNX, `BAAI/bge-small-en-v1.5`); no
code or query is sent anywhere. The one-time model download needs your consent (`NEXURA_ALLOW_MODEL_DOWNLOAD=1` / the extension's
prompt); without consent indexing fails with offline-install instructions instead of reaching the network.

| Threat | Control | Test |
|---|---|---|
| Retrieval or the debug endpoint reading outside the workspace | every path goes through `resolve_in_root`; stored paths are re-checked when read | `tests/test_indexer.py`, `tests/test_api_index.py` |
| Secrets or ignored content reaching the index, prompts, logs or API | skipped at indexing time **and** re-checked at read time (ignore rules, protected paths, secret scan, chunk still matches the file) before a chunk is shown to a model or returned by `/retrieve` | `tests/test_context_v2.py`, `tests/test_indexer.py` |
| A stale or poisoned index | a retrieved chunk is only a pointer: the agent must still `read_file` before editing (read-before-write is unchanged), and chunks are labelled "reference only" | `tests/test_context_v2.py` |
| Retrieval failure breaking a run | any index problem falls back to the V1 context builder and logs `retrieval_fallback` | `tests/test_context_v2.py`, `tests/test_index_hook.py` |
| More commands than the user agreed to | the default allowlist is untouched; a user may approve per workspace only `poetry, yarn, pnpm, make, uv, tox, pipenv` (revocable, stored per workspace); those cannot install/add/publish/fetch-and-run and runners (`poetry run X`) may only run default-allowlisted programs or linters; shell operators, path rules and approvals are unchanged; agents cannot grant anything | `tests/test_existing_project.py` |
| Patches editing the wrong place or half-applying | `apply_patch`: each search block must match exactly one place, all hunks apply or none, the validator dry-runs it before the user is asked, protected paths/dirty files/read-before-write/approvals behave exactly as for `write_file` | `tests/test_patching.py`, `tests/test_apply_patch.py` |
| Blaming (or hiding) regressions | the project's tests run before and after through the normal validator/execution path; only NEW failures count; the user opts in per run | `tests/test_existing_project.py`, `tests/test_existing_e2e.py` |
| Commands bypassing the validator | every command (including the baseline test run) goes validator -> execution manager -> `ExecutionBackend`; a contract test uses a fake backend that records calls | `tests/test_execution_backend_contract.py` |
| Absolute paths in logs/events/tool output | the workspace root is rewritten to relative paths before output is stored or shown | `tests/test_relative_paths.py` |
| New endpoints used without the token | every new endpoint needs the bearer token | `tests/test_api_existing.py`, `tests/test_api_index.py` |

V2 limits: the repository summary is written by the planner's model from filtered text (README, manifests, file names) and is stored
in the state database; it is secret-filtered but pattern-based like all redaction here. There is still **no sandbox** (the
`ExecutionBackend` interface exists so one can be added later; only the local backend is implemented). Undo is byte-exact; where a
machine's `core.autocrlf` setting would change line endings, the stored bytes are restored instead (see DECISIONS.md).

## Reporting

This is a private project; contact the owner directly.
