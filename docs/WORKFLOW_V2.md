# Nexura-AI Version 2: architecture and workflow

Version 2 keeps everything from Version 1 and adds what is needed for **projects that already exist**: a code search,
knowledge about the project, safer editing, and a "tests before and after" check.
Every new part is behind a switch. With the switches off, Nexura behaves exactly like Version 1.

Switches: `NEXURA_INDEXING_ENABLED=true`, `NEXURA_RETRIEVAL_MODE=v2`, `NEXURA_PATCH_MODE=v2`, plus `--baseline` per run.

## The architecture (the big picture)

```
 ┌─────────────── HOW YOU TALK TO NEXURA ────────────────┐
 │ Terminal: main.py        VS Code extension + server.py │   extension/src/project.ts: project panel, index progress,
 └───────────────────────────┬────────────────────────────┘   asks before model download / extra programs
                             v
 ┌──────────────── NEW IN V2: KNOW THE PROJECT (before planning) ───────────────────────────────┐
 │                                                                                              │
 │  core/project_profile.py   reads requirements.txt, package.json, imports ...                 │
 │                            -> language, framework, run command, test command                 │
 │                                                                                              │
 │  THE CODE INDEX (rag/), built in the background, stored OUTSIDE your project:                │
 │     rag/indexer.py      the librarian: which files are allowed (no secrets, no ignored files)│
 │     rag/chunker.py      cuts files into pieces (one function, one class)                     │
 │     rag/embedder.py     turns each piece into "meaning numbers" (small model, on your PC)    │
 │     rag/vector_store.py   drawer 1: search by MEANING   (Chroma)                             │
 │     rag/lexical_index.py  drawer 2: search by WORDS     (SQLite full-text)                   │
 │     rag/import_map.py   who uses whom ("app.py uses todo.py")                                │
 │                                                                                              │
 │  core/planner_context.py   project summary + profile + file list + best matching code        │
 └───────────────────────────┬──────────────────────────────────────────────────────────────────┘
                             v
 ┌──────────────── THE MANAGER: core/orchestrator.py ─────────────────────────────────────────────┐
 │  PLAN      agents/planner.py: plan must name REAL files; saved as version 1, 2, 3 ...           │
 │            (core/state_manager.py, plan_versions)     ──> YOU approve                           │
 │  BASELINE  core/baseline.py: run your tests once, remember who passed       (only with --baseline)│
 │                                                                                                  │
 │  FOR EACH TASK:                                                                                  │
 │    snapshot        core/checkpoints.py                                                           │
 │    SEARCH          rag/retriever.py: ask both drawers, mix the rankings (RRF), add hints        │
 │    what to show    core/context_builder.py: file list + named files in full + best pieces       │
 │                    ("reference only") + import hints; falls back to V1 if the index is not ready │
 │    suggest         agents/coder.py   (may use apply_patch)                                       │
 │    check rules     core/validator.py: V1 rules + TEST-APPLY the patch first                      │
 │    ask you         core/approvals.py: real diffs, approve or reject FILE BY FILE                 │
 │    do it           core/execution_manager.py -> core/execution_backend.py (runs commands)       │
 │                    core/patching.py: unique match, all-or-nothing, keeps line endings            │
 │                    -> changed files are re-indexed                                               │
 │    prove it        core/verification.py: the step's checks + Streamlit page render check         │
 │    fix if needed   retry manager + debugger (as in V1)                                           │
 │                                                                                                  │
 │  REGRESSION CHECK  run the tests again; a test that passed before and fails now = regression    │
 │  FINISH            completed / completed_with_failures (undo offered) / failed / cancelled       │
 └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## The workflow, in simple words

Imagine a new helper joining a team that already has a big codebase.
A good helper first **looks around**, then **plans**, then **edits carefully**, and **checks nothing else broke**.
Version 2 teaches Nexura to do exactly that.

1. **Look around (project profile).** Nexura reads well-known files and learns: "this is Python with Streamlit,
   you run it with `streamlit run app.py`, you test it with `python -m pytest`". This is plain code, not AI guessing.
2. **Make a catalogue (the index).** Like a library catalogue, Nexura cuts your code into pieces (one per function), and files
   each piece in two drawers: one finds pieces by **meaning** ("remove a task" finds `delete_task`), one by **exact words**
   (`filter_tasks`). Secret files and ignored files are never catalogued. The catalogue lives in Nexura's own folder.
3. **Plan with knowledge.** The planner sees a short summary of the project, the profile, the file list and the code that
   matches your request. It must name **real** files. A plan that names a file that does not exist is sent back.
   Every plan is kept as a numbered version.
4. **Tests before (optional).** If you choose `--baseline`, Nexura runs your tests once and writes down who passed.
5. **Search for each task.** For every step, the search asks both drawers and mixes the two lists. The AI is shown the files
   the step is about, plus the best matching pieces of other files, marked "reference only". It must still read a file before
   changing it. If the catalogue is missing or broken, Nexura quietly uses the Version 1 way.
6. **Edit carefully (apply_patch).** An edit is "find this text, put that text instead". The text must appear **exactly once**.
   Several edits in one file all apply, or none do. Windows/Linux line endings are kept. The validator tries the patch first,
   so a patch that cannot apply is refused before you are asked.
7. **Review file by file.** You see real before/after diffs and can accept some files and reject others.
8. **Prove it.** The step's checks run, as in Version 1. In Version 2 mode, a changed Streamlit page is also opened once,
   headless, to make sure it does not crash.
9. **Keep the catalogue fresh.** Every file Nexura changes is re-catalogued before the next step.
10. **Tests after.** At the end the tests run again. A test that passed before and fails now is a **regression**:
    the run is marked "completed with failures" and undo is offered. Tests that already failed before are not blamed.

**What never changes:** all Version 1 safety rules stay. Nothing secret is indexed, searched, logged or sent to an AI.
Indexing never changes your project and never connects to the internet. Out of scope for Version 2: Docker sandbox,
graph search, GitHub features, cloud embeddings.
