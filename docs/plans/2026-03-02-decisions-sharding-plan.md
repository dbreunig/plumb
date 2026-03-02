# Decision Log Sharding Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Shard the monolithic `decisions.jsonl` into per-branch JSONL files with DuckDB as the cross-shard query engine.

**Architecture:** Branch-scoped JSONL files under `.plumb/decisions/`. Single-branch operations use direct Python I/O. Cross-shard operations (dedup, status aggregation) use DuckDB's `read_json_auto()` glob. On branch merge, decisions roll into `main.jsonl`.

**Tech Stack:** Python stdlib (`json`, `pathlib`, `re`), DuckDB (new dependency), existing Pydantic models.

**Design doc:** `docs/plans/2026-03-02-decisions-sharding-design.md`

---

### Task 1: Path Helpers and Branch Name Sanitization

**Files:**
- Modify: `plumb/decision_log.py:45-46`
- Test: `tests/test_decision_log.py`

**Step 1: Write failing tests for path helpers**

Add to `tests/test_decision_log.py`:

```python
from plumb.decision_log import (
    _sanitize_branch_name,
    _decisions_dir,
    _branch_decisions_path,
)


class TestPathHelpers:
    def test_sanitize_simple_branch(self):
        assert _sanitize_branch_name("main") == "main"

    def test_sanitize_slashes(self):
        assert _sanitize_branch_name("feature/foo") == "feature-foo"

    def test_sanitize_multiple_slashes(self):
        assert _sanitize_branch_name("feature/bar/baz") == "feature-bar-baz"

    def test_sanitize_special_chars(self):
        assert _sanitize_branch_name("fix/bug#123") == "fix-bug-123"

    def test_sanitize_head(self):
        assert _sanitize_branch_name("HEAD") == "HEAD"

    def test_decisions_dir(self, initialized_repo):
        d = _decisions_dir(initialized_repo)
        assert d == initialized_repo / ".plumb" / "decisions"

    def test_branch_decisions_path(self, initialized_repo):
        p = _branch_decisions_path(initialized_repo, "feature/foo")
        assert p == initialized_repo / ".plumb" / "decisions" / "feature-foo.jsonl"

    def test_branch_decisions_path_main(self, initialized_repo):
        p = _branch_decisions_path(initialized_repo, "main")
        assert p == initialized_repo / ".plumb" / "decisions" / "main.jsonl"
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_decision_log.py::TestPathHelpers -v`
Expected: ImportError — `_sanitize_branch_name` not found.

**Step 3: Implement path helpers**

In `plumb/decision_log.py`, replace the existing `_decisions_path` function (lines 45-46) and add new helpers above it:

```python
import re as _re


def _sanitize_branch_name(branch: str) -> str:
    """Convert branch name to filesystem-safe filename component.
    Replaces slashes and special chars with dashes."""
    return _re.sub(r"[^a-zA-Z0-9._-]", "-", branch)


def _decisions_dir(repo_root: str | Path) -> Path:
    """Return the decisions directory: .plumb/decisions/"""
    return Path(repo_root) / ".plumb" / "decisions"


def _branch_decisions_path(repo_root: str | Path, branch: str) -> Path:
    """Return the JSONL path for a specific branch."""
    return _decisions_dir(repo_root) / f"{_sanitize_branch_name(branch)}.jsonl"


def _decisions_path(repo_root: str | Path) -> Path:
    """Legacy monolithic path. Used only for migration detection."""
    return Path(repo_root) / ".plumb" / "decisions.jsonl"
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_decision_log.py::TestPathHelpers -v`
Expected: All PASS.

**Step 5: Commit**

```bash
git add plumb/decision_log.py tests/test_decision_log.py
git commit -m "feat: add branch name sanitization and path helpers for decision sharding"
```

---

### Task 2: Branch-Scoped read_decisions

**Files:**
- Modify: `plumb/decision_log.py:49-65`
- Test: `tests/test_decision_log.py`

**Step 1: Write failing tests for branch-scoped reads**

Add to `tests/test_decision_log.py`:

```python
class TestBranchScopedRead:
    def test_read_empty_branch(self, initialized_repo):
        result = read_decisions(initialized_repo, branch="feature-x")
        assert result == []

    def test_read_specific_branch(self, initialized_repo, sample_decisions):
        # Write to a specific branch file
        append_decisions(initialized_repo, sample_decisions, branch="feature-x")
        result = read_decisions(initialized_repo, branch="feature-x")
        assert len(result) == 2

    def test_read_branch_isolation(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, [sample_decisions[0]], branch="branch-a")
        append_decisions(initialized_repo, [sample_decisions[1]], branch="branch-b")
        a = read_decisions(initialized_repo, branch="branch-a")
        b = read_decisions(initialized_repo, branch="branch-b")
        assert len(a) == 1
        assert a[0].id == "dec-aaa111"
        assert len(b) == 1
        assert b[0].id == "dec-bbb222"

    def test_latest_line_wins_within_branch(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="main")
        updated = sample_decisions[0].model_copy(update={"status": "approved"})
        append_decision(initialized_repo, updated, branch="main")
        result = read_decisions(initialized_repo, branch="main")
        assert len(result) == 1
        assert result[0].status == "approved"
```

Note: These tests depend on `append_decision`/`append_decisions` also accepting a `branch` param (Task 3). Write them together — the tests drive both changes.

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_decision_log.py::TestBranchScopedRead -v`
Expected: TypeError — `read_decisions()` got unexpected keyword argument 'branch'.

**Step 3: Modify read_decisions to accept branch**

Replace `read_decisions` in `plumb/decision_log.py`:

```python
def read_decisions(repo_root: str | Path, branch: str | None = None) -> list[Decision]:
    """Read decisions from a branch-specific JSONL file.
    If branch is None, reads from the legacy monolithic file (for backward compat during migration).
    Returns latest-line-wins deduped list."""
    if branch is not None:
        path = _branch_decisions_path(repo_root, branch)
    else:
        path = _decisions_path(repo_root)
    if not path.exists():
        return []
    by_id: dict[str, Decision] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            dec = Decision(**data)
            by_id[dec.id] = dec
        except (json.JSONDecodeError, Exception):
            continue
    return list(by_id.values())
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_decision_log.py::TestBranchScopedRead -v`
Expected: Still failing — need Task 3 (write functions) first. Run existing tests to verify no regression:

Run: `python -m pytest tests/test_decision_log.py::TestReadWriteDecisions -v`
Expected: All PASS (backward compat via `branch=None` falling back to legacy path).

**Step 5: Commit**

```bash
git add plumb/decision_log.py tests/test_decision_log.py
git commit -m "feat: add branch parameter to read_decisions"
```

---

### Task 3: Branch-Scoped Write Functions

**Files:**
- Modify: `plumb/decision_log.py:68-82` (append_decision, append_decisions)
- Test: `tests/test_decision_log.py`

**Step 1: Write failing tests**

The tests from Task 2 (`TestBranchScopedRead`) already exercise branch-scoped writes via `append_decision(..., branch=)`. Add one more:

```python
class TestBranchScopedWrite:
    def test_append_creates_decisions_dir(self, initialized_repo):
        d = Decision(id="dec-1", question="Q?", decision="A.")
        append_decision(initialized_repo, d, branch="new-branch")
        assert (initialized_repo / ".plumb" / "decisions" / "new-branch.jsonl").exists()

    def test_append_multiple_to_branch(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions, branch="feat")
        result = read_decisions(initialized_repo, branch="feat")
        assert len(result) == 2
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_decision_log.py::TestBranchScopedWrite -v`
Expected: TypeError — `append_decision()` got unexpected keyword argument 'branch'.

**Step 3: Modify write functions**

Replace `append_decision` and `append_decisions` in `plumb/decision_log.py`:

```python
def append_decision(repo_root: str | Path, decision: Decision, branch: str | None = None) -> None:
    """Append a single decision line. If branch is given, writes to branch file."""
    if branch is not None:
        path = _branch_decisions_path(repo_root, branch)
    else:
        path = _decisions_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(decision.model_dump()) + "\n")


def append_decisions(repo_root: str | Path, decisions: list[Decision], branch: str | None = None) -> None:
    """Append multiple decision lines. If branch is given, writes to branch file."""
    if branch is not None:
        path = _branch_decisions_path(repo_root, branch)
    else:
        path = _decisions_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for dec in decisions:
            f.write(json.dumps(dec.model_dump()) + "\n")
```

**Step 4: Run ALL decision_log tests**

Run: `python -m pytest tests/test_decision_log.py -v`
Expected: All PASS — old tests use `branch=None` (legacy path), new tests use `branch="..."`.

**Step 5: Commit**

```bash
git add plumb/decision_log.py tests/test_decision_log.py
git commit -m "feat: add branch parameter to append_decision and append_decisions"
```

---

### Task 4: Branch-Scoped Update and Delete

**Files:**
- Modify: `plumb/decision_log.py:85-156` (update_decision_status, delete_decisions_by_commit)
- Test: `tests/test_decision_log.py`

**Step 1: Write failing tests**

```python
class TestBranchScopedUpdate:
    def test_update_in_branch(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="feat")
        result = update_decision_status(
            initialized_repo, "dec-aaa111", branch="feat", status="approved"
        )
        assert result is not None
        assert result.status == "approved"
        decisions = read_decisions(initialized_repo, branch="feat")
        assert decisions[0].status == "approved"

    def test_update_not_found_in_branch(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="feat")
        result = update_decision_status(
            initialized_repo, "dec-aaa111", branch="other", status="approved"
        )
        assert result is None


class TestBranchScopedDelete:
    def test_delete_by_commit_in_branch(self, initialized_repo):
        d1 = Decision(id="dec-1", commit_sha="sha111")
        d2 = Decision(id="dec-2", commit_sha="sha222")
        append_decisions(initialized_repo, [d1, d2], branch="feat")
        removed = delete_decisions_by_commit(initialized_repo, "sha111", branch="feat")
        assert removed == 1
        remaining = read_decisions(initialized_repo, branch="feat")
        assert len(remaining) == 1
        assert remaining[0].id == "dec-2"
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_decision_log.py::TestBranchScopedUpdate tests/test_decision_log.py::TestBranchScopedDelete -v`
Expected: TypeError — unexpected keyword argument 'branch'.

**Step 3: Modify update and delete functions**

Replace `update_decision_status` in `plumb/decision_log.py`:

```python
def update_decision_status(
    repo_root: str | Path,
    decision_id: str,
    branch: str | None = None,
    **updates,
) -> Decision | None:
    """Update a decision by appending a new line with updated fields.
    If branch is given, scopes to that branch file.
    Returns the updated decision, or None if not found."""
    decisions = read_decisions(repo_root, branch=branch)
    target = None
    for d in decisions:
        if d.id == decision_id:
            target = d
            break
    if target is None:
        return None
    updated_data = target.model_dump()
    updated_data.update(updates)
    updated = Decision(**updated_data)
    append_decision(repo_root, updated, branch=branch)
    return updated
```

Replace `delete_decisions_by_commit`:

```python
def delete_decisions_by_commit(repo_root: str | Path, commit_sha: str, branch: str | None = None) -> int:
    """Delete decisions matching a commit SHA by rewriting the file.
    If branch is given, scopes to that branch file.
    Returns number of lines removed."""
    if branch is not None:
        path = _branch_decisions_path(repo_root, branch)
    else:
        path = _decisions_path(repo_root)
    if not path.exists():
        return 0
    lines = path.read_text().splitlines()
    kept = []
    removed = 0
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        try:
            data = json.loads(line_stripped)
            if data.get("commit_sha") == commit_sha:
                removed += 1
                continue
        except json.JSONDecodeError:
            pass
        kept.append(line_stripped)
    # Atomic rewrite
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".jsonl")
    try:
        with os.fdopen(fd, "w") as f:
            for k in kept:
                f.write(k + "\n")
        os.replace(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return removed
```

**Step 4: Run ALL decision_log tests**

Run: `python -m pytest tests/test_decision_log.py -v`
Expected: All PASS.

**Step 5: Commit**

```bash
git add plumb/decision_log.py tests/test_decision_log.py
git commit -m "feat: add branch parameter to update_decision_status and delete_decisions_by_commit"
```

---

### Task 5: Add DuckDB Dependency

**Files:**
- Modify: `pyproject.toml:10-20`

**Step 1: Add duckdb to dependencies**

In `pyproject.toml`, add `"duckdb"` to the `dependencies` list (line 19, before `"python-dotenv"`):

```toml
dependencies = [
    "dspy",
    "anthropic",
    "pytest",
    "pytest-cov",
    "gitpython",
    "click",
    "rich",
    "jsonlines",
    "duckdb",
    "python-dotenv",
]
```

**Step 2: Install**

Run: `pip install -e .`
Expected: duckdb installed successfully.

**Step 3: Verify import**

Run: `python -c "import duckdb; print(duckdb.__version__)"`
Expected: Version number printed.

**Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat: add duckdb dependency for cross-shard decision queries"
```

---

### Task 6: Cross-Shard read_all_decisions via DuckDB

**Files:**
- Modify: `plumb/decision_log.py`
- Test: `tests/test_decision_log.py`

**Step 1: Write failing tests**

```python
from plumb.decision_log import read_all_decisions


class TestReadAllDecisions:
    def test_empty_directory(self, initialized_repo):
        # Create decisions dir but no files
        (initialized_repo / ".plumb" / "decisions").mkdir(parents=True, exist_ok=True)
        result = read_all_decisions(initialized_repo)
        assert result == []

    def test_reads_across_branches(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="branch-a")
        append_decision(initialized_repo, sample_decisions[1], branch="branch-b")
        result = read_all_decisions(initialized_repo)
        assert len(result) == 2
        ids = {d.id for d in result}
        assert ids == {"dec-aaa111", "dec-bbb222"}

    def test_dedup_across_branches(self, initialized_repo, sample_decisions):
        """Same decision ID in two branch files — latest-line-wins via DuckDB."""
        append_decision(initialized_repo, sample_decisions[0], branch="branch-a")
        updated = sample_decisions[0].model_copy(update={"status": "approved"})
        append_decision(initialized_repo, updated, branch="branch-a")
        result = read_all_decisions(initialized_repo)
        assert len(result) == 1
        assert result[0].status == "approved"

    def test_reads_single_branch(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions, branch="main")
        result = read_all_decisions(initialized_repo)
        assert len(result) == 2
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_decision_log.py::TestReadAllDecisions -v`
Expected: ImportError — `read_all_decisions` not found.

**Step 3: Implement read_all_decisions**

Add to `plumb/decision_log.py`:

```python
def read_all_decisions(repo_root: str | Path) -> list[Decision]:
    """Read and deduplicate decisions across ALL branch JSONL files using DuckDB.
    Returns latest-line-wins deduped list of Decision objects."""
    decisions_dir = _decisions_dir(repo_root)
    if not decisions_dir.exists():
        return []
    jsonl_files = list(decisions_dir.glob("*.jsonl"))
    if not jsonl_files:
        return []

    import duckdb

    glob_pattern = str(decisions_dir / "*.jsonl")
    try:
        df = duckdb.sql(f"""
            SELECT * FROM (
                SELECT *,
                    ROW_NUMBER() OVER (PARTITION BY id ORDER BY rownum DESC) as _rn
                FROM read_json_auto('{glob_pattern}', format='newline_delimited')
            ) WHERE _rn = 1
        """).fetchdf()
    except duckdb.IOException:
        return []

    results = []
    for _, row in df.iterrows():
        data = {k: v for k, v in row.to_dict().items() if not k.startswith("_") and k != "rownum"}
        # Convert numpy/pandas types to Python native
        for k, v in data.items():
            if hasattr(v, "item"):
                data[k] = v.item()
            if isinstance(v, float) and v != v:  # NaN check
                data[k] = None
        try:
            results.append(Decision(**data))
        except Exception:
            continue
    return results
```

Note: DuckDB's `read_json_auto` with `newline_delimited` format handles JSONL. The `rownum` pseudo-column tracks line position within each file. The `ROW_NUMBER()` window function handles latest-line-wins dedup.

**Step 4: Run tests**

Run: `python -m pytest tests/test_decision_log.py::TestReadAllDecisions -v`
Expected: All PASS.

**Step 5: Commit**

```bash
git add plumb/decision_log.py tests/test_decision_log.py
git commit -m "feat: add read_all_decisions with DuckDB cross-shard queries"
```

---

### Task 7: Update filter_decisions for Cross-Shard

**Files:**
- Modify: `plumb/decision_log.py:107-121`
- Test: `tests/test_decision_log.py`

**Step 1: Write failing tests**

```python
class TestFilterDecisionsCrossShard:
    def test_filter_across_branches_by_status(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="branch-a")
        d2_approved = sample_decisions[1].model_copy(update={"status": "approved"})
        append_decision(initialized_repo, d2_approved, branch="branch-b")
        pending = filter_decisions(initialized_repo, status="pending")
        assert len(pending) == 1
        assert pending[0].id == "dec-aaa111"

    def test_filter_single_branch(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions, branch="feat")
        result = filter_decisions(initialized_repo, status="pending", branch="feat")
        assert len(result) == 2
```

**Step 2: Run tests to verify behavior**

Run: `python -m pytest tests/test_decision_log.py::TestFilterDecisionsCrossShard -v`
Expected: Failures — current `filter_decisions` reads from legacy path, not sharded files.

**Step 3: Update filter_decisions**

Replace `filter_decisions` in `plumb/decision_log.py`:

```python
def filter_decisions(
    repo_root: str | Path,
    status: str | None = None,
    branch: str | None = None,
) -> list[Decision]:
    """Filter decisions by status and/or branch.
    If branch is given, reads from that branch file only (fast, no DuckDB).
    If branch is None, reads across all shards via read_all_decisions."""
    if branch is not None:
        decisions = read_decisions(repo_root, branch=branch)
    else:
        decisions = read_all_decisions(repo_root)
    result = []
    for d in decisions:
        if status and d.status != status:
            continue
        result.append(d)
    return result
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_decision_log.py -v`
Expected: All PASS including old `TestFilterDecisions` tests (which use `initialized_repo` with legacy path — these will need the conftest update in Task 10 to fully work with sharded layout).

**Step 5: Commit**

```bash
git add plumb/decision_log.py tests/test_decision_log.py
git commit -m "feat: update filter_decisions to support cross-shard queries"
```

---

### Task 8: find_decision_branch Helper

For CLI commands like `plumb approve <id>` where the user provides a decision ID without knowing which branch it's on, we need a way to locate which branch file contains it.

**Files:**
- Modify: `plumb/decision_log.py`
- Test: `tests/test_decision_log.py`

**Step 1: Write failing tests**

```python
from plumb.decision_log import find_decision_branch


class TestFindDecisionBranch:
    def test_find_in_branch(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="feat")
        result = find_decision_branch(initialized_repo, "dec-aaa111")
        assert result == "feat"

    def test_not_found(self, initialized_repo):
        (initialized_repo / ".plumb" / "decisions").mkdir(parents=True, exist_ok=True)
        result = find_decision_branch(initialized_repo, "dec-nonexist")
        assert result is None

    def test_find_across_multiple_branches(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="branch-a")
        append_decision(initialized_repo, sample_decisions[1], branch="branch-b")
        assert find_decision_branch(initialized_repo, "dec-aaa111") == "branch-a"
        assert find_decision_branch(initialized_repo, "dec-bbb222") == "branch-b"
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_decision_log.py::TestFindDecisionBranch -v`
Expected: ImportError.

**Step 3: Implement find_decision_branch**

Add to `plumb/decision_log.py`:

```python
def find_decision_branch(repo_root: str | Path, decision_id: str) -> str | None:
    """Find which branch file contains a decision ID.
    Scans branch files directly (no DuckDB needed — just grep for the ID).
    Returns the unsanitized branch filename stem, or None."""
    decisions_dir = _decisions_dir(repo_root)
    if not decisions_dir.exists():
        return None
    for jsonl_file in decisions_dir.glob("*.jsonl"):
        content = jsonl_file.read_text()
        if f'"id": "{decision_id}"' in content or f'"id":"{decision_id}"' in content:
            return jsonl_file.stem
    return None
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_decision_log.py::TestFindDecisionBranch -v`
Expected: All PASS.

**Step 5: Commit**

```bash
git add plumb/decision_log.py tests/test_decision_log.py
git commit -m "feat: add find_decision_branch helper for locating decisions across shards"
```

---

### Task 9: Update git_hook.py Callers

**Files:**
- Modify: `plumb/git_hook.py:19-27,317-372`
- Test: `tests/test_git_hook.py` (verify existing tests still pass)

**Step 1: Identify all call sites in git_hook.py**

The branch is already computed at line 317: `branch = _get_branch_name(repo)`. Pass it to all decision functions:

- Line 322: `delete_decisions_by_commit(repo_root, config.last_commit)` → add `branch=branch`
- Line 326: `read_decisions(repo_root)` → `read_all_decisions(repo_root)` (needs cross-shard dedup)
- Line 348: `deduplicate_decisions(conv_decisions, existing_decisions=existing_decisions, use_llm=True)` → no change (operates on lists)
- Line 357: `append_decisions(repo_root, conv_decisions)` → add `branch=branch`
- Line 371: `read_decisions(repo_root)` → `read_all_decisions(repo_root)` (pending check is cross-shard)

**Step 2: Update imports**

In `plumb/git_hook.py`, update the import block (lines 19-27):

```python
from plumb.decision_log import (
    Decision,
    generate_decision_id,
    read_all_decisions,
    append_decisions,
    delete_decisions_by_commit,
    deduplicate_decisions,
)
```

Remove `read_decisions` and `filter_decisions` from the import (no longer needed here).

**Step 3: Update call sites**

Line 322:
```python
delete_decisions_by_commit(repo_root, config.last_commit, branch=branch)
```

Line 326:
```python
existing_decisions = read_all_decisions(repo_root)
```

Line 357:
```python
append_decisions(repo_root, conv_decisions, branch=branch)
```

Line 371:
```python
all_decisions = read_all_decisions(repo_root)
```

**Step 4: Run git_hook tests**

Run: `python -m pytest tests/test_git_hook.py -v`
Expected: All PASS. The tests mock or use temp repos, so they should work with the updated signatures. If any fail due to the sharded layout, the test fixtures need updating (see Task 11).

**Step 5: Commit**

```bash
git add plumb/git_hook.py
git commit -m "refactor: update git_hook.py to use branch-scoped decision functions"
```

---

### Task 10: Update cli.py Callers

**Files:**
- Modify: `plumb/cli.py:23-29,237-500,784`
- Test: `tests/test_cli.py`

**Step 1: Update imports**

In `plumb/cli.py`, update the import block (lines 23-29):

```python
from plumb.decision_log import (
    Decision,
    read_decisions,
    read_all_decisions,
    append_decision,
    update_decision_status,
    filter_decisions,
    find_decision_branch,
)
```

**Step 2: Update review command (lines 237-294)**

The review command shows pending decisions across all branches. Update line 237:

```python
pending = filter_decisions(repo_root, status="pending")
```

This now uses `read_all_decisions` under the hood (Task 7). No change needed.

For the update calls within the review loop (lines 268-285), we need to find which branch each decision is on:

```python
# Before the review loop, build a branch lookup
branch_for_id = {}
for d in pending:
    b = find_decision_branch(repo_root, d.id)
    if b:
        branch_for_id[d.id] = b
```

Then each `update_decision_status` call gets `branch=branch_for_id.get(d.id)`:

```python
update_decision_status(repo_root, d.id, branch=branch_for_id.get(d.id), status="approved", reviewed_at=now)
```

Apply the same pattern to the ignore, reject, and edit branches in the review loop.

**Step 3: Update approve command (lines 394-432)**

For `approve --all` (line 410):
```python
pending = filter_decisions(repo_root, status="pending")
```
Already cross-shard via Task 7.

For single approve (line 424), find the branch first:
```python
branch = find_decision_branch(repo_root, decision_id)
result = update_decision_status(
    repo_root, decision_id, branch=branch, status="approved", reviewed_at=now,
)
```

Apply the same `find_decision_branch` pattern to `reject`, `ignore`, `edit`, and `modify` commands.

**Step 4: Update status command (line 784)**

```python
decisions = read_all_decisions(repo_root)
```

**Step 5: Update _run_modify (line 302)**

```python
decisions = read_all_decisions(repo_root)
```

**Step 6: Update CLAUDE.md template (line 153)**

Change `decisions.jsonl` reference to `decisions/` directory:

```python
- **Decision log:** `.plumb/decisions/`
```

And line 169:
```python
- Never edit `.plumb/decisions/` files directly.
```

**Step 7: Run CLI tests**

Run: `python -m pytest tests/test_cli.py -v`
Expected: All PASS (may need fixture updates — see Task 11).

**Step 8: Commit**

```bash
git add plumb/cli.py
git commit -m "refactor: update cli.py to use branch-scoped and cross-shard decision functions"
```

---

### Task 11: Update sync.py Callers

**Files:**
- Modify: `plumb/sync.py:12-16,167,326`

**Step 1: Update imports**

```python
from plumb.decision_log import (
    Decision,
    read_all_decisions,
    update_decision_status,
    find_decision_branch,
)
```

**Step 2: Update read call (line 167)**

```python
decisions = read_all_decisions(repo_root)
```

**Step 3: Update synced_at marking (line 326)**

```python
for d in to_sync:
    branch = find_decision_branch(repo_root, d.id)
    update_decision_status(repo_root, d.id, branch=branch, synced_at=now)
```

**Step 4: Run sync tests**

Run: `python -m pytest tests/test_sync.py -v`
Expected: All PASS.

**Step 5: Commit**

```bash
git add plumb/sync.py
git commit -m "refactor: update sync.py to use cross-shard decision reads"
```

---

### Task 12: Migration Command

**Files:**
- Modify: `plumb/decision_log.py` (add `migrate_decisions` function)
- Modify: `plumb/cli.py` (add `plumb migrate` CLI command)
- Test: `tests/test_decision_log.py`

**Step 1: Write failing tests**

```python
from plumb.decision_log import migrate_decisions


class TestMigrateDecisions:
    def test_migrate_creates_sharded_dir(self, initialized_repo, sample_decisions):
        # Write to legacy monolithic file
        append_decisions(initialized_repo, sample_decisions)
        result = migrate_decisions(initialized_repo)
        assert result["migrated"] == 2
        assert (initialized_repo / ".plumb" / "decisions" / "main.jsonl").exists()
        assert not (initialized_repo / ".plumb" / "decisions.jsonl").exists()

    def test_migrate_deduplicates(self, initialized_repo, sample_decisions):
        # Write same decision twice (simulating update append)
        append_decision(initialized_repo, sample_decisions[0])
        updated = sample_decisions[0].model_copy(update={"status": "approved"})
        append_decision(initialized_repo, updated)
        append_decision(initialized_repo, sample_decisions[1])
        result = migrate_decisions(initialized_repo)
        assert result["migrated"] == 2  # 2 unique decisions, not 3 lines
        decisions = read_decisions(initialized_repo, branch="main")
        approved = [d for d in decisions if d.id == "dec-aaa111"]
        assert approved[0].status == "approved"

    def test_migrate_idempotent(self, initialized_repo):
        # No legacy file, decisions dir already exists
        (initialized_repo / ".plumb" / "decisions").mkdir(parents=True, exist_ok=True)
        result = migrate_decisions(initialized_repo)
        assert result["migrated"] == 0
        assert result["already_migrated"] is True

    def test_migrate_empty_legacy(self, initialized_repo):
        # Legacy file exists but is empty
        (initialized_repo / ".plumb" / "decisions.jsonl").write_text("")
        result = migrate_decisions(initialized_repo)
        assert result["migrated"] == 0
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_decision_log.py::TestMigrateDecisions -v`
Expected: ImportError.

**Step 3: Implement migrate_decisions**

Add to `plumb/decision_log.py`:

```python
def migrate_decisions(repo_root: str | Path) -> dict:
    """Migrate from monolithic decisions.jsonl to branch-sharded layout.
    Reads legacy file, deduplicates, writes to decisions/main.jsonl, removes legacy file.
    Returns summary dict."""
    repo_root = Path(repo_root)
    legacy_path = _decisions_path(repo_root)
    decisions_dir = _decisions_dir(repo_root)

    # Already migrated?
    if not legacy_path.exists():
        return {"migrated": 0, "already_migrated": decisions_dir.exists()}

    # Read and deduplicate from legacy file
    decisions = read_decisions(repo_root)  # branch=None reads legacy path
    if not decisions:
        legacy_path.unlink()
        decisions_dir.mkdir(parents=True, exist_ok=True)
        return {"migrated": 0, "already_migrated": False}

    # Write deduplicated decisions to main.jsonl
    decisions_dir.mkdir(parents=True, exist_ok=True)
    main_path = decisions_dir / "main.jsonl"
    with open(main_path, "w") as f:
        for dec in decisions:
            f.write(json.dumps(dec.model_dump()) + "\n")

    # Remove legacy file
    legacy_path.unlink()

    return {"migrated": len(decisions), "already_migrated": False}
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_decision_log.py::TestMigrateDecisions -v`
Expected: All PASS.

**Step 5: Add CLI command**

In `plumb/cli.py`, add:

```python
@cli.command()
def migrate():
    """Migrate from monolithic decisions.jsonl to branch-sharded layout."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    from plumb.decision_log import migrate_decisions
    result = migrate_decisions(repo_root)

    if result["already_migrated"]:
        console.print("Already migrated to sharded layout.")
        return

    if result["migrated"] == 0:
        console.print("No decisions to migrate.")
    else:
        console.print(f"[green]Migrated {result['migrated']} decisions to .plumb/decisions/main.jsonl[/green]")
```

**Step 6: Run all tests**

Run: `python -m pytest -v`
Expected: All PASS.

**Step 7: Commit**

```bash
git add plumb/decision_log.py plumb/cli.py tests/test_decision_log.py
git commit -m "feat: add plumb migrate command for monolithic-to-sharded migration"
```

---

### Task 13: Merge-Decisions Command

**Files:**
- Modify: `plumb/decision_log.py` (add `merge_branch_decisions` function)
- Modify: `plumb/cli.py` (add `plumb merge-decisions` CLI command)
- Test: `tests/test_decision_log.py`

**Step 1: Write failing tests**

```python
from plumb.decision_log import merge_branch_decisions


class TestMergeBranchDecisions:
    def test_merge_to_main(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions, branch="feat")
        result = merge_branch_decisions(initialized_repo, "feat")
        assert result["merged"] == 2
        # Branch file should be gone
        assert not (initialized_repo / ".plumb" / "decisions" / "feat.jsonl").exists()
        # Decisions should be in main
        main_decisions = read_decisions(initialized_repo, branch="main")
        assert len(main_decisions) == 2

    def test_merge_appends_to_existing_main(self, initialized_repo, sample_decisions):
        # Some decisions already in main
        d_main = Decision(id="dec-main1", question="Q?", decision="A.")
        append_decision(initialized_repo, d_main, branch="main")
        append_decisions(initialized_repo, sample_decisions, branch="feat")
        merge_branch_decisions(initialized_repo, "feat")
        main_decisions = read_decisions(initialized_repo, branch="main")
        assert len(main_decisions) == 3

    def test_merge_nonexistent_branch(self, initialized_repo):
        (initialized_repo / ".plumb" / "decisions").mkdir(parents=True, exist_ok=True)
        result = merge_branch_decisions(initialized_repo, "nonexistent")
        assert result["merged"] == 0

    def test_cannot_merge_main(self, initialized_repo):
        result = merge_branch_decisions(initialized_repo, "main")
        assert result["error"] == "cannot merge main into itself"
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_decision_log.py::TestMergeBranchDecisions -v`
Expected: ImportError.

**Step 3: Implement merge_branch_decisions**

Add to `plumb/decision_log.py`:

```python
def merge_branch_decisions(repo_root: str | Path, branch: str, target: str = "main") -> dict:
    """Merge a branch's decisions into the target branch (default: main).
    Appends branch file contents to target file, then deletes branch file.
    Returns summary dict."""
    if _sanitize_branch_name(branch) == _sanitize_branch_name(target):
        return {"merged": 0, "error": "cannot merge main into itself"}

    branch_path = _branch_decisions_path(repo_root, branch)
    if not branch_path.exists():
        return {"merged": 0}

    branch_content = branch_path.read_text().strip()
    if not branch_content:
        branch_path.unlink()
        return {"merged": 0}

    # Count decisions
    line_count = len([l for l in branch_content.splitlines() if l.strip()])

    # Append to target
    target_path = _branch_decisions_path(repo_root, target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "a") as f:
        f.write(branch_content + "\n")

    # Remove branch file
    branch_path.unlink()

    return {"merged": line_count}
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_decision_log.py::TestMergeBranchDecisions -v`
Expected: All PASS.

**Step 5: Add CLI command**

In `plumb/cli.py`:

```python
@cli.command(name="merge-decisions")
@click.argument("branch")
@click.option("--target", default="main", help="Target branch to merge into (default: main)")
def merge_decisions(branch, target):
    """Merge a branch's decisions into the target branch (default: main)."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    from plumb.decision_log import merge_branch_decisions
    result = merge_branch_decisions(repo_root, branch, target=target)

    if result.get("error"):
        console.print(f"[red]Error: {result['error']}[/red]")
        raise SystemExit(1)

    if result["merged"] == 0:
        console.print(f"No decisions found for branch '{branch}'.")
    else:
        console.print(f"[green]Merged {result['merged']} decision lines from '{branch}' into '{target}'.[/green]")
```

**Step 6: Commit**

```bash
git add plumb/decision_log.py plumb/cli.py tests/test_decision_log.py
git commit -m "feat: add plumb merge-decisions command"
```

---

### Task 14: Update Test Fixtures and Run Full Suite

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_decision_log.py` (update existing tests to use branch param)
- Modify: `tests/test_git_hook.py`, `tests/test_cli.py`, `tests/test_sync.py` as needed

**Step 1: Update conftest.py initialized_repo fixture**

The `initialized_repo` fixture creates `.plumb/` but not `.plumb/decisions/`. Add:

```python
@pytest.fixture
def initialized_repo(tmp_repo):
    """A tmp_repo with .plumb/ directory, config, and decisions dir."""
    ensure_plumb_dir(tmp_repo)
    cfg = PlumbConfig(
        spec_paths=["spec.md"],
        test_paths=["tests/"],
        initialized_at=datetime.now(timezone.utc).isoformat(),
    )
    save_config(tmp_repo, cfg)
    spec = tmp_repo / "spec.md"
    spec.write_text("# Spec\n\n## Features\n\nThe system must do X.\n")
    (tmp_repo / "tests").mkdir(exist_ok=True)
    (tmp_repo / ".plumb" / "decisions").mkdir(exist_ok=True)
    return tmp_repo
```

**Step 2: Verify existing tests still pass**

The existing `TestReadWriteDecisions`, `TestUpdateDecisionStatus`, `TestFilterDecisions`, `TestDeleteDecisionsByCommit` tests use `branch=None` which falls back to the legacy monolithic file. They should still pass without changes since `_decisions_path` still exists for backward compat.

However, if any existing test creates the monolithic `decisions.jsonl` and then tries to read via cross-shard functions, that won't work. Review each test file and fix as needed.

**Step 3: Run full test suite**

Run: `python -m pytest -v`
Expected: All PASS.

**Step 4: Fix any failures**

If tests in `test_git_hook.py`, `test_cli.py`, or `test_sync.py` fail because they were relying on the monolithic file path, update them to either:
- Pass `branch="main"` to decision functions, or
- Create the legacy file (for migration-testing purposes)

**Step 5: Commit**

```bash
git add tests/
git commit -m "test: update fixtures and tests for branch-sharded decision layout"
```

---

### Task 15: Update CLAUDE.md and Documentation

**Files:**
- Modify: `plumb/cli.py:146-172` (CLAUDE.md template)
- Modify: `CLAUDE.md` (update references)

**Step 1: Update the CLAUDE.md template in cli.py**

In `plumb/cli.py`, update the `_update_claude_md` block (lines 146-172). Change:

- `- **Decision log:** \`.plumb/decisions.jsonl\`` → `- **Decision log:** \`.plumb/decisions/\``
- `- Never edit \`.plumb/decisions.jsonl\` directly.` → `- Never edit files in \`.plumb/decisions/\` directly.`

**Step 2: Update the project's own CLAUDE.md**

Run: `plumb init` would regenerate it, but we can update manually — change the two references from `decisions.jsonl` to `decisions/`.

**Step 3: Run tests**

Run: `python -m pytest -v`
Expected: All PASS.

**Step 4: Commit**

```bash
git add plumb/cli.py CLAUDE.md
git commit -m "docs: update CLAUDE.md template for branch-sharded decision layout"
```

---

### Task 16: Final Integration Test

**Files:**
- Test: `tests/test_integration.py` (add a new test or update existing)

**Step 1: Write an end-to-end test**

```python
class TestDecisionShardingIntegration:
    def test_full_lifecycle(self, initialized_repo):
        """Test: write to branch → read across shards → merge to main."""
        from plumb.decision_log import (
            Decision, append_decision, append_decisions,
            read_decisions, read_all_decisions, filter_decisions,
            update_decision_status, merge_branch_decisions,
            find_decision_branch,
        )

        # 1. Write decisions to two branches
        d1 = Decision(id="dec-1", status="pending", question="Q1?", decision="A1")
        d2 = Decision(id="dec-2", status="pending", question="Q2?", decision="A2")
        append_decision(initialized_repo, d1, branch="feature-a")
        append_decision(initialized_repo, d2, branch="feature-b")

        # 2. Cross-shard read sees both
        all_decisions = read_all_decisions(initialized_repo)
        assert len(all_decisions) == 2

        # 3. Filter by status across shards
        pending = filter_decisions(initialized_repo, status="pending")
        assert len(pending) == 2

        # 4. Find decision branch
        assert find_decision_branch(initialized_repo, "dec-1") == "feature-a"

        # 5. Update in correct branch
        update_decision_status(initialized_repo, "dec-1", branch="feature-a", status="approved")
        d = read_decisions(initialized_repo, branch="feature-a")
        assert any(x.status == "approved" for x in d)

        # 6. Merge feature-a to main
        result = merge_branch_decisions(initialized_repo, "feature-a")
        assert result["merged"] > 0

        # 7. Main now has feature-a's decisions
        main_decisions = read_decisions(initialized_repo, branch="main")
        assert any(x.id == "dec-1" for x in main_decisions)

        # 8. feature-a file is gone
        assert find_decision_branch(initialized_repo, "dec-1") == "main"
```

**Step 2: Run integration test**

Run: `python -m pytest tests/test_integration.py::TestDecisionShardingIntegration -v`
Expected: All PASS.

**Step 3: Run full suite one final time**

Run: `python -m pytest -v`
Expected: All PASS.

**Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration test for decision sharding lifecycle"
```

---

## Execution Notes

- **Tasks 1-4** are the core refactor of `decision_log.py` — pure TDD, no external dependencies.
- **Task 5** adds DuckDB — quick pip install.
- **Tasks 6-8** add DuckDB-powered cross-shard functions.
- **Tasks 9-11** update all callers — these are mostly mechanical find-and-replace with branch param threading.
- **Task 12-13** add new CLI commands.
- **Task 14** ensures all existing tests still pass with the new layout.
- **Task 15-16** are cleanup and integration verification.

**Critical invariant to maintain throughout:** All existing tests must pass after each commit. The `branch=None` default provides backward compatibility with the legacy monolithic path, so nothing breaks until the migration command converts the layout.
