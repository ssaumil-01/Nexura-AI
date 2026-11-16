# Decisions

- 2026-11-16: V1 work branch `v1/hardening` created from `main` @ 6483046. Baseline: coder loop works but cap never triggers, always prints "Pipeline complete", server binds 0.0.0.0 with CORS *, tool paths via module globals.
- 2026-11-16: Owner said do NOT commit pre-existing `docs/*.md`; only files created by the agent (e.g. this one) are staged, by explicit path.
- 2026-11-16: Dev venv is `.venv/` inside the repo (git-ignored).
- 2026-11-16: Bug-reproduction tests use `xfail(strict=True)` until fixed.
