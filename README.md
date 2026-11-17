# Nexura AI (Version 1)

An orchestrator-driven coding agent. You describe what you want; Nexura **plans** it (you approve the plan), **builds** it step
by step, **validates** every action the model proposes, **executes** only through one controlled module, **verifies** each task
with objective checks, **debugs** failures with a bounded debugger, and **persists** its state so a run can resume. It edits
your real folder safely: every task is checkpointed in git, risky changes wait for your approval, and everything can be undone.

> Principle: **control over intelligence.** Agents only *propose*. The orchestrator decides, runs, verifies and records.

This is **V1**. Embeddings/retrieval (V2), code graphs and GitHub/CI/deploy features (V3) are deliberately **not** here; see
[docs/NEXURA_ROADMAP.md](docs/NEXURA_ROADMAP.md).

## How a run works

```
prompt -> Planner (structured plan + acceptance checks) -> you approve the plan
  for each task:   checkpoint (git ref) -> Coder proposes tool calls
                   -> validator (schema, paths, allowlist, read-before-write, protected/dirty/ignored files, trust)
                   -> [approval if risky] -> execution manager (the ONLY code that calls tools)
                   -> verification (the plan's acceptance checks)
                      pass -> task done
                      fail -> retry manager -> Debugger (fix only this failure) -> validate -> execute -> verify
                      exhausted -> task failed -> failure policy (stop, or skip dependents)
  end: run status = completed | completed_with_failures | failed | cancelled (the CLI exit code is non-zero unless completed)
```

Invariants (all covered by tests): agents never execute tools; every action is validated first; `task_complete` is only a
request (verification decides); only `state_manager` writes memory; agents cannot change the plan; everything is confined to
the workspace; every step is logged with prompt version and model; all loops are bounded; failures are never reported as success.

## Install

Requirements: **Python 3.11+**, **git**, Node 18+ (only for the VS Code extension and Node-based tasks), a Gemini or Groq API key.

```
python -m venv .venv
.venv\Scripts\activate            # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # then edit .env (never commit it)
```

`.env`:

| Variable | Meaning |
|---|---|
| `GOOGLE_API_KEY` / `GROQ_API_KEY` | Model provider key (Gemini models use Google; llama/mixtral/gemma names use Groq) |
| `PLANNER_MODEL_NAME`, `CODER_MODEL_NAME` | Model names |
| `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT` | Optional LangSmith tracing |
| `NEXURA_DATA_DIR` | Where the state DB, checkpoints and run logs go (default `./memory`) |
| `NEXURA_APPROVAL_LEVEL` | `ask_every_edit`, `ask_risky` (default) or `auto_within_limits` |
| `NEXURA_FAILURE_POLICY` | `stop` (default) or `continue` |
| `NEXURA_SHELL_HINT` | Text telling the model which shell/OS it is on |

## Use it

**VS Code (recommended):** build and install the extension, see [extension/README.md](extension/README.md).

**Command line** (the workspace is `workspace/<project>`; it must be a git repo, or pass `--git-init`):

```
python main.py --project calc --prompt "Create a CLI calculator in calc.py" --git-init
python main.py --project calc --prompt "..." --approval ask_every_edit     # confirm every edit
python main.py --resume <run_id>                                            # continue an unfinished run
python main.py --undo <run_id> [--task N] [--confirm]                       # restore files from the checkpoints
python main.py --metrics <run_id>                                           # success rate, retries, error types, time
```

Exit code is 0 only if every task was verified. Run folders (`plan.md` with live checkboxes, `plan.json`, `log.jsonl`) live in
`<data dir>/runs/<run_id>/`, not in your project.

**HTTP API:** `python server.py` with `NEXURA_TOKEN` set; see [docs/API.md](docs/API.md). Localhost only, bearer token required.

## Safety model

Summary below; full threat model in [docs/SECURITY.md](docs/SECURITY.md).

- **Confinement:** all paths resolve (real paths, no `..`, no symlink escapes, Windows drives handled) inside the opened folder.
- **Commands:** one allowlisted program (`python`, `pip`, `node`, `npm`, `npx`, `uvicorn`, `pytest`, `streamlit`, `flask`), no shell,
  no operators outside quotes; installs only through `install_dependency` and only with your approval; servers run as managed processes that are
  killed (whole tree) at task end.
- **Files:** read-before-write, size and binary limits, `.env*` and `.git/` never writable, lockfiles/keys/CI workflows need approval,
  files with unsaved editor changes are never touched, ignored and secret files never reach the model, credentials in output are redacted.
- **Git:** per-task checkpoints (temporary index; HEAD, branches and your staging area are never touched), exact undo, one active run per workspace.
- **Honesty:** a task is done only when its acceptance checks pass; failed work is reported as failed.

## Develop

```
pytest                                      # ~200 tests, fake LLM, no network or API cost
ruff check .                                # lint
python evals/run.py --fake                  # CI smoke eval (scripted model)
python evals/run.py                         # real-model evals (costs API credits; uses a throwaway venv for agent commands)
cd extension && npm run setup && npm test   # build + end-to-end VS Code smoke test
```

Layout: `agents/` (planner, coder, debugger, llm access), `core/` (orchestrator, validator, execution manager, verification,
approvals, checkpoints, state manager, runner, run manager), `tools/` (file, directory, system, verification, search tools),
`server.py` (API), `main.py` (CLI), `extension/` + `ui/` (VS Code extension and sidebar), `evals/`, `tests/`, `docs/`.
`rag/` is intentionally empty (V2/V3) and `tools/github_tool.py` is unused (V3).

## Known limits (V1)

- `python`/`node` are allowed, and they can run arbitrary code: V1 has an allowlist, not a sandbox (a container sandbox is V2).
- `pip install` goes to whichever Python is first on PATH; the approval prompt tells you which one.
- No repository indexing: large projects are summarised by path and first lines within a character budget.
- The old Tauri prototype lives in `archive/App/`. It is superseded by the VS Code extension and is not used by anything.
