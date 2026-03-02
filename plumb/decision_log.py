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


_STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would "
    "shall should may might can could to of in for on with at by from and or but "
    "not no nor so yet if then else it its this that these those i we you he she "
    "they me us him her them my our your his their".split()
)


def _normalize_text(text: str) -> set[str]:
    """Extract meaningful words minus stop words."""
    words = set(text.strip().lower().split())
    return words - _STOP_WORDS


def _text_similarity(a: str, b: str) -> float:
    """Jaccard similarity on normalized word sets."""
    words_a = _normalize_text(a)
    words_b = _normalize_text(b)
    if not words_a and not words_b:
        return 1.0
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def deduplicate_decisions(
    decisions: list[Decision],
    existing_decisions: list[Decision] | None = None,
    similarity_threshold: float = 0.5,
    use_llm: bool = False,
) -> list[Decision]:
    """Collapse decisions with same question and same decision text,
    preserving the earliest chunk_index. Also filter out new decisions
    that are semantically similar to already-resolved existing decisions."""
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
    deduped = list(seen.values())
    print(f"[dedup] After exact dedup: {len(deduped)}", flush=True)

    # Within-batch similarity dedup: compare new decisions against each other
    batch_unique: list[Decision] = []
    for d in deduped:
        new_text = f"{d.question or ''} {d.decision or ''}".strip()
        is_dup = False
        for kept in batch_unique:
            kept_text = f"{kept.question or ''} {kept.decision or ''}".strip()
            sim = _text_similarity(new_text, kept_text)
            if sim >= similarity_threshold:
                print(f"[dedup] Jaccard batch dup (sim={sim:.3f}): '{new_text[:60]}...'", flush=True)
                is_dup = True
                break
        if not is_dup:
            batch_unique.append(d)
    print(f"[dedup] After batch Jaccard: {len(batch_unique)}", flush=True)

    # Cross-reference against all existing decisions (pending, approved, etc.)
    if existing_decisions:
        result = []
        for d in batch_unique:
            new_text = f"{d.question or ''} {d.decision or ''}".strip()
            is_dup = False
            for existing in existing_decisions:
                existing_text = f"{existing.question or ''} {existing.decision or ''}".strip()
                sim = _text_similarity(new_text, existing_text)
                if sim >= similarity_threshold:
                    print(f"[dedup] Jaccard cross dup (sim={sim:.3f}): '{new_text[:60]}...'", flush=True)
                    is_dup = True
                    break
            if not is_dup:
                result.append(d)
        print(f"[dedup] After cross-ref Jaccard: {len(result)}", flush=True)
    else:
        result = batch_unique

    # LLM-based semantic dedup pass (Haiku — cheap/fast)
    if use_llm and len(result) >= 1:
        print(f"[dedup] Sending {len(result)} candidates to Haiku LLM dedup...", flush=True)
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
    """Use Haiku to catch semantic duplicates that Jaccard misses."""
    import dspy
    from plumb.programs.decision_deduplicator import DecisionDeduplicator

    candidates_str = "\n".join(
        _format_decision_line(i + 1, d) for i, d in enumerate(candidates)
    )
    recent_existing = existing_decisions[-50:] if existing_decisions else []
    existing_str = "\n".join(
        _format_decision_line(i + 1, d) for i, d in enumerate(recent_existing)
    ) or "(none)"

    print(f"[dedup:llm] Candidates sent to Haiku:\n{candidates_str}", flush=True)
    print(f"[dedup:llm] Existing context ({len(recent_existing)} decisions):\n{existing_str}", flush=True)

    from plumb.programs import get_program_lm

    override_lm = get_program_lm("decision_deduplicator")
    lm = override_lm or dspy.LM("anthropic/claude-haiku-4-5-20251001", max_tokens=32000)
    deduplicator = DecisionDeduplicator()
    with dspy.context(lm=lm):
        unique_indices = deduplicator(
            candidates=candidates_str, existing=existing_str
        )

    print(f"[dedup:llm] Haiku returned unique_indices: {unique_indices}", flush=True)

    # Convert 1-based indices to 0-based, filter to valid range
    valid = []
    for idx in unique_indices:
        zero_based = idx - 1
        if 0 <= zero_based < len(candidates):
            valid.append(zero_based)
    kept = [candidates[i] for i in valid]
    print(f"[dedup:llm] Keeping {len(kept)}/{len(candidates)} candidates (indices {valid})", flush=True)
    return kept
