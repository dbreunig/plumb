# Fan-Out Chunking Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Chunk source/test summaries and broadcast requirements across multiple concurrent LLM calls so mappers don't overflow context.

**Architecture:** Add `estimate_tokens`, `chunk_items`, and `run_chunked_mapper` to `plumb/programs/__init__.py`. Replace the single `run_with_retries` calls in `coverage_reporter.py` and `cli.py` with `run_chunked_mapper`. CodeCoverageMapper gets an OR-merge for `implemented`; TestMapper gets flat concatenation.

**Tech Stack:** Python stdlib (`concurrent.futures.ThreadPoolExecutor`), existing DSPy/pydantic stack.

---

### Task 1: Add `estimate_tokens` and `chunk_items` to programs/__init__.py

**Files:**
- Modify: `plumb/programs/__init__.py:84` (append after `run_with_retries`)
- Test: `tests/test_chunking.py` (create)

**Step 1: Write the failing tests**

Create `tests/test_chunking.py`:

```python
from plumb.programs import estimate_tokens, chunk_items


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_short_string(self):
        assert estimate_tokens("hello world") == len("hello world") // 4

    def test_longer_string(self):
        text = "a" * 400
        assert estimate_tokens(text) == 100


class TestChunkItems:
    def test_single_item_under_budget(self):
        items = [("a.py", "short text")]
        chunks = chunk_items(items, budget=1000)
        assert len(chunks) == 1
        assert chunks[0] == items

    def test_two_items_fit_one_chunk(self):
        items = [("a.py", "x" * 40), ("b.py", "y" * 40)]
        # 40 chars = 10 tokens each, budget 100 tokens -> fits in one chunk
        chunks = chunk_items(items, budget=100)
        assert len(chunks) == 1

    def test_items_split_across_chunks(self):
        items = [("a.py", "x" * 400), ("b.py", "y" * 400), ("c.py", "z" * 400)]
        # 400 chars = 100 tokens each, budget 150 -> one per chunk
        chunks = chunk_items(items, budget=150)
        assert len(chunks) == 3
        assert chunks[0] == [items[0]]
        assert chunks[1] == [items[1]]
        assert chunks[2] == [items[2]]

    def test_oversized_item_gets_own_chunk(self):
        items = [("big.py", "x" * 4000), ("small.py", "y" * 40)]
        # big = 1000 tokens, budget = 500 -> big gets its own chunk, small gets its own
        chunks = chunk_items(items, budget=500)
        assert len(chunks) == 2
        assert chunks[0] == [items[0]]
        assert chunks[1] == [items[1]]

    def test_empty_items(self):
        chunks = chunk_items([], budget=1000)
        assert chunks == []

    def test_greedy_packing(self):
        items = [("a.py", "x" * 80), ("b.py", "y" * 80), ("c.py", "z" * 80)]
        # 80 chars = 20 tokens each, budget 50 -> a+b fit (40), c alone
        chunks = chunk_items(items, budget=50)
        assert len(chunks) == 2
        assert chunks[0] == [items[0], items[1]]
        assert chunks[1] == [items[2]]
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_chunking.py -v`
Expected: ImportError — `estimate_tokens` and `chunk_items` don't exist yet.

**Step 3: Write the implementation**

Append to `plumb/programs/__init__.py` after the `run_with_retries` function (after line 83):

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chunking.py -v`
Expected: All 8 tests PASS.

**Step 5: Commit**

```bash
git add plumb/programs/__init__.py tests/test_chunking.py
git commit -m "feat: add estimate_tokens and chunk_items primitives"
```

---

### Task 2: Add `run_chunked_mapper` to programs/__init__.py

**Files:**
- Modify: `plumb/programs/__init__.py` (append after `chunk_items`)
- Test: `tests/test_chunking.py` (extend)

**Step 1: Write the failing tests**

Append to `tests/test_chunking.py`:

```python
from unittest.mock import MagicMock, patch
from plumb.programs import run_chunked_mapper


class TestRunChunkedMapper:
    def test_single_chunk_no_threading(self):
        """When everything fits in one chunk, call mapper directly."""
        mapper = MagicMock()
        mapper.return_value = ["result_a", "result_b"]

        combine_fn = MagicMock(return_value="combined_text")
        req_json = '{"reqs": "short"}'
        items = [("a.py", "x" * 40)]

        with patch("plumb.programs.run_with_retries", return_value=["result_a", "result_b"]) as mock_retry:
            results = run_chunked_mapper(
                mapper, req_json, items, budget=60000,
                combine_fn=combine_fn,
            )

        assert results == ["result_a", "result_b"]
        mock_retry.assert_called_once()
        combine_fn.assert_called_once()

    def test_multiple_chunks_concurrent(self):
        """When items exceed budget, fan out across threads."""
        mapper = MagicMock()
        # Each chunk returns different results
        call_results = [["r1"], ["r2"]]

        combine_fn = lambda chunk: "\n".join(t for _, t in chunk)
        req_json = "reqs"
        # Two items that won't fit together: budget is tiny
        items = [("a.py", "x" * 400), ("b.py", "y" * 400)]

        with patch("plumb.programs.run_with_retries", side_effect=call_results):
            results = run_chunked_mapper(
                mapper, req_json, items, budget=200,
                combine_fn=combine_fn,
            )

        assert sorted(results) == ["r1", "r2"]

    def test_merge_fn_called(self):
        """Custom merge_fn is used to reduce results."""
        mapper = MagicMock()
        call_results = [["a", "b"], ["c"]]

        combine_fn = lambda chunk: "text"
        merge_fn = MagicMock(return_value=["merged"])
        req_json = "reqs"
        items = [("a.py", "x" * 400), ("b.py", "y" * 400)]

        with patch("plumb.programs.run_with_retries", side_effect=call_results):
            results = run_chunked_mapper(
                mapper, req_json, items, budget=200,
                combine_fn=combine_fn, merge_fn=merge_fn,
            )

        assert results == ["merged"]
        merge_fn.assert_called_once()
        # merge_fn receives the list of per-chunk result lists
        args = merge_fn.call_args[0][0]
        assert args == [["a", "b"], ["c"]]

    def test_empty_items_returns_empty(self):
        mapper = MagicMock()
        results = run_chunked_mapper(
            mapper, "reqs", [], budget=60000,
            combine_fn=lambda c: "",
        )
        assert results == []
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_chunking.py::TestRunChunkedMapper -v`
Expected: ImportError — `run_chunked_mapper` doesn't exist yet.

**Step 3: Write the implementation**

Append to `plumb/programs/__init__.py` after `chunk_items`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chunking.py -v`
Expected: All 12 tests PASS.

**Step 5: Commit**

```bash
git add plumb/programs/__init__.py tests/test_chunking.py
git commit -m "feat: add run_chunked_mapper with concurrent fan-out"
```

---

### Task 3: Add `merge_coverage_results` helper

**Files:**
- Modify: `plumb/coverage_reporter.py` (add helper function before `check_spec_to_code_coverage`)
- Test: `tests/test_chunking.py` (extend)

**Step 1: Write the failing tests**

Append to `tests/test_chunking.py`:

```python
from plumb.coverage_reporter import merge_coverage_results
from plumb.programs.code_coverage_mapper import RequirementCoverage


class TestMergeCoverageResults:
    def test_or_semantics(self):
        """implemented=True in any chunk wins."""
        chunk1 = [
            RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py:foo"),
            RequirementCoverage(requirement_id="req-bbb", implemented=False, evidence=""),
        ]
        chunk2 = [
            RequirementCoverage(requirement_id="req-aaa", implemented=False, evidence=""),
            RequirementCoverage(requirement_id="req-bbb", implemented=True, evidence="b.py:bar"),
        ]
        merged = merge_coverage_results([chunk1, chunk2])

        by_id = {r.requirement_id: r for r in merged}
        assert by_id["req-aaa"].implemented is True
        assert by_id["req-aaa"].evidence == "a.py:foo"
        assert by_id["req-bbb"].implemented is True
        assert by_id["req-bbb"].evidence == "b.py:bar"

    def test_false_in_all_stays_false(self):
        chunk1 = [RequirementCoverage(requirement_id="req-aaa", implemented=False, evidence="")]
        chunk2 = [RequirementCoverage(requirement_id="req-aaa", implemented=False, evidence="")]
        merged = merge_coverage_results([chunk1, chunk2])
        assert len(merged) == 1
        assert merged[0].implemented is False

    def test_evidence_dedup(self):
        """Same evidence from multiple chunks is not duplicated."""
        chunk1 = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py:foo")]
        chunk2 = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py:foo")]
        merged = merge_coverage_results([chunk1, chunk2])
        assert merged[0].evidence == "a.py:foo"

    def test_evidence_joined(self):
        """Different evidence strings are joined."""
        chunk1 = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py:foo")]
        chunk2 = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="b.py:bar")]
        merged = merge_coverage_results([chunk1, chunk2])
        assert "a.py:foo" in merged[0].evidence
        assert "b.py:bar" in merged[0].evidence

    def test_single_chunk_passthrough(self):
        chunk = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py")]
        merged = merge_coverage_results([chunk])
        assert merged == chunk
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_chunking.py::TestMergeCoverageResults -v`
Expected: ImportError — `merge_coverage_results` doesn't exist yet.

**Step 3: Write the implementation**

Add to `plumb/coverage_reporter.py` before `check_spec_to_code_coverage` (before line 187):

```python
def merge_coverage_results(
    per_chunk_results: list[list],
) -> list:
    """Merge CodeCoverageMapper results across chunks.

    OR semantics: implemented=True in any chunk wins.
    Evidence strings are joined (deduplicated).
    """
    from plumb.programs.code_coverage_mapper import RequirementCoverage

    if len(per_chunk_results) == 1:
        return per_chunk_results[0]

    by_id: dict[str, dict] = {}
    for chunk_results in per_chunk_results:
        for r in chunk_results:
            if r.requirement_id not in by_id:
                by_id[r.requirement_id] = {
                    "implemented": r.implemented,
                    "evidence_parts": [r.evidence] if r.evidence else [],
                }
            else:
                entry = by_id[r.requirement_id]
                if r.implemented:
                    entry["implemented"] = True
                if r.evidence and r.evidence not in entry["evidence_parts"]:
                    entry["evidence_parts"].append(r.evidence)

    return [
        RequirementCoverage(
            requirement_id=rid,
            implemented=data["implemented"],
            evidence="; ".join(data["evidence_parts"]),
        )
        for rid, data in by_id.items()
    ]
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chunking.py -v`
Expected: All 17 tests PASS.

**Step 5: Commit**

```bash
git add plumb/coverage_reporter.py tests/test_chunking.py
git commit -m "feat: add merge_coverage_results with OR semantics"
```

---

### Task 4: Integrate chunking into CodeCoverageMapper call path

**Files:**
- Modify: `plumb/coverage_reporter.py:288-314` (replace LLM call block)
- Test: `tests/test_coverage_reporter.py` (update existing mock)

**Step 1: Write the failing test**

Add to `tests/test_coverage_reporter.py` (new test method in the existing class):

```python
def test_use_llm_true_uses_chunked_mapper(self, initialized_repo):
    """Verify coverage mapping goes through run_chunked_mapper."""
    reqs = [
        {"id": "req-abc12345", "text": "Must do X"},
        {"id": "req-def45678", "text": "Must do Y"},
    ]
    req_path = initialized_repo / ".plumb" / "requirements.json"
    req_path.write_text(json.dumps(reqs))

    src = initialized_repo / "src"
    src.mkdir(exist_ok=True)
    (src / "main.py").write_text("def do_x():\n    '''Does X'''\n    pass\n")

    from plumb.programs.code_coverage_mapper import RequirementCoverage
    mock_results = [
        RequirementCoverage(requirement_id="req-abc12345", implemented=True, evidence="src/main.py:do_x"),
        RequirementCoverage(requirement_id="req-def45678", implemented=False, evidence=""),
    ]

    with patch("plumb.programs.configure_dspy"), \
         patch("plumb.programs.run_chunked_mapper", return_value=mock_results) as mock_chunked:
        covered, total = check_spec_to_code_coverage(initialized_repo, use_llm=True)

    assert covered == 1
    assert total == 2
    mock_chunked.assert_called_once()
    # Verify items are passed as list of tuples, not a pre-combined string
    call_kwargs = mock_chunked.call_args
    items_arg = call_kwargs[1]["items"] if "items" in call_kwargs[1] else call_kwargs[0][2]
    assert isinstance(items_arg, list)
    assert all(isinstance(item, tuple) and len(item) == 2 for item in items_arg)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_coverage_reporter.py::TestSpecToCodeCoverage::test_use_llm_true_uses_chunked_mapper -v`
Expected: FAIL — `run_chunked_mapper` not imported/used yet.

**Step 3: Write the implementation**

In `plumb/coverage_reporter.py`, replace lines 288-314:

Old code (lines 288-314):
```python
    # --- LLM mapping ---
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.code_coverage_mapper import CodeCoverageMapper

    configure_dspy()
    mapper = CodeCoverageMapper()

    if full_remap:
        # Full re-map: all requirements, all files
        dirty_reqs = requirements
        summaries_for_llm = _combine_summaries(per_file_summaries)
    else:
        # Incremental: only dirty requirements + changed/new file summaries
        dirty_reqs = [r for r in requirements if r["id"] in dirty_req_ids]
        affected_summaries = {
            f: per_file_summaries[f]
            for f in (changed_files | new_files)
            if f in per_file_summaries
        }
        summaries_for_llm = _combine_summaries(affected_summaries) if affected_summaries else ""

    req_json = json.dumps([{"id": r["id"], "text": r["text"]} for r in dirty_reqs])

    try:
        results = run_with_retries(mapper, req_json, summaries_for_llm)
    except Exception:
        return (0, len(requirements))
```

New code:
```python
    # --- LLM mapping ---
    from plumb.programs import configure_dspy, run_chunked_mapper
    from plumb.programs.code_coverage_mapper import CodeCoverageMapper

    configure_dspy()
    mapper = CodeCoverageMapper()

    if full_remap:
        dirty_reqs = requirements
        items = list(per_file_summaries.items())
    else:
        dirty_reqs = [r for r in requirements if r["id"] in dirty_req_ids]
        items = [
            (f, per_file_summaries[f])
            for f in (changed_files | new_files)
            if f in per_file_summaries
        ]

    req_json = json.dumps([{"id": r["id"], "text": r["text"]} for r in dirty_reqs])

    def _combine(chunk):
        return "\n\n".join(text for _, text in chunk)

    try:
        results = run_chunked_mapper(
            mapper, req_json, items, budget=60000,
            combine_fn=_combine, merge_fn=merge_coverage_results,
        )
    except Exception:
        return (0, len(requirements))
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_coverage_reporter.py -v`
Expected: All tests PASS. The existing `test_use_llm_true_calls_mapper` test that mocks `run_with_retries` will need its mock target updated to `run_chunked_mapper` — update the patch target in that test too.

**Step 5: Commit**

```bash
git add plumb/coverage_reporter.py tests/test_coverage_reporter.py
git commit -m "feat: integrate chunked mapper into CodeCoverageMapper path"
```

---

### Task 5: Integrate chunking into TestMapper call path

**Files:**
- Modify: `plumb/cli.py:733-743` (replace LLM call block)
- Test: `tests/test_chunking.py` (extend with integration test)

**Step 1: Write the failing test**

Append to `tests/test_chunking.py`:

```python
import json

class TestTestMapperChunking:
    def test_test_summaries_chunked(self):
        """Verify test summaries are passed as items to run_chunked_mapper."""
        from plumb.programs.test_mapper import TestMapping

        mock_results = [
            TestMapping(test_function="test_foo", file_path="tests/test_a.py",
                        requirement_ids=["req-aaa"], confidence=0.9),
        ]

        test_summaries = [
            {"file": "tests/test_a.py", "name": "test_foo", "docstring": "Tests foo", "preview": "def test_foo(): ..."},
            {"file": "tests/test_b.py", "name": "test_bar", "docstring": "Tests bar", "preview": "def test_bar(): ..."},
        ]

        # Verify the combine_fn produces valid JSON array
        items = [(s["name"], json.dumps(s)) for s in test_summaries]
        combine_fn = lambda chunk: json.dumps([json.loads(t) for _, t in chunk])
        result = combine_fn(items)
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert parsed[0]["name"] == "test_foo"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_chunking.py::TestTestMapperChunking -v`
Expected: PASS (this one tests the combine_fn logic only — it's a design verification).

**Step 3: Write the implementation**

In `plumb/cli.py`, replace lines 733-743:

Old code:
```python
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.test_mapper import TestMapper

    configure_dspy()
    mapper = TestMapper()

    req_json = json.dumps([{"id": r["id"], "text": r["text"]} for r in requirements])
    summaries_json = json.dumps(test_summaries)

    try:
        mappings = run_with_retries(mapper, req_json, summaries_json)
```

New code:
```python
    from plumb.programs import configure_dspy, run_chunked_mapper
    from plumb.programs.test_mapper import TestMapper

    configure_dspy()
    mapper = TestMapper()

    req_json = json.dumps([{"id": r["id"], "text": r["text"]} for r in requirements])
    items = [(s["name"], json.dumps(s)) for s in test_summaries]

    def _combine(chunk):
        return json.dumps([json.loads(t) for _, t in chunk])

    try:
        mappings = run_chunked_mapper(
            mapper, req_json, items, budget=60000, combine_fn=_combine,
        )
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chunking.py tests/test_cli.py -v`
Expected: All PASS.

**Step 5: Commit**

```bash
git add plumb/cli.py tests/test_chunking.py
git commit -m "feat: integrate chunked mapper into TestMapper path"
```

---

### Task 6: Update existing tests that mock `run_with_retries`

**Files:**
- Modify: `tests/test_coverage_reporter.py` (update mock targets)
- Modify: `tests/test_cli.py` (update mock targets if needed)

**Step 1: Find all tests mocking `run_with_retries` for mapper calls**

Search for `patch("plumb.programs.run_with_retries"` in test files. Update the mock target to `patch("plumb.programs.run_chunked_mapper"` where the mock is used for mapper calls (not for other DSPy programs that still use `run_with_retries`).

**Step 2: Update each mock**

In `tests/test_coverage_reporter.py`, the `test_use_llm_true_calls_mapper` test (line 286-302) patches `run_with_retries` — change to `run_chunked_mapper`.

Check `tests/test_cli.py` for any `map-tests` command tests that mock `run_with_retries`.

**Step 3: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All PASS.

**Step 4: Commit**

```bash
git add tests/test_coverage_reporter.py tests/test_cli.py
git commit -m "test: update mocks from run_with_retries to run_chunked_mapper"
```

---

### Task 7: Final verification

**Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All PASS.

**Step 2: Verify no regressions with a quick manual check**

Run: `python -m plumb status` (should use cached coverage, no LLM call)
Expected: Status output as before.

**Step 3: Commit any remaining changes**

If clean, no commit needed.
