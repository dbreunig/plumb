from __future__ import annotations

import json
import os
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


def _decisions_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".plumb" / "decisions.jsonl"


def read_decisions(repo_root: str | Path) -> list[Decision]:
    """Read decisions.jsonl, returning latest-line-wins deduped list."""
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


def append_decision(repo_root: str | Path, decision: Decision) -> None:
    """Append a single decision line to decisions.jsonl."""
    path = _decisions_path(repo_root)
    path.parent.mkdir(exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(decision.model_dump()) + "\n")


def append_decisions(repo_root: str | Path, decisions: list[Decision]) -> None:
    """Append multiple decision lines."""
    path = _decisions_path(repo_root)
    path.parent.mkdir(exist_ok=True)
    with open(path, "a") as f:
        for dec in decisions:
            f.write(json.dumps(dec.model_dump()) + "\n")


def update_decision_status(
    repo_root: str | Path,
    decision_id: str,
    **updates,
) -> Decision | None:
    """Update a decision by appending a new line with updated fields.
    Returns the updated decision, or None if not found."""
    decisions = read_decisions(repo_root)
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
    append_decision(repo_root, updated)
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


def delete_decisions_by_commit(repo_root: str | Path, commit_sha: str) -> int:
    """Delete decisions matching a commit SHA by rewriting the file.
    Returns number of lines removed."""
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


def deduplicate_decisions(decisions: list[Decision]) -> list[Decision]:
    """Collapse decisions with same question and same decision text,
    preserving the earliest chunk_index."""
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
    return list(seen.values())
