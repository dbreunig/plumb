"""Turn rendering and per-session chunking for the decision extractor."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from plumb.traces import SessionRef, ToolCall, Turn

logger = logging.getLogger(__name__)

# Backwards-compatible name; a ConversationTurn *is* a Turn.
ConversationTurn = Turn


class Chunk(BaseModel):
    chunk_index: int
    agent: str
    session_id: str
    turn_start: int
    turn_end: int
    parent_session_id: Optional[str] = None
    start_timestamp: Optional[str] = None
    end_timestamp: Optional[str] = None
    truncated: bool = False
    turns: list[Turn] = Field(default_factory=list)

    @property
    def header(self) -> str:
        parent = f" parent={self.parent_session_id}" if self.parent_session_id else ""
        return f"[agent={self.agent} session={self.session_id}{parent} turns {self.turn_start}-{self.turn_end}]"

    @property
    def text(self) -> str:
        return "\n".join([self.header] + [render_turn(t) for t in self.turns])


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def render_tool_call(tc: ToolCall) -> str:
    if tc.category in ("Edit", "Write", "Read") and tc.file_path:
        head = f"[{tc.category} {tc.file_path}]"
    elif tc.input_summary:
        head = f"[{tc.category}: {tc.input_summary}]"
    else:
        head = f"[{tc.category}: {tc.name}]"
    if tc.result_summary:
        head += f" -> {tc.result_summary}"
    return "  " + head


def render_turn(t: Turn) -> str:
    lines = [f"[{t.role}]: {t.content}" if t.content else f"[{t.role}]"]
    lines += [render_tool_call(tc) for tc in t.tool_calls]
    return "\n".join(lines)


def _looks_like_file_read(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    if stripped.startswith("```"):
        return True
    return bool(re.match(r"^[/.]", stripped) or re.match(r"^\w+[/\\]", stripped))


def reduce_noise(turns: list[Turn]) -> list[Turn]:
    """Replace >500-token turns that look like file reads with a placeholder."""
    result = []
    for turn in turns:
        if estimate_tokens(turn.content) > 500 and _looks_like_file_read(turn.content):
            first_line = turn.content.strip().split("\n")[0].strip("`").strip()[:100]
            result.append(turn.model_copy(update={"content": f"[file read: {first_line}]"}))
        else:
            result.append(turn)
    return result


def _split_at_tool_boundary(turns: list[Turn], max_tokens: int, cut: set[int]) -> list[list[Turn]]:
    """Pack turns into groups of <= max_tokens.

    A single turn that alone exceeds the budget is copied with its content cut
    to max_tokens*4 characters; the copy's id() is added to `cut` so the
    enclosing chunk can be marked truncated.
    """
    chunks, current, current_tokens = [], [], 0
    for turn in turns:
        n = estimate_tokens(render_turn(turn))
        if n > max_tokens:
            turn = turn.model_copy(update={"content": turn.content[: max_tokens * 4]})
            cut.add(id(turn))
            n = estimate_tokens(render_turn(turn))
        if current and current_tokens + n > max_tokens:
            chunks.append(current)
            current, current_tokens = [turn], n
        else:
            current.append(turn)
            current_tokens += n
    if current:
        chunks.append(current)
    return chunks


def _chunk_session(turns: list[Turn], max_tokens: int, cut: set[int]) -> list[list[Turn]]:
    groups: list[list[Turn]] = []
    current: list[Turn] = []
    for turn in turns:
        if turn.role == "user" and current:
            groups.append(current)
            current = [turn]
        else:
            current.append(turn)
    if current:
        groups.append(current)
    final: list[list[Turn]] = []
    for g in groups:
        if sum(estimate_tokens(render_turn(t)) for t in g) <= max_tokens:
            final.append(g)
        else:
            final.extend(_split_at_tool_boundary(g, max_tokens, cut))
    return final


def parents_from_refs(refs: dict[tuple[str, str], SessionRef]) -> dict[tuple[str, str], str]:
    """(agent, session_id) -> parent session id, for refs that have one."""
    return {key: ref.parent_session_id for key, ref in refs.items() if ref.parent_session_id}


def chunk_conversation(
    turns: list[Turn],
    max_tokens: int = 6000,
    parents: dict[tuple[str, str], str] | None = None,
) -> list[Chunk]:
    """Group by (agent, session_id) in first-seen order, then by user turn.

    One-turn overlap between consecutive chunks of the *same* session; the
    turn range excludes the overlap so provenance points at new content only.
    `parents` maps (agent, session_id) -> parent session id for subagent
    sessions; it is rendered into the chunk header.
    """
    if not turns:
        return []
    parents = parents or {}
    sessions: dict[tuple[str, str], list[Turn]] = {}
    for t in turns:
        sessions.setdefault((t.agent, t.session_id), []).append(t)

    chunks: list[Chunk] = []
    for (agent, session_id), sturns in sessions.items():
        sturns = sorted(sturns, key=lambda t: t.ordinal)
        cut: set[int] = set()
        groups = _chunk_session(sturns, max_tokens, cut)
        for i, group in enumerate(groups):
            overlap = [groups[i - 1][-1]] if i > 0 else []
            all_turns = overlap + group
            stamps = [t.timestamp for t in all_turns if t.timestamp]
            chunks.append(Chunk(
                chunk_index=len(chunks), agent=agent, session_id=session_id,
                turn_start=group[0].ordinal, turn_end=group[-1].ordinal,
                parent_session_id=parents.get((agent, session_id)),
                start_timestamp=stamps[0] if stamps else None,
                end_timestamp=stamps[-1] if stamps else None,
                truncated=any(id(t) in cut for t in all_turns),
                turns=all_turns,
            ))
    return chunks


def read_conversation_with_refs(
    repo_root: Path,
    since_commit: str | None = None,
    since_datetime: str | None = None,
) -> tuple[list[Turn], dict[tuple[str, str], SessionRef]]:
    """Stage 1: every registered TraceSource, every session in this repo since the cutoff.

    Returns the turns plus the SessionRef for each (agent, session_id), which
    the hook needs for source_path / parent_session_id provenance. A source or
    session that fails to read is logged and skipped; the rest still load.
    """
    from plumb.traces import all_sources
    from plumb.traces.repo import resolve_cutoff

    cutoff = resolve_cutoff(repo_root, since_commit, since_datetime)
    turns: list[Turn] = []
    refs: dict[tuple[str, str], SessionRef] = {}
    for source in all_sources():
        try:
            found = source.discover(repo_root, cutoff)
        except Exception:
            logger.warning("Trace source %s failed to discover sessions", source.name, exc_info=True)
            continue
        for ref in found:
            try:
                parsed = source.parse(ref, cutoff)
            except Exception:
                logger.warning(
                    "Skipping %s session %s (%s): failed to parse",
                    ref.agent, ref.session_id, ref.path, exc_info=True,
                )
                continue
            refs[(ref.agent, ref.session_id)] = ref
            turns.extend(parsed)
    return turns, refs


def read_conversation(
    repo_root: Path,
    since_commit: str | None = None,
    since_datetime: str | None = None,
) -> list[Turn]:
    return read_conversation_with_refs(repo_root, since_commit, since_datetime)[0]
