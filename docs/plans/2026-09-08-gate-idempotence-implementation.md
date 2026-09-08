# Gate Idempotence Implementation Plan

> **For Claude:** Execute task by task with TDD. Each task ends in its own
> commit with the exact message given.

**Goal:** Make the review-mode gate idempotent per commit attempt, per
`2026-09-08-gate-idempotence-design.md`.

**Architecture:** A new `plumb/gate.py` computes a `git patch-id --stable`
signature over the filtered staged diff and stores it in a transient
`.plumb/gate.json`. The pre-commit hook short-circuits when the signature
matches and no pendings remain, writes the state whenever it blocks, and
suppresses the diff-only fallback inside a review cycle. Test paths join the
diff filter.

**Tech stack:** Python, GitPython + subprocess for `patch-id`, pytest.

---

### Task 1: Gate state module and gitignore entry

**Files:**
- Create: `plumb/gate.py`
- Modify: `plumb/config.py` (`_PLUMB_GITIGNORE`, `ensure_plumb_dir`)
- Test: `tests/test_gate.py`

**`plumb/gate.py` contents:**

```python
"""Reviewed-diff signature and gate state for the review-mode pre-commit hook."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

EMPTY_SIGNATURE = "empty"


def diff_signature(repo_root: str | Path, diff: str) -> str:
    """First field of `git patch-id --stable` over *diff*; EMPTY_SIGNATURE when blank."""
    if not diff.strip():
        return EMPTY_SIGNATURE
    out = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=diff, capture_output=True, text=True, cwd=str(repo_root),
    ).stdout.strip()
    if not out:
        return EMPTY_SIGNATURE
    return out.split()[0]


def _gate_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".plumb" / "gate.json"


def read_gate_state(repo_root: str | Path) -> dict | None:
    """The stored gate state, or None when absent or unreadable."""
    try:
        return json.loads(_gate_path(repo_root).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_gate_state(repo_root: str | Path, signature: str) -> None:
    _gate_path(repo_root).write_text(json.dumps({
        "diff_signature": signature,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }))


def clear_gate_state(repo_root: str | Path) -> None:
    _gate_path(repo_root).unlink(missing_ok=True)
```

**`plumb/config.py`:** change `_PLUMB_GITIGNORE` to
`"record.log\nrecord.lock\ngate.json\n"`. In `ensure_plumb_dir`, when the
`.gitignore` exists, read it and append any `_PLUMB_GITIGNORE` line it lacks
(preserve existing content and order; write only when something is missing).

**Tests (write first, watch them fail, then implement):**
- `diff_signature` returns the same value for the same diff twice, differs
  for a different diff, and returns `EMPTY_SIGNATURE` for `""` and `"\n"`.
  Use a real temp git repo (`git init`) so `git patch-id` runs.
- `read_gate_state` on a missing file → None; corrupt JSON → None.
- write then read round-trips the signature; `clear_gate_state` removes the
  file and is a no-op when already absent.
- `ensure_plumb_dir` on a fresh dir writes all three lines; on a dir with an
  old two-line `.gitignore` appends `gate.json` and keeps the old lines; on a
  current three-line file rewrites nothing (compare mtime or content).

**Commit:** `feat(gate): diff signature and transient gate state file`

---

### Task 2: Test paths leave the analyzed diff

**Files:**
- Modify: `plumb/git_hook.py` (`_get_plumb_managed_paths`)
- Test: `tests/test_git_hook.py` (or wherever `_filter_paths`/
  `_get_staged_diff_filtered` tests live; add them if none exist)

**Change:**

```python
def _get_plumb_managed_paths(config) -> list[str]:
    """Return paths managed by plumb that should be excluded from diff analysis."""
    return [".plumb/"] + list(config.spec_paths) + list(config.test_paths)
```

**Tests:**
- `_filter_paths` drops files under a configured test path and keeps other
  files.
- A staged change touching only the tests directory yields an empty filtered
  diff, and `_run_hook_inner` returns 0 without extracting (patch
  `extract_decisions` and assert it was not called). Use a temp repo with a
  written config.

Check existing tests for `_get_plumb_managed_paths`/`_filter_paths`
expectations and update any that assert the old two-element list.

**Commit:** `fix(hook): exclude configured test paths from the analyzed diff`

---

### Task 3: Hook short-circuit, write-on-block, fallback guard, post-commit clear

**Files:**
- Modify: `plumb/git_hook.py` (`_run_hook_inner`, `extract_decisions`,
  `run_post_commit`)
- Test: `tests/test_git_hook.py`

**Changes to `_run_hook_inner`:**

1. After the amend-detection step, compute
   `signature = diff_signature(repo_root, diff)` and
   `state = read_gate_state(repo_root)` (skip both when `dry_run`).
2. When `state` exists, `state.get("diff_signature") == signature`, and
   `read_all_decisions(repo_root)` has no `status == "pending"` rows:
   `clear_gate_state(repo_root)` and return 0. This runs before
   `extract_decisions`, so a clean second attempt makes no LLM calls.
3. Pass `allow_diff_fallback=(state is None)` into `extract_decisions`.
4. At both `return 1` sites (the single `if pending:` block), call
   `write_gate_state(repo_root, signature)` first. Never write when
   `dry_run`.

**Change to `extract_decisions`:** add `allow_diff_fallback: bool = True`;
wrap the `_extract_decisions_from_diff` fallback in `if allow_diff_fallback:`.
Record mode callers stay unchanged and keep the default.

**Change to `run_post_commit`:** call `clear_gate_state(repo_root)` next to
the `last_extracted_at = None` reset.

**Tests (patch every LLM boundary; no network):**
- Loop simulation: temp repo, config in review mode, staged code change.
  First `run_hook` with `extract_decisions` patched to return one pending
  decision → returns 1, `gate.json` exists with the diff's signature.
  Resolve the pending (rewrite its status in the shard). Second `run_hook`
  with `extract_decisions` patched to raise `AssertionError("must not run")`
  → returns 0, `gate.json` gone.
- Same setup, but stage an additional code file before the second run →
  signature differs → `extract_decisions` IS called, with
  `allow_diff_fallback=False`.
- Unresolved pendings + matching signature → still returns 1, state
  rewritten.
- `dry_run=True` neither reads nor writes `gate.json` (pre-create a state
  file with a matching signature; dry run still extracts).
- `run_post_commit` removes an existing `gate.json`.
- `extract_decisions(..., allow_diff_fallback=False)` with no conversation
  turns returns `[]` and never constructs `_extract_decisions_from_diff`'s
  extractor (patch it, assert not called).

**Commit:** `feat(hook): reviewed-diff short-circuit makes the review gate idempotent`

---

### Task 4: Docs

**Files:**
- Modify: `plumb_spec.md` (pre-commit hook behavior section), `README.md`
  ("Review mode" parts of How it works), design doc Status → Implemented.
- Check: both SKILL.md copies for any wording that promises re-analysis on
  the second commit; update if present, keep the two copies identical.

**Content:**
- Spec: the hook stores a reviewed-diff signature when it blocks; a re-run
  with the same signature and zero pending decisions passes without
  analysis; test paths are excluded from diff analysis, so tests-only
  commits pass without review; `.plumb/gate.json` is transient and
  gitignored.
- README review-mode sections: after the decisions are resolved and sync has
  staged its output, the second `git commit` passes without re-analysis.
- Project State tree in README: add `gate.json` under `.plumb/` with a "not
  committed" note.

**Commit:** `docs: gate idempotence (reviewed-diff signature; tests-only commits pass)`
