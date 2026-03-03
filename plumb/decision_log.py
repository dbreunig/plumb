from __future__ import annotations

import json
import os
import re as _re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class FileRef(BaseModel):
    file: str
    lines: list[int] = Field(default_factory=list)


class Decision(BaseModel):
    id: str
    status: str = "pending"
    question: Optional[str] = None
    decision: Optional[str] = None
    made_by: Optional[str] = None
    commit_sha: Optional[str] = None
    branch: Optional[str] = None
    ref_status: str = "ok"
    conversation_available: bool = True
    file_refs: list[FileRef] = Field(default_factory=list)
    related_requirement_ids: list[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    chunk_index: Optional[int] = None
    conversation_truncated: bool = False
    rejection_reason: Optional[str] = None
    user_note: Optional[str] = None
    synced_at: Optional[str] = None
    reviewed_at: Optional[str] = None
    created_at: Optional[str] = None


def generate_decision_id() -> str:
    return f"dec-{uuid.uuid4().hex[:12]}"


def _sanitize_branch_name(branch: str) -> str:
    """Convert branch name to filesystem-safe filename component."""
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


def read_decisions(repo_root: str | Path, branch: str | None = None) -> list[Decision]:
    """Read decisions.jsonl, returning latest-line-wins deduped list.

    When *branch* is given, read from the branch-scoped shard file.
    When *branch* is None, read from the legacy monolithic file.
    """
    path = _branch_decisions_path(repo_root, branch) if branch else _decisions_path(repo_root)
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


def append_decision(repo_root: str | Path, decision: Decision, branch: str | None = None) -> None:
    """Append a single decision line to decisions.jsonl.

    When *branch* is given, write to the branch-scoped shard file.
    When *branch* is None, write to the legacy monolithic file.
    """
    path = _branch_decisions_path(repo_root, branch) if branch else _decisions_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(decision.model_dump()) + "\n")


def append_decisions(repo_root: str | Path, decisions: list[Decision], branch: str | None = None) -> None:
    """Append multiple decision lines.

    When *branch* is given, write to the branch-scoped shard file.
    When *branch* is None, write to the legacy monolithic file.
    """
    path = _branch_decisions_path(repo_root, branch) if branch else _decisions_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for dec in decisions:
            f.write(json.dumps(dec.model_dump()) + "\n")


def update_decision_status(
    repo_root: str | Path,
    decision_id: str,
    branch: str | None = None,
    **updates,
) -> Decision | None:
    """Update a decision by appending a new line with updated fields.

    When *branch* is given, read from and write to the branch-scoped shard.
    When *branch* is None, use the legacy monolithic file.

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


def read_all_decisions(repo_root: str | Path) -> list[Decision]:
    """Read and deduplicate decisions across ALL branch JSONL files using DuckDB.

    Returns latest-line-wins deduplication by decision ID across every shard.
    """
    decisions_dir = _decisions_dir(repo_root)
    if not decisions_dir.exists():
        return []
    jsonl_files = list(decisions_dir.glob("*.jsonl"))
    if not jsonl_files:
        return []

    import duckdb

    glob_pattern = str(decisions_dir / "*.jsonl")
    try:
        conn = duckdb.connect(":memory:")
        query = f"""
            WITH raw AS (
                SELECT *, ROW_NUMBER() OVER () AS _line_num
                FROM read_json_auto('{glob_pattern}', format='newline_delimited')
            ),
            deduped AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY _line_num DESC) AS _rn
                FROM raw
            )
            SELECT * EXCLUDE (_line_num, _rn) FROM deduped WHERE _rn = 1
        """
        rel = conn.execute(query)
        columns = [desc[0] for desc in rel.description]
        rows = rel.fetchall()
        conn.close()
    except Exception:
        return []

    decisions = []
    for row in rows:
        raw = dict(zip(columns, row))
        # Convert DuckDB/numpy types to Python native types
        cleaned = _clean_duckdb_row(raw)
        try:
            decisions.append(Decision(**cleaned))
        except Exception:
            continue
    return decisions


def _clean_duckdb_row(raw: dict) -> dict:
    """Convert DuckDB result row values to Python-native types for Pydantic."""
    import math

    cleaned = {}
    for key, value in raw.items():
        if key == "rowid":
            continue
        value = _to_python_native(value)
        # Convert NaN to None
        if isinstance(value, float) and math.isnan(value):
            value = None
        # Handle file_refs: DuckDB returns list of dicts or structs
        if key == "file_refs" and isinstance(value, list):
            converted_refs = []
            for item in value:
                if isinstance(item, dict):
                    # Convert inner values too
                    item = {k: _to_python_native(v) for k, v in item.items()}
                    converted_refs.append(FileRef(**item))
                elif isinstance(item, (list, tuple)):
                    # Struct as tuple: (file, lines)
                    converted_refs.append(FileRef(file=str(item[0]), lines=list(item[1]) if len(item) > 1 else []))
                else:
                    converted_refs.append(item)
            value = converted_refs
        # Handle related_requirement_ids: DuckDB may return as special list type
        elif key == "related_requirement_ids" and value is not None:
            if isinstance(value, (list, tuple)):
                value = [str(v) for v in value]
            else:
                value = []
        cleaned[key] = value
    return cleaned


def _to_python_native(value):
    """Convert numpy/DuckDB scalar types to Python builtins."""
    import math

    if value is None:
        return None
    # Handle numpy types if numpy is available
    try:
        import numpy as np
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (np.floating,)):
            f = float(value)
            return None if math.isnan(f) else f
        if isinstance(value, (np.bool_,)):
            return bool(value)
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:
        pass
    # Handle DuckDB list types
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, list):
        return [_to_python_native(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_python_native(v) for k, v in value.items()}
    return value


def filter_decisions(
    repo_root: str | Path,
    status: str | None = None,
    branch: str | None = None,
) -> list[Decision]:
    """Filter decisions by status and/or branch.

    When *branch* is given, read from that single branch file (fast, no DuckDB).
    When *branch* is None, use read_all_decisions() to query across all shards.
    """
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


def find_decision_branch(repo_root: str | Path, decision_id: str) -> str | None:
    """Find which branch file contains a decision ID.

    Returns the branch name (file stem) or None if not found.
    """
    decisions_dir = _decisions_dir(repo_root)
    if not decisions_dir.exists():
        return None
    for jsonl_file in decisions_dir.glob("*.jsonl"):
        content = jsonl_file.read_text()
        if f'"id": "{decision_id}"' in content or f'"id":"{decision_id}"' in content:
            return jsonl_file.stem
    return None


def delete_decisions_by_commit(repo_root: str | Path, commit_sha: str, branch: str | None = None) -> int:
    """Delete decisions matching a commit SHA by rewriting the file.

    When *branch* is given, rewrite the branch-scoped shard file.
    When *branch* is None, rewrite the legacy monolithic file.

    Returns number of lines removed."""
    path = _branch_decisions_path(repo_root, branch) if branch else _decisions_path(repo_root)
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

    line_count = len([l for l in branch_content.splitlines() if l.strip()])

    target_path = _branch_decisions_path(repo_root, target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "a") as f:
        f.write(branch_content + "\n")

    branch_path.unlink()
    return {"merged": line_count}


def deduplicate_decisions(
    decisions: list[Decision],
    existing_decisions: list[Decision] | None = None,
    use_llm: bool = False,
) -> list[Decision]:
    """Collapse decisions with same question and same decision text,
    preserving the earliest chunk_index. Then use LLM semantic dedup
    to filter out duplicates of existing decisions."""
    # Exact dedup
    print(f"[dedup] Input: {len(decisions)} candidates, {len(existing_decisions or [])} existing", flush=True)
    seen: dict[tuple, Decision] = {}
    for d in decisions:
        key = (
            (d.question or "").strip().lower(),
            (d.decision or "").strip().lower(),
        )
        if key in seen:
            existing = seen[key]
            if (d.chunk_index or 0) < (existing.chunk_index or 0):
                seen[key] = d
        else:
            seen[key] = d
    result = list(seen.values())
    print(f"[dedup] After exact dedup: {len(result)}", flush=True)

    # LLM-based semantic dedup pass (Haiku — cheap/fast)
    if use_llm and len(result) >= 1:
        print(f"[dedup] Sending {len(result)} candidates to LLM dedup...", flush=True)
        result = _llm_dedup(result, existing_decisions or [])
        print(f"[dedup] After LLM dedup: {len(result)}", flush=True)

    print(f"[dedup] Final: {len(result)} decisions", flush=True)
    return result


def _format_decision_line(index: int, d: Decision) -> str:
    q = d.question or ""
    dec = d.decision or ""
    return f"{index}. [Q] {q} [D] {dec}"


def _llm_dedup(
    candidates: list[Decision],
    existing_decisions: list[Decision],
) -> list[Decision]:
    """Use LLM to catch semantic duplicates."""
    import dspy
    from plumb.programs.decision_deduplicator import DecisionDeduplicator

    candidates_str = "\n".join(
        _format_decision_line(i + 1, d) for i, d in enumerate(candidates)
    )
    # Smart selection: always include all approved/synced decisions (validated
    # choices must never be re-proposed), then fill remaining capacity with
    # recent unresolved decisions.
    max_existing = 200
    if existing_decisions:
        approved = [d for d in existing_decisions if d.status in ("approved", "edited", "synced")]
        others = [d for d in existing_decisions if d.status not in ("approved", "edited", "synced")]
        remaining_cap = max(0, max_existing - len(approved))
        recent_existing = approved + others[-remaining_cap:] if remaining_cap else approved
    else:
        recent_existing = []
    existing_str = "\n".join(
        _format_decision_line(i + 1, d) for i, d in enumerate(recent_existing)
    ) or "(none)"

    print(f"[dedup:llm] Sending {len(candidates)} candidates against {len(recent_existing)} existing decisions", flush=True)

    from plumb.programs import get_program_lm

    override_lm = get_program_lm("decision_deduplicator")
    lm = override_lm or dspy.LM("anthropic/claude-haiku-4-5-20251001", max_tokens=32000)
    deduplicator = DecisionDeduplicator()
    with dspy.context(lm=lm):
        unique_indices = deduplicator(
            candidates=candidates_str, existing=existing_str
        )

    print(f"[dedup:llm] LLM returned unique_indices: {unique_indices}", flush=True)

    # Convert 1-based indices to 0-based, filter to valid range
    valid = []
    for idx in unique_indices:
        zero_based = idx - 1
        if 0 <= zero_based < len(candidates):
            valid.append(zero_based)
    kept = [candidates[i] for i in valid]
    print(f"[dedup:llm] Keeping {len(kept)}/{len(candidates)} candidates (indices {valid})", flush=True)
    return kept
