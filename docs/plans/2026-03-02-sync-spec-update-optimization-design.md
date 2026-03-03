# Sync Spec Update Optimization

## Problem

`plumb sync` makes one LLM call per spec section touched by a decision. When
decisions hit many different sections, this creates a slow sequential chain of
expensive LLM calls. The total wall-clock time is the sum of all call latencies.

## Design

### Core idea

Send the **entire spec file** as input context (specs are small enough), but
return only **structured edits** — not the whole file — as output. This reduces
LLM calls from N-per-section to 1-per-spec-file for the common case.

Handle new section insertion separately via a cheap outline-merge call, triggered
only when the primary call indicates new sections are needed.

### New DSPy signatures

#### WholeFileSpecUpdater (primary, 1 call per spec file)

```python
class SectionEdit(BaseModel):
    header: str      # exact header line, e.g. "### Token Handling"
    content: str     # full content for this section (below the header)

class WholeFileSpecUpdaterSignature(dspy.Signature):
    """Update a markdown spec to incorporate approved decisions.
    Return only sections that need changes. For existing sections,
    use the exact header text. For new sections, provide the header
    and content. Do not modify sections unrelated to the decisions."""

    spec_content: str = dspy.InputField(desc="Full markdown spec file")
    decisions_text: str = dspy.InputField(desc="Decisions as numbered Q&A list")
    section_updates: list[SectionEdit] = dspy.OutputField(
        desc="Updates to existing sections - header must match exactly"
    )
    new_sections: list[SectionEdit] = dspy.OutputField(
        desc="Brand new sections to add - empty list if none needed"
    )
```

#### OutlineMerger (conditional, only when new sections exist)

```python
class OutlineMergerSignature(dspy.Signature):
    """Given the current spec outline and new section headers to add,
    return the complete merged outline with new headers placed at
    the appropriate positions. Return headers only, one per line."""

    current_outline: str = dspy.InputField(desc="Current spec headers, one per line")
    new_headers: str = dspy.InputField(desc="New section headers to place")
    merged_outline: str = dspy.OutputField(desc="All headers in correct order")
```

### Updated sync flow

```
For each spec file:
  1. Read full content
  2. Format all decisions as numbered Q&A list
  3. Call WholeFileSpecUpdater -> section_updates + new_sections
  4. Apply section_updates by matching headers in the file
  5. If new_sections is non-empty:
     a. Extract current outline (headers only)
     b. Call OutlineMerger with current outline + new headers
     c. Insert new sections at positions from merged outline
  6. Atomic write
```

### Applying section updates (no LLM)

Parse spec into sections by header. For each update, match the header (exact
first, then normalized fallback: strip whitespace, case-insensitive). Replace
the section body. Leave unmatched sections untouched.

### Inserting new sections (from outline merge)

Walk the merged outline in order. For each header that doesn't exist in the
current content, find the preceding header in the outline, locate it in the
content, and insert the new section after it.

### What stays the same

- Test generation: already batched, already checks coverage via markers
- Spec re-parse: same
- Decision sync marking: same

### LLM call reduction

| Scenario | Before | After |
|----------|--------|-------|
| 5 decisions, 5 sections, no new sections | 5 calls | 1 call |
| 5 decisions, 5 sections, 2 new sections | 5 calls | 2 calls |
| 10 decisions, 1 section | 1 call | 1 call |

### Risks and mitigations

**Unintended edits**: LLM sees the whole file and might return updates to
sections it shouldn't touch. Mitigated by the signature instructing "only
sections that need changes" and by the structured output format (can't silently
modify unreferenced sections).

**Header matching failures**: LLM might return a header that doesn't exactly
match. Mitigated by normalized fallback matching.

**Large spec files**: If a spec is very large, input tokens increase but output
stays proportional to changes. This is acceptable since input tokens are cheap
relative to output tokens and LLM call latency.

### Files to modify

- `plumb/programs/spec_updater.py` — add `SectionEdit`, `WholeFileSpecUpdater`,
  `OutlineMerger`; keep old signatures for backward compat initially
- `plumb/sync.py` — replace the per-section loop with single whole-file call,
  add `apply_section_updates()` and `insert_new_sections()` helpers, add
  conditional outline-merge path
