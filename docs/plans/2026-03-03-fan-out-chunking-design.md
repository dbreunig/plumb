# Fan-Out Chunking for TestMapper and CodeCoverageMapper

## Problem

Both `TestMapper` and `CodeCoverageMapper` put the entire codebase context into a single LLM request. As the codebase grows, this overflows context limits and becomes slow/expensive.

## Approach: Fan-Out with Requirement Broadcasting

Chunk source/test summaries into groups that fit within a token budget. Broadcast all requirements to every chunk. Merge results across chunks.

- **Token budget:** 60k input tokens per chunk
- **Concurrency:** Concurrent execution via `ThreadPoolExecutor`
- **Location:** Shared utilities in `plumb/programs/__init__.py`

## Primitives (in `plumb/programs/__init__.py`)

### `estimate_tokens(text: str) -> int`

`len(text) // 4` heuristic (same as `conversation.py`).

### `chunk_items(items: list[tuple[str, str]], budget: int) -> list[list[tuple[str, str]]]`

- Takes `(key, text)` pairs (source file summaries or test summaries)
- Greedy bin-packing: add items to current chunk until budget exceeded, then start new chunk
- Single items exceeding budget get their own chunk (never dropped)

### `run_chunked_mapper(mapper, requirements_json, items, budget, combine_fn, merge_fn=None) -> list`

- Estimates token cost of `requirements_json`
- Sets item budget = `budget - req_tokens`
- Calls `chunk_items()` to partition
- If 1 chunk: direct call via `run_with_retries()` (no overhead)
- If multiple chunks: `ThreadPoolExecutor` fan-out, each calling `run_with_retries(mapper, req_json, combine_fn(chunk))`
- `combine_fn`: converts a chunk's items into the string format the mapper expects
- `merge_fn`: optional reducer for combining results across chunks (default: flatten)

## Merge Strategies

### CodeCoverageMapper

Every chunk sees all requirements but only some source files. A requirement marked `implemented=True` in any chunk wins (logical OR). Evidence strings are concatenated (deduplicated). False in all chunks stays False. Safe because a chunk can only produce false negatives, never false positives.

### TestMapper

Each test appears in exactly one chunk (no duplication). Results are simply concatenated across chunks. No merge logic needed.

## Integration

### CodeCoverageMapper (`coverage_reporter.py`)

Replace the single `run_with_retries(mapper, req_json, summaries_for_llm)` call with `run_chunked_mapper()`. Pass source summaries as `list[tuple[str, str]]` items instead of pre-combining them. Incremental caching logic is untouched — chunking happens inside the LLM call path, after dirty requirements are determined.

### TestMapper (`cli.py`)

Replace the single `run_with_retries(mapper, req_json, summaries_json)` call with `run_chunked_mapper()`. Each test summary becomes an item keyed by test name. `combine_fn` reassembles the JSON array per chunk.

## Key Properties

- Single-chunk case has zero overhead (detected and bypassed)
- Incremental caching in `coverage_reporter.py` unchanged
- DSPy signatures unchanged
- No changes to CLI UX or output format
