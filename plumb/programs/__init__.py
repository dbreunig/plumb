from __future__ import annotations

from pathlib import Path

import dspy
from dspy.adapters import XMLAdapter

from plumb import PlumbAuthError, PlumbInferenceError

_configured = False


def resolve_model(repo_root: str | Path | None = None) -> str:
    """The configured inference model (a litellm model string).

    Resolves the repo root when not given; a missing or unloadable config
    falls back to DEFAULT_MODEL."""
    from plumb.config import DEFAULT_MODEL, find_repo_root, load_config

    if repo_root is None:
        repo_root = find_repo_root()
    if repo_root is not None:
        cfg = load_config(repo_root)
        if cfg is not None and cfg.model:
            return cfg.model
    return DEFAULT_MODEL


def get_lm(repo_root: str | Path | None = None) -> dspy.LM:
    """Build the default LM from the configured model (a litellm model string)."""
    return dspy.LM(resolve_model(repo_root), max_tokens=28000)


def configure_dspy(repo_root: str | Path | None = None) -> None:
    """Lazy DSPy configuration. No-op if already configured.
    Never call at import time — a missing API key would break
    non-LLM commands like plumb status."""
    global _configured
    if _configured:
        return
    from dotenv import load_dotenv
    load_dotenv(override=False)
    lm = get_lm(repo_root)
    dspy.configure(lm=lm, adapter=XMLAdapter())
    _configured = True


def validate_api_access(
    repo_root: str | Path | None = None, model: str | None = None,
) -> None:
    """Check that the configured model's provider credentials are set and work.

    Loads .env first, then falls back to exported environment variables. Asks
    litellm which env vars the model's provider needs and names the exact
    missing one(s), then performs a smoke test to verify the credentials are
    valid. Raises PlumbAuthError if missing or invalid."""
    from dotenv import load_dotenv

    load_dotenv(override=False)

    explicit_model = model is not None
    if model is None:
        model = resolve_model(repo_root)

    import litellm  # lazy: importing litellm is slow

    env_check = litellm.validate_environment(model)
    if not env_check.get("keys_in_environment"):
        missing = env_check.get("missing_keys") or []
        if missing:
            names = ", ".join(missing)
            verb = "are" if len(missing) > 1 else "is"
        else:
            names, verb = "A required environment variable", "is"
        raise PlumbAuthError(
            f"{names} {verb} not set. Plumb is configured to use {model}. "
            f"Set it in a .env file at the repo root or export it."
        )

    # Smoke test: verify the credentials actually work
    lm = dspy.LM(model, max_tokens=28000) if explicit_model else get_lm(repo_root)
    try:
        response = lm("Reply with only the word: hello")
        if not response:
            raise PlumbAuthError("API returned empty response - key may be invalid")
    except Exception as e:
        err_str = str(e).lower()
        if "auth" in err_str or "api key" in err_str or "401" in err_str:
            raise PlumbAuthError(
                f"API key for {model} is invalid or rejected: {e}"
            ) from e
        raise PlumbAuthError(
            f"Failed to verify API access: {e}"
        ) from e


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
