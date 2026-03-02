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


def filter_decisions(
    repo_root: str | Path,
    status: str | None = None,
    branch: str | None = None,
) -> list[Decision]:
    """Filter decisions by status and/or branch."""
    decisions = read_decisions(repo_root)
    result = []
    for d in decisions:
        if status and d.status != status:
            continue
        if branch and d.branch != branch:
            continue
        result.append(d)
    return result


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
    recent_existing = existing_decisions[-50:] if existing_decisions else []
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
