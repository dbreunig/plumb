# Decision Log: Branch-Sharded JSONL + DuckDB Query Layer

**Date:** 2026-03-02
**Status:** Approved

## Problem

The monolithic `.plumb/decisions.jsonl` file is growing unwieldy at 1,183 lines (395 unique decisions). Every read operation parses the entire file. Status updates append duplicate lines, inflating the file ~3x. In a multiplayer setting, concurrent developers generate merge conflicts on the shared file.

## Decision

Shard the decision log into per-branch JSONL files. Use DuckDB as a query engine for cross-shard operations. Keep simple single-branch operations as direct Python file I/O.

## Design

### File Layout

```
.plumb/
  decisions/
    main.jsonl             # canonical history (absorbs merged branches)
    feature-x.jsonl        # active branch working file
    fix-auth-bug.jsonl     # another active branch
```

- Each branch gets its own file, named by sanitized branch name (`feature/foo` -> `feature-foo.jsonl`).
- `main.jsonl` is the long-lived canonical log.
- On branch merge: append branch file contents to `main.jsonl`, delete branch file.
- Working on main directly: decisions go straight to `main.jsonl`.

### Read/Write Split

**Simple operations (direct Python, no DuckDB):**

| Operation | Scope |
|-----------|-------|
| `append_decision()` | Current branch file |
| `append_decisions()` | Current branch file |
| `update_decision_status()` | Current branch file |
| `delete_decisions_by_commit()` | Current branch file |
| `read_decisions(branch=)` | Single branch file |

**Cross-shard operations (DuckDB, lazy-imported):**

| Operation | Why DuckDB |
|-----------|------------|
| `read_all_decisions()` | Window dedup across all branch files |
| `deduplicate_decisions()` | Scan all branches for duplicates |
| `filter_decisions()` (no branch) | Cross-file aggregation |
| `plumb status` / `plumb coverage` | Aggregate stats across all shards |

DuckDB is imported lazily inside cross-shard functions only. Single-branch operations never touch it.

### Branch Lifecycle

1. Pre-commit hook detects current branch via `git rev-parse --abbrev-ref HEAD`.
2. Decisions append to `.plumb/decisions/<branch>.jsonl`.
3. Reviews and syncs update the same branch file.
4. On merge to main: `plumb merge-decisions` (or post-merge hook) appends branch contents to `main.jsonl` and deletes the branch file.
5. If branch had no decisions, no-op.

**Edge cases:**
- Branch name sanitization: slashes to dashes. Original name preserved in the `branch` field on each decision record.
- Stale branch files: `plumb status` flags orphaned files with no corresponding remote branch.

### DuckDB Integration

- Added as a project dependency. Lazy import only.
- Cross-shard functions return `list[Decision]` (Pydantic objects), keeping the existing API surface unchanged for callers.
- Parameterized queries where possible; branch names come from git, not user input.

Core query pattern for cross-shard dedup:
```sql
SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY rownum DESC) as rn
    FROM read_json_auto('.plumb/decisions/*.jsonl')
) WHERE rn = 1
```

### Migration

1. `plumb migrate` command reads existing `decisions.jsonl` with latest-line-wins dedup.
2. Creates `.plumb/decisions/` directory.
3. Writes deduplicated decisions (~395 records) to `decisions/main.jsonl`.
4. Removes old `decisions.jsonl`.
5. Idempotent: if directory exists and old file doesn't, migration already done.
6. No silent fallback: if old file is detected, warn and require explicit migration.

### Files Changed

- `decision_log.py` — New path helpers, branch-aware read/write, DuckDB cross-shard functions.
- `git_hook.py` — Pass current branch to decision functions.
- `cli.py` — Add `plumb migrate` and `plumb merge-decisions` commands, update `plumb status`.
- `sync.py` — Minimal changes (already receives decisions as lists).

## Alternatives Considered

- **SQLite as primary store:** Good query performance but binary file in git causes unresolvable merge conflicts in multiplayer.
- **SQLite as local cache over JSONL:** Two sources of truth, cache invalidation complexity.
- **DuckDB for all operations:** Over-engineers simple append/read paths.
- **Per-commit sharding:** Too granular. Commit SHA not available at pre-commit time.
- **Per-hook-run sharding:** Zero conflicts but excessive file proliferation.
- **Local-only decisions:** Ruled out because decisions are a shared team resource.
