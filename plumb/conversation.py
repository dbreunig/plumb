from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class ConversationTurn(BaseModel):
    role: str
    content: str = ""
    timestamp: Optional[str] = None


class Chunk(BaseModel):
    chunk_index: int
    start_timestamp: Optional[str] = None
    end_timestamp: Optional[str] = None
    truncated: bool = False
    turns: list[ConversationTurn] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(f"[{t.role}]: {t.content}" for t in self.turns)


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def locate_conversation_log(config_path: str | None = None) -> Path | None:
    """Check config path, then auto-detect common Claude Code log locations."""
    if config_path:
        p = Path(config_path)
        if p.exists():
            return p

    # Auto-detect common Claude Code conversation log locations
    home = Path.home()
    candidates = [
        home / ".claude" / "conversations.jsonl",
        home / ".claude" / "conversation_log.jsonl",
        home / ".claude" / "logs" / "conversation.jsonl",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def read_conversation_log(
    path: Path, since: str | None = None
) -> list[ConversationTurn]:
    """Read JSONL conversation log, filtering by timestamp if since is provided."""
    turns: list[ConversationTurn] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            turn = ConversationTurn(
                role=data.get("role", "unknown"),
                content=data.get("content", ""),
                timestamp=data.get("timestamp"),
            )
            if since and turn.timestamp and turn.timestamp <= since:
                continue
            turns.append(turn)
        except (json.JSONDecodeError, Exception):
            continue
    return turns


def _looks_like_file_read(content: str) -> bool:
    """Heuristic: content begins with a file path or code fence."""
    stripped = content.strip()
    if not stripped:
        return False
    if stripped.startswith("```"):
        return True
    # Looks like a file path (starts with / or ./ or contains common extensions)
    if re.match(r"^[/.]", stripped) or re.match(r"^\w+[/\\]", stripped):
        return True
    return False


def reduce_noise(turns: list[ConversationTurn]) -> list[ConversationTurn]:
    """Replace tool result turns >500 tokens that look like file reads
    with a short placeholder."""
    result = []
    for turn in turns:
        tokens = estimate_tokens(turn.content)
        if tokens > 500 and _looks_like_file_read(turn.content):
            # Extract filename heuristic
            first_line = turn.content.strip().split("\n")[0]
            filename = first_line.strip("`").strip()
            if len(filename) > 100:
                filename = filename[:100]
            result.append(
                ConversationTurn(
                    role=turn.role,
                    content=f"[file read: {filename}]",
                    timestamp=turn.timestamp,
                )
            )
        else:
            result.append(turn)
    return result


def _split_at_tool_boundary(turns: list[ConversationTurn], max_tokens: int) -> list[list[ConversationTurn]]:
    """Split a list of turns at tool call boundaries to stay under max_tokens."""
    chunks: list[list[ConversationTurn]] = []
    current: list[ConversationTurn] = []
    current_tokens = 0

    for turn in turns:
        turn_tokens = estimate_tokens(turn.content)
        if current and current_tokens + turn_tokens > max_tokens:
            # Try to split at a role boundary
            chunks.append(current)
            current = [turn]
            current_tokens = turn_tokens
        else:
            current.append(turn)
            current_tokens += turn_tokens

    if current:
        chunks.append(current)
    return chunks


def read_conversation(
    repo_root: Path,
    config_path: str | None = None,
    since_commit: str | None = None,
) -> list[ConversationTurn]:
    """Unified entry point for reading conversation turns.

    If config_path is set and points to an existing file, use the legacy
    read_conversation_log(). Otherwise, auto-detect Claude Code session files.
    """
    if config_path:
        log_path = locate_conversation_log(config_path)
        if log_path is not None:
            return read_conversation_log(log_path, since=since_commit)

    from plumb.claude_session import read_claude_sessions

    return read_claude_sessions(repo_root, since_commit=since_commit)


def chunk_conversation(
    turns: list[ConversationTurn], max_tokens: int = 6000
) -> list[Chunk]:
    """Chunk conversation into groups bounded by user turn boundaries.

    1. Group by user turn boundary (user msg + all following assistant turns)
    2. If chunk > max_tokens, split at turn boundaries
    3. One-turn overlap between chunks
    """
    if not turns:
        return []

    # Step 1: Group by user turn
    groups: list[list[ConversationTurn]] = []
    current_group: list[ConversationTurn] = []

    for turn in turns:
        if turn.role == "user" and current_group:
            groups.append(current_group)
            current_group = [turn]
        else:
            current_group.append(turn)

    if current_group:
        groups.append(current_group)

    # Step 2: Check sizes, split oversized groups
    final_groups: list[list[ConversationTurn]] = []
    for group in groups:
        group_tokens = sum(estimate_tokens(t.content) for t in group)
        if group_tokens <= max_tokens:
            final_groups.append(group)
        else:
            # Split at turn boundaries
            sub_chunks = _split_at_tool_boundary(group, max_tokens)
            final_groups.extend(sub_chunks)

    # Step 3: Build Chunk objects with one-turn overlap
    chunks: list[Chunk] = []
    for i, group in enumerate(final_groups):
        overlap_turns = []
        if i > 0 and final_groups[i - 1]:
            overlap_turns = [final_groups[i - 1][-1]]

        all_turns = overlap_turns + group
        timestamps = [t.timestamp for t in all_turns if t.timestamp]

        chunks.append(
            Chunk(
                chunk_index=i,
                start_timestamp=timestamps[0] if timestamps else None,
                end_timestamp=timestamps[-1] if timestamps else None,
                truncated=False,
                turns=all_turns,
            )
        )

    return chunks
