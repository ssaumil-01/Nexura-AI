"""Decides what to do after a failed attempt on a task. Pure; the orchestrator owns the state."""
from dataclasses import dataclass

import config
from core.error_classifier import HINTS, classify


@dataclass
class RetryDecision:
    action: str      # retry | retry_with_error | switch_strategy | fail
    hint: str        # extra prompt text for the next attempt
    error_type: str


def decide(task: dict | None, attempt: int, error: str) -> RetryDecision:
    """attempt = number of failed attempts so far (1-based). 1: retry; 2: add error context; 3: switch strategy; >MAX: fail."""
    etype = classify(error)
    if attempt > config.MAX_RETRIES:
        return RetryDecision("fail", "", etype)
    if attempt == 1:
        return RetryDecision("retry", "", etype)
    ctx = f"Previous attempt failed.\nError output:\n{error[-1500:]}\nHint: {HINTS[etype]}"
    if attempt == 2:
        return RetryDecision("retry_with_error", ctx, etype)
    return RetryDecision("switch_strategy",
                         ctx + "\nThe previous approach is not working. Simplify: rewrite the failing file from scratch with the most straightforward implementation.",
                         etype)
