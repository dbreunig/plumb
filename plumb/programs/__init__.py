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


def estimate_tokens(text: str) -> int:
    """Rough token count: 1 token per 4 characters."""
    return len(text) // 4


def chunk_items(
    items: list[tuple[str, str]], budget: int,
) -> list[list[tuple[str, str]]]:
    """Greedy bin-pack (key, text) pairs into chunks under a token budget.

    Items that exceed the budget on their own get a dedicated chunk (never dropped).
    """
    if not items:
        return []
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_tokens = 0
    for item in items:
        item_tokens = estimate_tokens(item[1])
        if current and current_tokens + item_tokens > budget:
            chunks.append(current)
            current = [item]
            current_tokens = item_tokens
        else:
            current.append(item)
            current_tokens += item_tokens
    if current:
        chunks.append(current)
    return chunks


def run_chunked_mapper(
    mapper,
    requirements_json: str,
    items: list[tuple[str, str]],
    budget: int,
    combine_fn,
    merge_fn=None,
) -> list:
    """Fan-out mapper calls across chunked items, broadcasting requirements.

    *combine_fn(chunk)* converts a chunk (list of (key, text) tuples) into the
    single string the mapper expects as its second argument.

    *merge_fn(list_of_result_lists)* reduces per-chunk results into a single
    list.  Default: flat concatenation.
    """
    if not items:
        return []

    req_tokens = estimate_tokens(requirements_json)
    item_budget = max(budget - req_tokens, 1)
    chunks = chunk_items(items, item_budget)

    if len(chunks) == 1:
        combined = combine_fn(chunks[0])
        return run_with_retries(mapper, requirements_json, combined)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _call_chunk(chunk):
        combined = combine_fn(chunk)
        return run_with_retries(mapper, requirements_json, combined)

    per_chunk_results: list[list] = [None] * len(chunks)
    with ThreadPoolExecutor() as executor:
        future_to_idx = {
            executor.submit(_call_chunk, chunk): i
            for i, chunk in enumerate(chunks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            per_chunk_results[idx] = future.result()

    if merge_fn is not None:
        return merge_fn(per_chunk_results)

    # Default: flatten
    merged: list = []
    for results in per_chunk_results:
        merged.extend(results)
    return merged
