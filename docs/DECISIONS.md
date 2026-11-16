# Decisions

- 2026-11-16: V1 work branch `v1/hardening` created from `main` @ 6483046. Baseline: coder loop works but cap never triggers, always prints "Pipeline complete", server binds 0.0.0.0 with CORS *, tool paths via module globals.
- 2026-11-16: Owner said do NOT commit pre-existing `docs/*.md`; only files created by the agent (e.g. this one) are staged, by explicit path.
- 2026-11-16: Dev venv is `.venv/` inside the repo (git-ignored).
- 2026-11-16: Bug-reproduction tests use `xfail(strict=True)` until fixed.
- 2026-11-16: Tools receive `ProjectContext` via injected `config["configurable"]["project"]` (RunnableConfig param, hidden from the LLM schema); no module globals. execution_manager (Phase 3) will call tools with the same config.
- 2026-11-16: Tool errors are strings `ERROR: <CODE>: <message>`; batch_file_operations returns a JSON list of per-op results.
- 2026-11-16: run_command allows one allowlisted program (shell=False, bare executable names, no shell operators); `python -c` can still execute arbitrary Python (accepted until the V2 sandbox).
- 2026-11-16: `.env*` and `.git/` are never writable; other protected paths need `ctx.allow_protected` (approval flow comes in Phase 5).
- 2026-11-16: task_status/task_errors are keyed by str(step_id) (not int) so they survive JSON checkpoint round-trips.
- 2026-11-16: Acceptance is one flat pydantic model with a `type` enum and per-type required fields (Gemini-friendly response schema) instead of a discriminated union.
- 2026-11-16: Failure policy default is `stop` (config.FAILURE_POLICY / NEXURA_FAILURE_POLICY); run status: all done=completed, some done=completed_with_failures, none done=failed.
- 2026-11-16: Interactive tool approval is an explicit `approval` node using interrupt(); only yes/y/approve approves, other text rejects and is sent to the agent.
- 2026-11-16: Graph nodes name their LangGraph config parameter `config`; the config module is imported as `app_config` inside core/graph.py.
