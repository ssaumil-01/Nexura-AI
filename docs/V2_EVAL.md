# V2 evaluation (draft)

Status: **final for V2 sign-off, with a small sample** (see the limitations). Part 1: retrieval measurements (Phase 3). Part 2: V1-vs-V2
whole runs with a real model (Phase 6).

## Retrieval quality (Phase 3, V2-3.8)

**How it is measured.** `python evals/retrieval_eval.py` indexes the four fixture projects (`evals/fixtures/`) with the real
local model (`BAAI/bge-small-en-v1.5`, CPU) and runs the prompt of each of the 18 V2 tasks as a query. For each task,
`evals/retrieval_labels.yaml` lists by hand the files (and key symbols) retrieval *should* return.

- **recall@k**: share of the labelled files that appear among the files of the top k results (k = 8 chunks).
- **MRR**: 1 / rank of the first labelled file in the result list (1.0 = the first result is relevant).
- **symbol recall**: share of the labelled symbols (functions, classes, constants) that appear in the retrieved chunks.
- The 18 tasks are split into a **tuning half** and a **held-out half** (alternating by id). Settings were chosen on the
  tuning half only; the held-out half shows whether the choice generalises.

**Result (2026-11-18, k = 8, RRF_K = 60, at most 1 chunk per file):**

| retrieval | recall@k tune | recall@k held-out | recall@k all | MRR tune | MRR held-out | MRR all | symbol recall all |
|---|---|---|---|---|---|---|---|
| vector only | 1.0 | 1.0 | 1.0 | 1.0 | 0.926 | 0.963 | 1.0 |
| keyword only (FTS5) | 0.972 | 1.0 | 0.986 | 1.0 | 0.815 | 0.907 | 1.0 |
| **hybrid (RRF)** | 0.972 | 1.0 | 0.986 | 1.0 | 0.926 | 0.963 | 1.0 |

**Tuning (one pass, on the tuning half):**

- `RRF_K` in {10, 30, 60, 100} made no difference on this label set (lists are short); kept the standard 60.
- **Per-file cap.** Without a cap, hybrid filled its 8 slots with several chunks of the same few files (README windows, test
  files) and missed files: recall@k 0.944 (tune) / **0.863 (held-out)**, below keyword-only. In `bs_fix_bug` the file with
  the actual bug (`shop/discounts.py`) was pushed out. Caps tried on the tuning half: none 0.944, **1 chunk per file 0.972**,
  2 per file 0.944. With a cap of 1 the held-out recall rose to 1.0 and symbol recall stayed at 1.0 (no labelled function was
  lost), so the cap is 1 (`rag/retriever.py: MAX_PER_FILE`). Files named in the task are shown in full anyway, and the agent
  must `read_file` before editing, so one chunk per file is enough to point it at the right files.

**Done-criterion check (plan: hybrid recall@k >= the better of vector-only and keyword-only, or the gap documented).**
Hybrid equals keyword-only on recall and equals vector-only on MRR. It is **one file below vector-only** on recall in the
tuning half (task `inv_bulk_discount`: `inventory/config.py` is ranked 9th). Hybrid stays the V2 choice because the keyword
side makes exact names (`line_total`, `removeUser`) and paths reliable, which pure meaning search does not guarantee, and its
ranking (MRR) is the best of the three.

**Caveats (important).**

- The fixtures are small (4 to 13 indexable files). With k = 8, recall is close to its ceiling and does not separate the
  methods well. Phase 6 runs real tasks end to end, which is the measurement that matters.
- 18 labelled queries is a small sample; one task moves recall by about 0.03.
- Labels were written by the same person who wrote the fixtures; queries are the task prompts as given.

Raw results: `evals/results/retrieval_<date>.json` (per task: recall, reciprocal rank, symbol recall and the top files).

## V1 vs V2 whole runs with a real model (Phase 6, 2026-11-18)

**Setup.** `python evals/paired.py` runs the 18 V2 tasks (4 fixture projects) for two arms interleaved, task by task:
**V1** = `RETRIEVAL_MODE=v1` (the V1 context, V1 planner) and **V2** = `RETRIEVAL_MODE=v2` (index built before the run, retrieval
context, context-aware planner; patching stays v1 so only retrieval differs). Same model for planner and coder
(`gemini-3.5-flash-lite`, temperature 0.2), auto-approval, the harness's own checks and the fixture's own test suite decide success.
The local embedding model is `BAAI/bge-small-en-v1.5` (CPU). Raw results: `evals/results/2026-11-18_130606_{v1-v1,v2-v1}.json`
(local, not committed); tables from `evals/compare.py` and `evals/failures.py`.

**Sample actually obtained: 29 runs per arm** (all 18 tasks once, 11 tasks a second time), not the planned 54. The Gemini free
tier ran out of quota (the first attempt also exposed a bug: a per-minute token limit was treated as a daily quota; fixed, with a test).
One V2 run was cut short by that quota (counted as a failure in the table). The model `gemini-3.1-flash-lite-preview` from `.env`
was not usable for a full run on this key, hence `gemini-3.5-flash-lite` for both arms.

| | V1 | V2 |
|---|---|---|
| runs | 29 | 29 |
| success rate | 55% (16/29) | 62% (18/29) |
| success rate, first pass of all 18 tasks only | 78% (14/18) | 94% (17/18) |
| tokens per run, mean [min-max] | 88,993 [12,111-218,684] | 93,832 [12,180-217,174] |
| retries (total) | 35 | 14 |
| tool calls (total) | 319 | 375 |
| regressions introduced (fixture suite) | 0 | 0 |
| wall time (sum, s) | 3,507 | 4,848 |
| retrieval fallbacks | n/a | 0 |
| index build per run | n/a | 3.8 s mean (2.7-10.2 s; 13-file projects) |

Failure categories (`evals/failures.py`): final check failed (node) 4 vs 3; (python) 5 vs 6; verification failed 2 vs 1; tool cap 2 vs 0;
model quota 0 vs 1. Per-task: V2 succeeds where V1 failed in `inv_bulk_discount`, `jt_due_dates`, `jt_todo_file`; V1 succeeded where V2
did not in `fn_search`. Both fail `bs_fix_bug` in the one run each: the plan's first step required the project's failing test to pass
before the fix was made (a planner behaviour, unrelated to retrieval).

**Reading the numbers honestly.**
- The difference (62% vs 55%; 94% vs 78% on the first pass) is **not statistically meaningful** with 18-29 runs: single tasks flip
  between repeats (e.g. `fn_search`, `fn_count`, `jt_remove` pass once and fail once in both arms), so the run-to-run noise is as large as the effect.
- V2 used about 5% more tokens per run and 38% more wall time (indexing, summary and retrieval calls, longer prompts); retries were fewer (14 vs 35).
- No regressions in either arm, and V2 never needed its fallback.
- The fixtures are small (4-13 files), which is the setting where retrieval helps least; the benefit on large repositories is **unmeasured**.

**Default decision (V2-6.3).** V2 is at least as good as V1 on success and not worse on regressions, so the rule in the plan allows
proposing `RETRIEVAL_MODE=v2`. Recommendation: **keep `v1` as the default for now** and ask the owner to decide, because (a) the evidence is
a small sample with noise of the same size as the gain, (b) v2 needs an optional install and a model download, and (c) it costs more time per run.
Suggested next step if v2 should become the default: a 3-repeat run on a key with enough quota and at least one larger real repository.

**Tuning pass (V2-6.4).** None. The report shows no failure pattern attributable to retrieval (0 fallbacks, no "wrong file" failures:
the failing checks are logic/format mistakes in the model's code), so changing top-k, budgets or boosts would be tuning without a signal.

**Limitations.** Small sample and single model; free-tier throttling changes timing; the oracle and fake-model tests (which are
deterministic) cover the pipeline, not model quality; patching v2 (`apply_patch`) and the baseline/regression check were verified with
fake-model end-to-end tests but **not** compared live.
