from __future__ import annotations

import json as _json
import os
import shutil
import subprocess
from pathlib import Path

import dspy
from dspy.adapters import XMLAdapter

from plumb import PlumbAuthError, PlumbInferenceError

_configured = False

# Default model for direct API access
_DEFAULT_API_MODEL = "anthropic/claude-sonnet-4-20250514"
# Default model alias for Claude Code CLI fallback
_DEFAULT_CLI_MODEL = "sonnet"


class ClaudeCodeLM(dspy.LM):
    """DSPy LM backend that routes through the Claude Code CLI.

    This enables Plumb to work for users on Claude Max/Pro plans who
    authenticate via OAuth through Claude Code, without needing a separate
    ``ANTHROPIC_API_KEY``.

    Calls ``claude -p --model <model> --output-format json
    --no-session-persistence`` as a subprocess for each inference request.
    """

    def __init__(self, model: str = _DEFAULT_CLI_MODEL, max_tokens: int = 28000, **kwargs):
        self.cli_model = model
        self._max_tokens = max_tokens
        super().__init__(model=f"claude-code/{model}", model_type="chat", **kwargs)

    def forward(self, prompt=None, messages=None, **kwargs):
        # Build prompt text from either a raw string or a messages list
        if prompt is not None:
            prompt_text = prompt if isinstance(prompt, str) else str(prompt)
        elif messages:
            parts = []
            for msg in messages:
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            c.get("text", str(c)) if isinstance(c, dict) else str(c)
                            for c in content
                        )
                    parts.append(content)
                else:
                    parts.append(str(msg))
            prompt_text = "\n\n".join(parts)
        else:
            prompt_text = ""

        try:
            result = subprocess.run(
                [
                    "claude", "-p",
                    "--model", self.cli_model,
                    "--output-format", "json",
                    "--no-session-persistence",
                ],
                input=prompt_text,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as e:
            raise PlumbInferenceError(f"Claude Code CLI timed out: {e}") from e
        except FileNotFoundError:
            raise PlumbAuthError(
                "Claude Code CLI ('claude') not found on PATH. "
                "Install it from https://claude.ai/code or set ANTHROPIC_API_KEY instead."
            )

        if result.returncode != 0:
            raise PlumbInferenceError(
                f"Claude Code CLI exited {result.returncode}: {result.stderr[:500]}"
            )

        # Parse JSON output — array of event objects; text is in the final
        # "result" event.
        try:
            events = _json.loads(result.stdout)
            if isinstance(events, list):
                for event in reversed(events):
                    if isinstance(event, dict) and event.get("type") == "result":
                        return [event.get("result", "")]
            return [result.stdout]
        except _json.JSONDecodeError:
            return [result.stdout]

    def __call__(self, prompt=None, messages=None, **kwargs):
        return self.forward(prompt=prompt, messages=messages, **kwargs)


def _claude_code_available() -> bool:
    """Return True if the ``claude`` CLI is installed and runnable."""
    return shutil.which("claude") is not None


def get_lm() -> dspy.LM:
    """Return the best available LM backend.

    Resolution order:
    1. ``ANTHROPIC_API_KEY`` is set → direct Anthropic API via LiteLLM (fast).
    2. ``claude`` CLI is on PATH → Claude Code CLI fallback (works with
       Max/Pro plan OAuth, no API key needed).
    3. Neither available → raise ``PlumbAuthError``.
    """
    from dotenv import load_dotenv
    load_dotenv(override=False)

    if os.environ.get("ANTHROPIC_API_KEY"):
        return dspy.LM(_DEFAULT_API_MODEL, max_tokens=28000)

    if _claude_code_available():
        return ClaudeCodeLM(model=_DEFAULT_CLI_MODEL, max_tokens=28000)

    raise PlumbAuthError(
        "No LLM backend available. Plumb needs one of:\n"
        "  1. ANTHROPIC_API_KEY set in environment or .env file, OR\n"
        "  2. Claude Code CLI installed (https://claude.ai/code) with an active session.\n"
        "Set ANTHROPIC_API_KEY or install Claude Code to continue."
    )


def configure_dspy() -> None:
    """Lazy DSPy configuration. No-op if already configured.
    Never call at import time — missing credentials would break
    non-LLM commands like ``plumb status``."""
    global _configured
    if _configured:
        return
    lm = get_lm()
    dspy.configure(lm=lm, adapter=XMLAdapter())
    _configured = True


def validate_api_access() -> None:
    """Verify that a working LLM backend is available.

    Checks for ``ANTHROPIC_API_KEY`` first, then falls back to the Claude
    Code CLI. Performs a smoke test to confirm the backend actually works.
    Raises ``PlumbAuthError`` if neither is available or functional.
    """
    from dotenv import load_dotenv
    load_dotenv(override=False)

    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_cli = _claude_code_available()

    if not has_api_key and not has_cli:
        raise PlumbAuthError(
            "No LLM backend available. Plumb needs one of:\n"
            "  1. ANTHROPIC_API_KEY set in environment or .env file, OR\n"
            "  2. Claude Code CLI installed (https://claude.ai/code) with an active session."
        )

    # Smoke test whichever backend we'll use
    lm = get_lm()
    try:
        response = lm("Reply with only the word: hello")
        if not response or not str(response[0]).strip():
            raise PlumbAuthError("LLM backend returned empty response")
    except PlumbAuthError:
        raise
    except Exception as e:
        err_str = str(e).lower()
        if "auth" in err_str or "api key" in err_str or "401" in err_str:
            raise PlumbAuthError(
                f"API key is invalid or rejected: {e}"
            ) from e
        raise PlumbAuthError(
            f"Failed to verify LLM access: {e}"
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
