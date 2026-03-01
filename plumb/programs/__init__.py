from __future__ import annotations

import dspy

from plumb import PlumbInferenceError

_configured = False


def get_lm() -> dspy.LM:
    return dspy.LM("anthropic/claude-sonnet-4-20250514", max_tokens=28000)


def configure_dspy() -> None:
    """Lazy DSPy configuration. No-op if already configured.
    Never call at import time — ANTHROPIC_API_KEY absence would break
    non-LLM commands like plumb status."""
    global _configured
    if _configured:
        return
    lm = get_lm()
    dspy.configure(lm=lm)
    _configured = True


def run_with_retries(fn, *args, max_retries: int = 2, **kwargs):
    """Call fn with retries. Raises PlumbInferenceError on final failure."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_error = e
    raise PlumbInferenceError(
        f"LLM inference failed after {max_retries + 1} attempts: {last_error}"
    )
