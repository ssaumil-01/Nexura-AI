# Nexura architecture (V1 core + V2 code intelligence)

## Control plane (unchanged by V2)

```
prompt -> planner_node -> review_plan (you approve, or give feedback -> new plan version)
  task_picker (baseline once) -> coder -> validate -> [approval] -> execute -> verify
                                    ^                                  |
                                    +---- debugger <- retry_manager <--+  (bounded)
  advance_task -> next task ... -> last task: regression check -> run status
```

Invariants: agents only propose; only `execution_manager` calls tools (always through the validator first and an
`ExecutionBackend` for commands); only `state_manager` writes memory; agents cannot edit the plan; everything is confined to the
workspace.

## V2 pieces

```
                      +----------------- index (outside your project) -----------------+
 files -> discover ->|  chunker (tree-sitter: py/js/ts/tsx, line-window fallback)        |
 (ignore rules,       |     -> embedder (local ONNX, consent gate, sqlite vector cache)  |
  secret scan,        |     -> Chroma (vectors + metadata only)                          |
  size caps)          |     -> SQLite FTS5 (chunk text, lines, symbol) + import map      |
                      |  manifest (hashes, schema/chunker/model versions)  IndexManager  |
                      +--------------------------------+---------------------------------+
                                                       |
   RETRIEVAL_MODE=v2:  retriever = RRF(vector, FTS5) + path/symbol/named/changed-file boosts, de-duplicated, 1 chunk/file
        |                    |
        |                    +--> planner context (existing projects): summary + profile + file list + hits
        +--> context builder v2: file list, named files in full, "reference only" chunks, import hints
             (read-time safety re-check; any failure -> V1 context builder, event retrieval_fallback)

   existing projects:  project profile (deterministic detection) -> per-workspace command approvals
                       baseline test run (opt-in) ........................ regression check after the last task
                       PATCH_MODE=v2: apply_patch (unique match, atomic multi-hunk, keeps CRLF/BOM/encoding)
                       per-file approve/reject; plan versions in SQLite (plan_versions)
```

Modules:

| Area | Files |
|---|---|
| Index | `rag/chunker.py`, `embedder.py`, `vector_store.py`, `lexical_index.py`, `import_map.py`, `indexer.py` (Indexer, IndexManager), `retriever.py` |
| Context | `core/context_builder.py` (V1 builder, `build_task_context`, v2 builder), `core/planner_context.py` (summary, planner context) |
| Existing projects | `core/project_profile.py`, `core/baseline.py`, `core/patching.py`, `core/safety.py` (`parse_command(extra_allowed=...)`) |
| Execution seam | `core/execution_backend.py` (`ExecutionBackend`, `LocalBackend`) |
| State | `core/state_manager.py` (migration 3: `plan_versions`, `project_profile`, `repo_summary`, `workspace_commands`, `test_baselines`) |
| API / UI | `server.py`, `extension/src/project.ts` (open-project flow), `ui/src/App.tsx` |
| Evals | `evals/run.py` (`--retrieval-mode`, `--patch-mode`, `--repeat`), `compare.py`, `retrieval_eval.py`, `oracle_v2.py`, `fixtures/` |

## Flags and defaults

| Flag (env) | Default | Effect |
|---|---|---|
| `NEXURA_RETRIEVAL_MODE` | `v1` | `v2` = retrieval context + context-aware planner (falls back to V1 when there is no ready index) |
| `NEXURA_INDEXING_ENABLED` | `false` | builds/uses the local index (needs `requirements-index.txt`) |
| `NEXURA_PATCH_MODE` | `v1` | `v2` = `apply_patch` and the stricter `patch_file` |
| `NEXURA_ALLOW_MODEL_DOWNLOAD` | unset | consent for the one-time local embedding model download |

With the defaults the behaviour is V1's (a test asserts the V1 context byte for byte and that the index is never touched).

## What is deliberately not here

No graph retrieval (`rag/graph_rag.py` is empty), no GitHub/CI/deploy tools (`tools/github_tool.py` is unused), no cloud
embeddings, no `WorkspaceEdit`, no container sandbox (only the `ExecutionBackend` seam exists).
