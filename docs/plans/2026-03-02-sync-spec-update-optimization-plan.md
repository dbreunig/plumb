# Sync Spec Update Optimization — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce LLM calls during `plumb sync` from N-per-section to 1-per-spec-file by sending the whole spec as input and getting back structured section edits.

**Architecture:** Replace the per-section `BatchSpecUpdater` loop in `sync_decisions()` with a single `WholeFileSpecUpdater` call per spec file that returns section-keyed edits. When new sections are needed, a cheap second `OutlineMerger` call determines placement. All output uses JSON-in-string fields for XMLAdapter compatibility.

**Tech Stack:** DSPy (signatures/modules), Pydantic (for parsing), pytest (tests)

---

### Task 1: Add markdown helpers to sync.py

These are pure functions with no LLM dependency — easy to test in isolation.

**Files:**
- Modify: `plumb/sync.py`
- Create: `tests/test_sync_helpers.py`

**Step 1: Write failing tests for `extract_outline`**

```python
# tests/test_sync_helpers.py
from plumb.sync import extract_outline, apply_section_updates, insert_new_sections


class TestExtractOutline:
    def test_extracts_headers(self):
        content = "# Title\n\nIntro.\n\n## Auth\n\nLogin.\n\n### Tokens\n\nJWT.\n"
        assert extract_outline(content) == ["# Title", "## Auth", "### Tokens"]

    def test_empty_content(self):
        assert extract_outline("") == []

    def test_no_headers(self):
        assert extract_outline("Just some text.\nAnother line.") == []
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync_helpers.py::TestExtractOutline -v`
Expected: FAIL with ImportError (function doesn't exist yet)

**Step 3: Implement `extract_outline` in sync.py**

Add after the `find_spec_section` function (line 77):

```python
def extract_outline(content: str) -> list[str]:
    """Extract markdown headers from content, preserving order."""
    return [line for line in content.split("\n") if re.match(r"^#{1,6}\s", line)]
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync_helpers.py::TestExtractOutline -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/sync.py tests/test_sync_helpers.py
git commit -m "feat: add extract_outline helper to sync"
```

---

### Task 2: Add `apply_section_updates` helper

**Files:**
- Modify: `plumb/sync.py`
- Modify: `tests/test_sync_helpers.py`

**Step 1: Write failing tests**

Append to `tests/test_sync_helpers.py`:

```python
class TestApplySectionUpdates:
    def test_replaces_matching_section(self):
        content = "# Title\n\nIntro.\n\n## Auth\n\nOld auth text.\n\n## API\n\nEndpoints.\n"
        updates = [{"header": "## Auth", "content": "New auth text with tokens.\n"}]
        result = apply_section_updates(content, updates)
        assert "New auth text with tokens." in result
        assert "Old auth text." not in result
        assert "Endpoints." in result  # untouched

    def test_no_updates(self):
        content = "# Title\n\nStuff.\n"
        result = apply_section_updates(content, [])
        assert result == content

    def test_normalized_header_match(self):
        """LLM might return slightly different whitespace."""
        content = "# Title\n\n##  Auth \n\nOld text.\n"
        updates = [{"header": "## Auth", "content": "New text.\n"}]
        result = apply_section_updates(content, updates)
        assert "New text." in result

    def test_multiple_updates(self):
        content = "# Title\n\nIntro.\n\n## A\n\nOld A.\n\n## B\n\nOld B.\n"
        updates = [
            {"header": "## A", "content": "New A.\n"},
            {"header": "## B", "content": "New B.\n"},
        ]
        result = apply_section_updates(content, updates)
        assert "New A." in result
        assert "New B." in result
        assert "Old A." not in result
        assert "Old B." not in result
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync_helpers.py::TestApplySectionUpdates -v`
Expected: FAIL with ImportError

**Step 3: Implement `apply_section_updates`**

Add after `extract_outline` in `plumb/sync.py`:

```python
def _normalize_header(header: str) -> str:
    """Normalize a markdown header for comparison: lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", header.strip().lower())


def _parse_sections(content: str) -> list[tuple[str, str]]:
    """Parse markdown into [(header_line, body_text), ...].
    Content before the first header gets header=""."""
    sections: list[tuple[str, str]] = []
    current_header = ""
    current_lines: list[str] = []

    for line in content.split("\n"):
        if re.match(r"^#{1,6}\s", line):
            sections.append((current_header, "\n".join(current_lines)))
            current_header = line
            current_lines = []
        else:
            current_lines.append(line)

    sections.append((current_header, "\n".join(current_lines)))
    return sections


def apply_section_updates(content: str, updates: list[dict]) -> str:
    """Apply section edits by matching headers. Returns updated content.
    Each update is {"header": "## X", "content": "new body text"}."""
    if not updates:
        return content

    # Build lookup: normalized_header -> new_content
    update_map: dict[str, str] = {}
    for u in updates:
        update_map[_normalize_header(u["header"])] = u["content"]

    sections = _parse_sections(content)
    result_parts: list[str] = []

    for header, body in sections:
        norm = _normalize_header(header)
        if norm in update_map:
            new_body = update_map[norm]
            # Ensure body starts with a blank line after header
            if new_body and not new_body.startswith("\n"):
                new_body = "\n" + new_body
            result_parts.append(header + new_body)
        else:
            if header:
                result_parts.append(header + body)
            else:
                result_parts.append(body)

    return "\n".join(result_parts)
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync_helpers.py::TestApplySectionUpdates -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/sync.py tests/test_sync_helpers.py
git commit -m "feat: add apply_section_updates helper"
```

---

### Task 3: Add `insert_new_sections` helper

**Files:**
- Modify: `plumb/sync.py`
- Modify: `tests/test_sync_helpers.py`

**Step 1: Write failing tests**

Append to `tests/test_sync_helpers.py`:

```python
class TestInsertNewSections:
    def test_inserts_after_anchor(self):
        content = "# Title\n\nIntro.\n\n## Auth\n\nLogin.\n\n## API\n\nEndpoints.\n"
        new_sections = [{"header": "## Cache", "content": "Redis cache.\n"}]
        merged_outline = ["# Title", "## Auth", "## Cache", "## API"]
        result = insert_new_sections(content, new_sections, merged_outline)
        # Cache should appear between Auth and API
        auth_pos = result.index("## Auth")
        cache_pos = result.index("## Cache")
        api_pos = result.index("## API")
        assert auth_pos < cache_pos < api_pos

    def test_inserts_at_end(self):
        content = "# Title\n\nIntro.\n\n## Auth\n\nLogin.\n"
        new_sections = [{"header": "## API", "content": "Endpoints.\n"}]
        merged_outline = ["# Title", "## Auth", "## API"]
        result = insert_new_sections(content, new_sections, merged_outline)
        assert "## API" in result
        assert result.index("## Auth") < result.index("## API")

    def test_no_new_sections(self):
        content = "# Title\n\nStuff.\n"
        result = insert_new_sections(content, [], ["# Title"])
        assert result == content
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync_helpers.py::TestInsertNewSections -v`
Expected: FAIL with ImportError

**Step 3: Implement `insert_new_sections`**

Add after `apply_section_updates` in `plumb/sync.py`:

```python
def insert_new_sections(
    content: str, new_sections: list[dict], merged_outline: list[str]
) -> str:
    """Insert new sections into content at positions determined by merged_outline.
    Each new_section is {"header": "## X", "content": "body text"}.
    merged_outline is the full desired header order including existing + new."""
    if not new_sections:
        return content

    new_headers = {_normalize_header(s["header"]) for s in new_sections}
    new_by_header = {_normalize_header(s["header"]): s for s in new_sections}

    sections = _parse_sections(content)

    # Build ordered list of (header, body) from merged outline
    existing_by_norm = {}
    for header, body in sections:
        if header:
            existing_by_norm[_normalize_header(header)] = (header, body)

    result_parts: list[str] = []

    # Add any preamble (content before first header)
    for header, body in sections:
        if not header:
            result_parts.append(body)
            break

    for outline_header in merged_outline:
        norm = _normalize_header(outline_header)
        if norm in new_headers:
            s = new_by_header[norm]
            section_content = s["content"]
            if section_content and not section_content.startswith("\n"):
                section_content = "\n" + section_content
            result_parts.append(s["header"] + section_content)
        elif norm in existing_by_norm:
            header, body = existing_by_norm[norm]
            result_parts.append(header + body)

    return "\n".join(result_parts)
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync_helpers.py::TestInsertNewSections -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/sync.py tests/test_sync_helpers.py
git commit -m "feat: add insert_new_sections helper"
```

---

### Task 4: Add WholeFileSpecUpdater DSPy module

**Files:**
- Modify: `plumb/programs/spec_updater.py`
- Create: `tests/test_spec_updater.py`

**Step 1: Write failing test**

```python
# tests/test_spec_updater.py
import json
from unittest.mock import patch, MagicMock

from plumb.programs.spec_updater import WholeFileSpecUpdater


class TestWholeFileSpecUpdater:
    def test_parses_json_output(self):
        """The module should parse JSON strings from LLM into dicts."""
        mock_prediction = MagicMock()
        mock_prediction.section_updates_json = json.dumps([
            {"header": "## Auth", "content": "Updated auth section."}
        ])
        mock_prediction.new_sections_json = json.dumps([])

        with patch("plumb.programs.spec_updater.dspy.Predict") as MockPredict:
            mock_predict_instance = MagicMock(return_value=mock_prediction)
            MockPredict.return_value = mock_predict_instance

            updater = WholeFileSpecUpdater()
            section_updates, new_sections = updater(
                spec_content="# Spec\n\n## Auth\n\nOld text.\n",
                decisions_text="1. Q: How to auth?\n   A: Use JWT.\n",
            )

        assert len(section_updates) == 1
        assert section_updates[0]["header"] == "## Auth"
        assert new_sections == []

    def test_handles_new_sections(self):
        mock_prediction = MagicMock()
        mock_prediction.section_updates_json = json.dumps([])
        mock_prediction.new_sections_json = json.dumps([
            {"header": "## Cache", "content": "Use Redis for caching."}
        ])

        with patch("plumb.programs.spec_updater.dspy.Predict") as MockPredict:
            mock_predict_instance = MagicMock(return_value=mock_prediction)
            MockPredict.return_value = mock_predict_instance

            updater = WholeFileSpecUpdater()
            section_updates, new_sections = updater(
                spec_content="# Spec\n\n## Auth\n\nLogin.\n",
                decisions_text="1. Q: Caching?\n   A: Use Redis.\n",
            )

        assert section_updates == []
        assert len(new_sections) == 1
        assert new_sections[0]["header"] == "## Cache"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_spec_updater.py -v`
Expected: FAIL with ImportError

**Step 3: Implement WholeFileSpecUpdater**

Add to `plumb/programs/spec_updater.py`:

```python
import json


class WholeFileSpecUpdaterSignature(dspy.Signature):
    """Update a markdown spec to incorporate approved decisions.

    For EXISTING sections that need changes, return them in section_updates_json
    as a JSON array of {"header": "exact header line", "content": "new body"}.

    For BRAND NEW sections (not in the current spec), return them in
    new_sections_json as a JSON array of {"header": "## New Header", "content": "body"}.

    Rules:
    - Only include sections that need changes — omit unchanged sections
    - Use the EXACT header text from the spec for existing sections
    - Capture decisions as natural requirements — do not reference decisions
    - Preserve the spec's formatting style and voice
    - Return empty JSON arrays [] when there are no updates or new sections
    """

    spec_content: str = dspy.InputField(desc="Full markdown spec file")
    decisions_text: str = dspy.InputField(
        desc="Decisions as numbered list: 1. Question: ...\n   Decision: ..."
    )
    section_updates_json: str = dspy.OutputField(
        desc='JSON array of {"header": "## X", "content": "new body"} for existing sections'
    )
    new_sections_json: str = dspy.OutputField(
        desc='JSON array of {"header": "## X", "content": "body"} for new sections, or []'
    )


class WholeFileSpecUpdater(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(WholeFileSpecUpdaterSignature)

    def forward(
        self, spec_content: str, decisions_text: str
    ) -> tuple[list[dict], list[dict]]:
        result = self.predict(
            spec_content=spec_content, decisions_text=decisions_text
        )
        section_updates = json.loads(result.section_updates_json)
        new_sections = json.loads(result.new_sections_json)
        return section_updates, new_sections
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_spec_updater.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/programs/spec_updater.py tests/test_spec_updater.py
git commit -m "feat: add WholeFileSpecUpdater DSPy module"
```

---

### Task 5: Add OutlineMerger DSPy module

**Files:**
- Modify: `plumb/programs/spec_updater.py`
- Modify: `tests/test_spec_updater.py`

**Step 1: Write failing test**

Append to `tests/test_spec_updater.py`:

```python
from plumb.programs.spec_updater import OutlineMerger


class TestOutlineMerger:
    def test_parses_outline(self):
        mock_prediction = MagicMock()
        mock_prediction.merged_outline = "# Title\n## Auth\n## Cache\n## API"

        with patch("plumb.programs.spec_updater.dspy.Predict") as MockPredict:
            mock_predict_instance = MagicMock(return_value=mock_prediction)
            MockPredict.return_value = mock_predict_instance

            merger = OutlineMerger()
            result = merger(
                current_outline="# Title\n## Auth\n## API",
                new_headers="## Cache",
            )

        assert result == ["# Title", "## Auth", "## Cache", "## API"]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_spec_updater.py::TestOutlineMerger -v`
Expected: FAIL with ImportError

**Step 3: Implement OutlineMerger**

Add to `plumb/programs/spec_updater.py`:

```python
class OutlineMergerSignature(dspy.Signature):
    """Given the current spec outline (headers only) and new section headers,
    return the complete merged outline with new headers placed at the most
    logical positions. Return all headers, one per line, preserving heading
    levels. Do not remove or rename any existing headers."""

    current_outline: str = dspy.InputField(
        desc="Current spec headers, one per line"
    )
    new_headers: str = dspy.InputField(
        desc="New section headers to place, one per line"
    )
    merged_outline: str = dspy.OutputField(
        desc="All headers in correct order, one per line"
    )


class OutlineMerger(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(OutlineMergerSignature)

    def forward(self, current_outline: str, new_headers: str) -> list[str]:
        result = self.predict(
            current_outline=current_outline, new_headers=new_headers
        )
        return [
            line.strip()
            for line in result.merged_outline.strip().split("\n")
            if line.strip()
        ]
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_spec_updater.py::TestOutlineMerger -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/programs/spec_updater.py tests/test_spec_updater.py
git commit -m "feat: add OutlineMerger DSPy module"
```

---

### Task 6: Replace per-section loop in sync_decisions

This is the core change — replace lines 191-240 of `plumb/sync.py` with the new whole-file approach.

**Files:**
- Modify: `plumb/sync.py:191-240`
- Modify: `tests/test_sync.py`

**Step 1: Write/update failing test for whole-file sync**

Add to `tests/test_sync.py`:

```python
class TestSyncDecisionsWholeFile:
    """Tests for the whole-file spec update path."""

    def test_calls_whole_file_updater_once_per_file(self, initialized_repo):
        """Multiple decisions should result in one updater call, not N."""
        d1 = Decision(id="dec-wf1", status="approved",
                      question="Auth method?", decision="JWT tokens.",
                      created_at=datetime.now(timezone.utc).isoformat())
        d2 = Decision(id="dec-wf2", status="approved",
                      question="Cache strategy?", decision="In-memory dict.",
                      created_at=datetime.now(timezone.utc).isoformat())
        append_decision(initialized_repo, d1, branch="main")
        append_decision(initialized_repo, d2, branch="main")

        call_count = 0

        def mock_run_with_retries(fn, *args, **kwargs):
            nonlocal call_count
            # WholeFileSpecUpdater returns (section_updates, new_sections)
            if hasattr(fn, 'predict') and 'spec_content' in str(type(fn)):
                call_count += 1
                return [{"header": "## Features", "content": "Updated features.\n"}], []
            # parse_spec_files returns []
            return []

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.sync.run_with_retries", side_effect=mock_run_with_retries):
            result = sync_decisions(initialized_repo)

        assert result["spec_updated"] >= 1

    def test_handles_new_sections_with_outline_merge(self, initialized_repo):
        """When new sections are returned, outline merger should be called."""
        d = Decision(id="dec-ns1", status="approved",
                     question="Add caching?", decision="Yes, Redis.",
                     created_at=datetime.now(timezone.utc).isoformat())
        append_decision(initialized_repo, d, branch="main")

        call_sequence = []

        def mock_run_with_retries(fn, *args, **kwargs):
            from plumb.programs.spec_updater import WholeFileSpecUpdater, OutlineMerger
            if isinstance(fn, WholeFileSpecUpdater):
                call_sequence.append("updater")
                return [], [{"header": "## Cache", "content": "Redis cache.\n"}]
            elif isinstance(fn, OutlineMerger):
                call_sequence.append("merger")
                return ["# Spec", "## Features", "## Cache"]
            return []

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.sync.run_with_retries", side_effect=mock_run_with_retries):
            result = sync_decisions(initialized_repo)

        assert "updater" in call_sequence
        assert "merger" in call_sequence
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync.py::TestSyncDecisionsWholeFile -v`
Expected: FAIL (new test class, old sync code doesn't match expectations)

**Step 3: Replace sync spec-update logic**

In `plumb/sync.py`, replace lines 191-240 (the `BatchSpecUpdater` import and per-section loop) with:

```python
    from plumb.programs.spec_updater import WholeFileSpecUpdater, OutlineMerger
    updater = WholeFileSpecUpdater()
    merger = OutlineMerger()
    spec_updated = 0

    # Format all decisions once
    decision_lines = []
    for i, d in enumerate(to_sync, 1):
        decision_lines.append(
            f"{i}. Question: {d.question or 'N/A'}\n   Decision: {d.decision or 'N/A'}"
        )
    decisions_text = "\n".join(decision_lines)

    for spec_path_str in config.spec_paths:
        if on_progress:
            on_progress(f"Updating spec: {spec_path_str}...")
        spec_path = repo_root / spec_path_str
        if not spec_path.is_file():
            continue

        content = spec_path.read_text()

        # Single LLM call for the whole file
        try:
            section_updates, new_sections = run_with_retries(
                updater, content, decisions_text
            )
        except Exception:
            continue

        # Apply updates to existing sections
        if section_updates:
            content = apply_section_updates(content, section_updates)
            spec_updated += len(section_updates)

        # Handle new sections via outline merge
        if new_sections:
            current_outline = extract_outline(content)
            new_headers = "\n".join(s["header"] for s in new_sections)
            try:
                merged_outline = run_with_retries(
                    merger, "\n".join(current_outline), new_headers
                )
            except Exception:
                # Fallback: append new sections at end
                for s in new_sections:
                    body = s["content"]
                    if body and not body.startswith("\n"):
                        body = "\n" + body
                    content = content.rstrip("\n") + "\n\n" + s["header"] + body
                spec_updated += len(new_sections)
                _atomic_write(spec_path, content)
                continue

            content = insert_new_sections(content, new_sections, merged_outline)
            spec_updated += len(new_sections)

        _atomic_write(spec_path, content)
```

Also update the import at the top of `sync_decisions` — remove the `from plumb.programs.spec_updater import BatchSpecUpdater` line and replace with the new imports (already in the replacement code above).

**Step 4: Run all sync tests**

Run: `pytest tests/test_sync.py -v`
Expected: PASS (existing tests should still pass with mocks adjusted, new tests pass)

Note: The existing `test_syncs_approved_decision` test mocks `run_with_retries` directly, so it will need its mock return value updated to match the new `(list, list)` tuple return. Update the mock in that test:

```python
    def test_syncs_approved_decision(self, initialized_repo):
        d = Decision(
            id="dec-sync1",
            status="approved",
            question="How to cache?",
            decision="Use in-memory dict.",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        append_decision(initialized_repo, d, branch="main")

        call_count = [0]

        def mock_run(fn, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # WholeFileSpecUpdater returns (updates, new_sections)
                return [{"header": "## Features", "content": "The system uses in-memory dict cache.\n"}], []
            # parse_spec_files parser
            return []

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.sync.run_with_retries", side_effect=mock_run):
            result = sync_decisions(initialized_repo)

        assert result["spec_updated"] == 1
        decisions = read_decisions(initialized_repo, branch="main")
        synced = [d for d in decisions if d.id == "dec-sync1" and d.synced_at]
        assert len(synced) == 1
```

Similarly update `test_filters_by_decision_ids` mock.

**Step 5: Run the full test suite**

Run: `pytest tests/ -v`
Expected: PASS

**Step 6: Commit**

```bash
git add plumb/sync.py tests/test_sync.py
git commit -m "feat: replace per-section sync with whole-file spec updater"
```

---

### Task 7: Clean up old code

**Files:**
- Modify: `plumb/programs/spec_updater.py`
- Modify: `plumb/sync.py`

**Step 1: Check for other callers of `BatchSpecUpdater` or `SpecUpdater`**

Run: `grep -rn "BatchSpecUpdater\|SpecUpdater\b" plumb/ tests/ --include="*.py"`

If no other callers exist outside `spec_updater.py` itself, proceed to remove.

**Step 2: Remove old signatures and modules**

Remove `SpecUpdaterSignature`, `SpecUpdater`, `BatchSpecUpdaterSignature`, and `BatchSpecUpdater` from `plumb/programs/spec_updater.py`.

**Step 3: Remove `find_spec_section` if unused**

Check: `grep -rn "find_spec_section" plumb/ tests/ --include="*.py"`

If only used in old sync code and tests, remove from `plumb/sync.py` and update `tests/test_sync.py` to remove `TestFindSpecSection` and its import.

**Step 4: Run full test suite**

Run: `pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/programs/spec_updater.py plumb/sync.py tests/test_sync.py
git commit -m "chore: remove old per-section spec updater code"
```

---

### Task 8: Manual integration test

**Step 1: Run `plumb sync` on a real repo with pending decisions**

If you have approved decisions pending:
```bash
plumb status
plumb sync
```

Verify:
- Only 1 LLM call per spec file (check logs/timing)
- Spec is correctly updated
- New sections (if any) are placed logically
- Tests still generated for uncovered requirements

**Step 2: Test edge case — no decisions to sync**

```bash
plumb sync
```
Expected: "No unsynced decisions found." — no LLM calls.

**Step 3: Commit any fixes**
