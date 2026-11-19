# Nexura-AI Version 1: architecture and workflow

Version 1 is a coding helper for **new, small projects**. You describe what you want. Nexura plans it, builds it step by step,
checks every step for real, and can undo everything.

## The architecture (the big picture)

```
 ┌──────────────────────────── HOW YOU TALK TO NEXURA ─────────────────────────────┐
 │   Terminal: main.py                         VS Code extension (extension/, ui/)  │
 │        │                                         │  (secret token, your own PC)  │
 │        │                                    server.py  (local web server)        │
 └────────┼─────────────────────────────────────────┼───────────────────────────────┘
          └───────────────────┬─────────────────────┘
                              v
                     core/runner.py   starts a run: checks the folder, takes the lock
                              │
                              v
 ┌──────────────────── THE MANAGER: core/orchestrator.py (a fixed flowchart) ───────────────────┐
 │                                                                                               │
 │  PLAN       agents/planner.py ──> plan (steps + checks) ──> YOU approve (or give feedback)   │
 │                                                                                               │
 │  FOR EACH STEP ("task"):                                                                      │
 │    1. snapshot        core/checkpoints.py       hidden git snapshot (for undo)               │
 │    2. what to show    core/context_builder.py   files + history, within a size limit         │
 │    3. suggest         agents/coder.py           AI suggests actions ("create calc.py ...")   │
 │    4. check rules     core/validator.py         allowed? inside the folder? read first?      │
 │    5. ask you         core/approvals.py         only for risky actions (delete, install ...) │
 │    6. do it           core/execution_manager.py the ONLY part that touches files/commands    │
 │                          └─> tools/  (create_file, read_file, run_command, ...)              │
 │    7. prove it        core/verification.py      run the step's checks                        │
 │         passed ──> task done                                                                  │
 │         failed ──> core/retry_manager.py + core/error_classifier.py                          │
 │                       └─> agents/debugger.py suggests a fix ──> back to step 4 (max 3 tries) │
 │                                                                                               │
 │  FINISH     completed / completed_with_failures / failed / cancelled                         │
 └───────────────────────────────────────────────────────────────────────────────────────────────┘
          │                                   │
          v                                   v
 core/state_manager.py                   core/safety.py
 (memory: nexura.db, logs, resume)       (path fence, command list, secret hiding)
```

## The workflow, in simple words

Think of a **building site**. The AI models are **workers** who only *suggest*. The orchestrator is the **site manager** who
decides, does the work, and inspects it.

1. **You ask.** "Create calc.py. `python calc.py 2+3*4` must print 14."
2. **Start.** `runner.py` checks the folder is a git folder and that no other run is using it (a "lock").
3. **Plan.** The Planner AI writes a short list of steps. Each step comes with **checks** that prove it worked,
   for example "run `python calc.py 2+3*4` and expect 14". Nothing happens until you say yes.
4. **Snapshot.** Before each step, a hidden git snapshot is saved, so the step can be undone later.
5. **Suggest.** The Coder AI suggests actions, like "create calc.py with this code". It cannot do anything itself.
6. **Check the rules.** The Validator refuses anything unsafe: paths outside the folder, `.env` files, programs not on the
   short list, or changing a file the AI never read.
7. **Ask you (only if risky).** Deleting, installing packages or changing many files waits for your OK.
8. **Do it.** The Execution Manager is the only part that really writes files or runs commands.
9. **Prove it.** Verification runs the step's checks. The AI saying "I'm done" is not enough. Only passing checks count.
10. **Fix if needed.** If a check fails, the Debugger AI sees the exact error and suggests a small fix. This goes through steps
    6 to 9 again, at most 3 times. After that, the step is marked failed.
11. **Finish honestly.** The run ends as completed, completed with failures, failed, or cancelled.
12. **Remember.** Everything is written to a small database and a log file. You can continue a stopped run (`--resume`)
    or undo it (`--undo`).

**Main limit of Version 1:** in a big existing project it cannot *find* the right code (it shows the AI only some files, up to a
size limit), and its find-and-replace edits break easily.
