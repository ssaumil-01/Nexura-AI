# V1 eval baseline

This is the baseline that V2 and V3 will be compared against. It was produced with the real model, against 14 tasks, using
checks written by us that are independent of the planner's own acceptance checks (`evals/tasks.yaml`, runner `evals/run.py`,
raw results in `evals/results/`).

**Read this first.** The last *complete* real-model run is **run 2: 10 of 14 tasks (71%)**. Several robustness fixes were made
after it (listed below); they are covered by unit tests but **could not be re-measured live**, because the Gemini free-tier daily
quota for the key in use ran out (see "Why there is no run 3"). Re-run `python evals/run.py` with a key that has quota before
using these numbers as the V2 reference, and replace the table.

## Setup

| | |
|---|---|
| Model (coder / debugger) | `gemini-3.1-flash-lite-preview` |
| Model (planner) | `gemini-3.1-flash-lite` |
| Approval level | `auto_within_limits`; plan and approvals auto-approved and counted |
| Environment | Windows 11, Python 3.13, a throwaway venv first on PATH for the agent's `python`/`pip`, Node 22 |
| Per-task limits | 15 tool calls per task, 3 retries per task, soft time limit 900 s |
| Pass criterion | all of the task's own `checks` pass after the run (not the agent's say-so) |

## Results

- **Run 1** (2026-11-17 00:56, commit 37b4df0, prompts planner 2 / coder 1): **6/14 passed (43%)**, 29 retries, 140 tool calls, 1 approval, 1343 s, 406k tokens.
- **Run 2** (2026-11-17 01:18, commit ecaa0bc, prompts planner 3 / coder 2): **10/14 passed (71%)**, 24 retries, 153 tool calls, 6 approvals, 1088 s, 423k tokens.

| task | run 1 | run 2 | run 2: run status | retries | tool calls | approvals | time (s) |
|---|---|---|---|---|---|---|---|
| calc_cli | PASS | PASS | completed | 2 | 19 | 1 | 128.6 |
| fastapi_users | FAIL | FAIL | completed_with_failures | 4 | 7 | 1 | 109.6 |
| flask_hello | FAIL | PASS | completed_with_failures | 4 | 11 | 1 | 103.8 |
| static_site | PASS | PASS | completed_with_failures | 4 | 11 | 0 | 47.4 |
| streamlit_app | FAIL | FAIL | completed_with_failures | 4 | 8 | 2 | 215.9 |
| algo_tests | FAIL | PASS | completed | 2 | 13 | 1 | 64.0 |
| node_script | PASS | PASS | completed | 0 | 6 | 0 | 15.8 |
| json_config | FAIL | PASS | completed | 0 | 15 | 0 | 106.9 |
| package_bugfix | FAIL | FAIL | failed | 0 | 0 | 0 | 6.7 |
| add_function | FAIL | PASS | completed | 0 | 14 | 0 | 90.7 |
| todo_cli | PASS | PASS | completed | 0 | 12 | 0 | 60.0 |
| csv_report | PASS | PASS | completed_with_failures | 4 | 12 | 0 | 44.6 |
| validators | FAIL | FAIL | completed | 0 | 17 | 0 | 63.5 |
| tests_for_existing | PASS | PASS | completed | 0 | 8 | 0 | 30.5 |

"Run status" is the pipeline's own verdict. `completed_with_failures` on a PASS means a task inside the run failed (or was skipped)
but the final result still met our independent checks; `completed` on a FAIL (validators) means the agent's own checks passed while ours did not.

## Failure analysis

**Run 1 (6/14).** Almost all failures were pipeline defects, not model weakness:

| Cause | Tasks hit | Fix (in run 2) |
|---|---|---|
| The command validator rejected `python -c "a; b"` because of the `;` inside quotes. Under `shell=False` a quoted `;` is just data. This also broke our own eval checks. | algo_tests, json_config, add_function, validators, package_bugfix | Operators are rejected only outside quotes (still rejected for `.cmd` launchers on Windows) |
| The model emitted `http_status` checks with only a `start_command` and no `url`; three retries repeated it and the whole run failed at planning. | fastapi_users, flask_hello, streamlit_app | URL inferred from the start command's port or the framework default |
| `python3` was not allow-listed. | json_config | `python3`/`pip3` accepted |
| Bare `pytest` does not put the project root on `sys.path`, so local packages failed to import; the debugger could not work out why. | package_bugfix | Prompts and the error hint say `python -m pytest` |

**Run 2 (10/14), the four remaining failures:**

| Task | What happened | Class |
|---|---|---|
| fastapi_users, streamlit_app | The planner used `pip install -r requirements.txt` as an *acceptance check*. Installs are only allowed through the approved install tool, so the check could never pass and the debugger looped on it until the task failed. | Planner quality plus a pipeline gap (a check that can never run should be caught at planning time) |
| package_bugfix | A Gemini `503 UNAVAILABLE: high demand` error during planning ended the run immediately; there was no retry for transient API errors. | Pipeline gap |
| validators | The model's `is_phone` rejected a valid number. The planner's checks and the agent's own tests were too weak to notice, so the run reported `completed` while our independent check failed. | Model/plan quality: a verified-but-wrong result. This is the main residual risk of acceptance-check verification |

Cost and effort for the passing runs: about 140-150 tool calls and ~420k tokens for 14 tasks, roughly 18-22 minutes of wall time.
Retries concentrate in the web-server tasks (4 each), where verification involves starting a server.

## What changed after run 2 (not re-measured live)

1. Planning-time validation of acceptance commands: `pip install`, `curl`, `cat`, shell operators etc. are sent back to the planner; after three bad
   answers the plan is *repaired* (only the unrunnable checks are dropped, with a visible warning) instead of failing the run.
2. The planner retries transient API errors (429, 5xx, "high demand", timeouts) with backoff.
3. Every model client now has an explicit request timeout (`NEXURA_LLM_TIMEOUT_S`, default 120 s). The Google client defaults were *no timeout and 6 internal retries*;
   a stalled connection hung one eval run for 36 minutes before this was found.
4. Daily quota exhaustion fails fast with `LLM_QUOTA` and a clear message instead of retrying for minutes.
5. Prompt versions: planner 5, coder 2, debugger 1. The eval runner has a hard per-task limit.

## Why there is no run 3

Run 3 was stopped after 36 minutes because of the missing-timeout hang above (item 3). Run 4 (with the fixes) started and then every request began
failing with `429 RESOURCE_EXHAUSTED ... Generate content requests per day limit exceeded on Free Tier` (500 requests per day per model on the key used,
"retry in about 10h"). Three full runs plus probes had used the daily allowance, so run 4 produced no usable numbers and its partial output was discarded.
Observations from the interrupted runs are consistent with the fixes (the plan-time validator correctly rejected `curl`, `cat` and `pip install` checks),
but they are anecdotes, not measurements.

## Caveats

- 14 tasks and one model: differences of one or two tasks are noise; model "high demand" periods change results.
- Four tasks need network access (package installs); the numbers depend on PyPI and the machine.
- The checks are ours and deterministic, but a few tasks (static_site, streamlit_app) only check structure, not behavior.
- Tokens are as reported by the API (input + output); tasks that fail early use few.

## Reproduce

```
cp .env.example .env                  # GOOGLE_API_KEY, CODER_MODEL_NAME, PLANNER_MODEL_NAME
python evals/run.py                   # all 14 tasks (about 20-30 min, ~420k tokens; free-tier keys may run out of daily quota)
python evals/run.py --only calc_cli   # one task
python evals/run.py --fake            # scripted model, 1 smoke task, no cost (this is what CI runs)
python evals/report.py evals/results/a.json evals/results/b.json   # tables for several runs
```
