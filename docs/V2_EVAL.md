# V2 evaluation (draft)

Status: **draft**. This file holds the retrieval measurements from Phase 3. The V1-vs-V2 comparison of whole runs with a real
model (Phase 6) will be added here later.

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
