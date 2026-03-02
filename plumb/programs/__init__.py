from __future__ import annotations

import os
from pathlib import Path

import dspy
from dspy.adapters import XMLAdapter

from plumb import PlumbAuthError, PlumbInferenceError

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
    from dotenv import load_dotenv
    load_dotenv(override=False)
    lm = get_lm()
    dspy.configure(lm=lm, adapter=XMLAdapter())
    _configured = True


def validate_api_access() -> None:
    """Check that ANTHROPIC_API_KEY is set. Loads .env first, then falls back
    to exported environment variables. Raises PlumbAuthError if not found."""
    from dotenv import load_dotenv

    load_dotenv(override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise PlumbAuthError(
            "ANTHROPIC_API_KEY is not set. "
            "Plumb requires a valid Anthropic API key to analyze commits.\n"
            "Set it in a .env file or export it: export ANTHROPIC_API_KEY=your-key-here"
        )


def get_program_lm(program_name: str, repo_root: str | Path | None = None) -> dspy.LM | None:
    """Return a per-program LM override from config, or None for the default."""
    from plumb.config import find_repo_root, load_config

    if repo_root is None:
        repo_root = find_repo_root()
    if repo_root is None:
        return None
    cfg = load_config(repo_root)
    if cfg is None:
        return None
    entry = cfg.program_models.get(program_name)
    if entry is None:
        return None
    model = entry.get("model")
    if not model:
        return None
    max_tokens = entry.get("max_tokens", 8192)
    return dspy.LM(model, max_tokens=max_tokens)


def run_with_retries(fn, *args, max_retries: int = 2, **kwargs):
    """Call fn with retries. Raises PlumbAuthError for auth failures,
    PlumbInferenceError on other final failures."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err_str = str(e)
            if "AuthenticationError" in err_str or "API Key" in err_str:
                raise PlumbAuthError(
                    f"API key is invalid or rejected: {e}"
                ) from e
            last_error = e
    raise PlumbInferenceError(
        f"LLM inference failed after {max_retries + 1} attempts: {last_error}"
    )
