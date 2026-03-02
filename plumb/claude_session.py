"""Read Claude Code native session files for conversation extraction.

Claude Code stores session data at ~/.claude/projects/<encoded-path>/<uuid>.jsonl.
This module discovers and parses those files to extract conversation turns.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from plumb.conversation import ConversationTurn

logger = logging.getLogger(__name__)


def encode_project_path(repo_root: Path) -> str:
    """Convert an absolute path to Claude Code's encoded directory name.

    /Users/foo/myrepo -> -Users-foo-myrepo
    """
    path_str = str(repo_root.resolve()).rstrip("/")
    return path_str.replace("/", "-")


def find_session_dir(repo_root: Path) -> Optional[Path]:
    """Return ~/.claude/projects/<encoded>/ if it exists."""
    encoded = encode_project_path(repo_root)
    session_dir = Path.home() / ".claude" / "projects" / encoded
    if session_dir.is_dir():
        return session_dir
    return None


def list_session_files(
    session_dir: Path, modified_after: Optional[datetime] = None
) -> list[Path]:
    """List *.jsonl files, optionally filtering by mtime, sorted by mtime ascending."""
    files = list(session_dir.glob("*.jsonl"))
    if modified_after is not None:
        ts = modified_after.timestamp()
        files = [f for f in files if f.stat().st_mtime >= ts]
    files.sort(key=lambda f: f.stat().st_mtime)
    return files


def _parse_session_entry(entry: dict) -> Optional[ConversationTurn]:
    """Parse one JSONL line from a Claude Code session file.

    Returns a ConversationTurn or None if the entry should be skipped.
    """
    entry_type = entry.get("type")
    if entry_type not in ("user", "assistant"):
        return None

    if entry.get("isSidechain") or entry.get("isMeta"):
        return None

    timestamp = entry.get("timestamp")
    message = entry.get("message", {})
    content = message.get("content", "")

    if entry_type == "user":
        # Skip entries where content is a list (tool_results)
        if not isinstance(content, str):
            return None
        return ConversationTurn(role="user", content=content, timestamp=timestamp)

    # Assistant entries: content is a list with one block per JSONL line
    if not isinstance(content, list) or not content:
        return None

    block = content[0]
    block_type = block.get("type")

    if block_type == "text":
        text = block.get("text", "")
        if text:
            return ConversationTurn(role="assistant", content=text, timestamp=timestamp)

    elif block_type == "tool_use":
        name = block.get("name", "unknown")
        return ConversationTurn(
            role="assistant",
            content=f"[tool: {name}]",
            timestamp=timestamp,
        )

    # Skip thinking blocks and anything else
    return None


def parse_session_file(
    path: Path, since: Optional[datetime] = None
) -> list[ConversationTurn]:
    """Read one session JSONL file and return parsed conversation turns."""
    turns: list[ConversationTurn] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        logger.warning("Could not read session file: %s", path)
        return turns

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        turn = _parse_session_entry(entry)
        if turn is None:
            continue

        if since and turn.timestamp:
            try:
                turn_dt = datetime.fromisoformat(
                    turn.timestamp.replace("Z", "+00:00")
                )
                if turn_dt <= since:
                    continue
            except (ValueError, TypeError):
                pass

        turns.append(turn)
    return turns


def _commit_sha_to_datetime(repo_root: Path, sha: str) -> Optional[datetime]:
    """Convert a commit SHA to its committed datetime using gitpython."""
    try:
        from git import Repo

        repo = Repo(repo_root)
        commit = repo.commit(sha)
        return commit.committed_datetime
    except Exception:
        logger.debug("Could not resolve commit SHA %s to datetime", sha)
        return None


def read_claude_sessions(
    repo_root: Path, since_commit: Optional[str] = None
) -> list[ConversationTurn]:
    """Top-level orchestrator: find and parse all relevant Claude Code session files.

    1. Find session directory for this repo
    2. Convert last_commit SHA to datetime (for filtering)
    3. List session files modified after that datetime
    4. Parse each file, merge turns, sort by timestamp
    """
    session_dir = find_session_dir(repo_root)
    if session_dir is None:
        return []

    commit_dt: Optional[datetime] = None
    if since_commit:
        commit_dt = _commit_sha_to_datetime(repo_root, since_commit)

    files = list_session_files(session_dir, modified_after=commit_dt)
    if not files:
        return []

    all_turns: list[ConversationTurn] = []
    for f in files:
        turns = parse_session_file(f, since=commit_dt)
        all_turns.extend(turns)

    # Sort by timestamp
    def sort_key(t: ConversationTurn) -> str:
        return t.timestamp or ""

    all_turns.sort(key=sort_key)
    return all_turns
