import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import time

import pytest

from plumb.claude_session import (
    encode_project_path,
    find_session_dir,
    list_session_files,
    _parse_session_entry,
    parse_session_file,
    read_claude_sessions,
)
from plumb.conversation import ConversationTurn


class TestEncodeProjectPath:
    def test_basic_path(self):
        assert encode_project_path(Path("/Users/foo/myrepo")) == "-Users-foo-myrepo"

    def test_trailing_slash(self, tmp_path):
        # resolve() will normalize, but we strip trailing slash explicitly
        p = Path("/Users/foo/bar/")
        assert encode_project_path(p) == "-Users-foo-bar"

    def test_root_path(self):
        assert encode_project_path(Path("/")) == ""


class TestFindSessionDir:
    def test_found(self, tmp_path):
        # Create a fake session dir
        encoded = "-Users-fake-repo"
        session_dir = tmp_path / ".claude" / "projects" / encoded
        session_dir.mkdir(parents=True)

        with patch("plumb.claude_session.Path.home", return_value=tmp_path):
            with patch("plumb.claude_session.encode_project_path", return_value=encoded):
                result = find_session_dir(Path("/Users/fake/repo"))
        assert result == session_dir

    def test_not_found(self, tmp_path):
        with patch("plumb.claude_session.Path.home", return_value=tmp_path):
            result = find_session_dir(Path("/Users/fake/nonexistent"))
        assert result is None


class TestListSessionFiles:
    def test_lists_jsonl_files(self, tmp_path):
        (tmp_path / "a.jsonl").write_text("{}")
        (tmp_path / "b.jsonl").write_text("{}")
        (tmp_path / "c.txt").write_text("")  # not jsonl
        files = list_session_files(tmp_path)
        assert len(files) == 2
        assert all(f.suffix == ".jsonl" for f in files)

    def test_filters_by_mtime(self, tmp_path):
        old = tmp_path / "old.jsonl"
        old.write_text("{}")

        # Set old file mtime to the past
        past = time.time() - 3600
        import os
        os.utime(old, (past, past))

        new = tmp_path / "new.jsonl"
        new.write_text("{}")

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        files = list_session_files(tmp_path, modified_after=cutoff)
        assert len(files) == 1
        assert files[0].name == "new.jsonl"

    def test_sorted_by_mtime(self, tmp_path):
        import os

        a = tmp_path / "a.jsonl"
        a.write_text("{}")
        os.utime(a, (100, 100))

        b = tmp_path / "b.jsonl"
        b.write_text("{}")
        os.utime(b, (200, 200))

        files = list_session_files(tmp_path)
        assert files[0].name == "a.jsonl"
        assert files[1].name == "b.jsonl"

    def test_empty_dir(self, tmp_path):
        assert list_session_files(tmp_path) == []


class TestParseSessionEntry:
    def test_user_text(self):
        entry = {
            "type": "user",
            "isSidechain": False,
            "isMeta": False,
            "message": {"role": "user", "content": "Hello world"},
            "timestamp": "2026-01-01T00:00:00Z",
        }
        turn = _parse_session_entry(entry)
        assert turn is not None
        assert turn.role == "user"
        assert turn.content == "Hello world"
        assert turn.timestamp == "2026-01-01T00:00:00Z"

    def test_user_tool_results_skipped(self):
        entry = {
            "type": "user",
            "isSidechain": False,
            "isMeta": False,
            "message": {"role": "user", "content": [{"type": "tool_result"}]},
            "timestamp": "2026-01-01T00:00:00Z",
        }
        assert _parse_session_entry(entry) is None

    def test_meta_skipped(self):
        entry = {
            "type": "user",
            "isSidechain": False,
            "isMeta": True,
            "message": {"role": "user", "content": "meta stuff"},
            "timestamp": "2026-01-01T00:00:00Z",
        }
        assert _parse_session_entry(entry) is None

    def test_sidechain_skipped(self):
        entry = {
            "type": "assistant",
            "isSidechain": True,
            "isMeta": False,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            "timestamp": "2026-01-01T00:00:00Z",
        }
        assert _parse_session_entry(entry) is None

    def test_assistant_text_block(self):
        entry = {
            "type": "assistant",
            "isSidechain": False,
            "isMeta": False,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Here is the answer"}],
            },
            "timestamp": "2026-01-01T00:01:00Z",
        }
        turn = _parse_session_entry(entry)
        assert turn is not None
        assert turn.role == "assistant"
        assert turn.content == "Here is the answer"

    def test_assistant_tool_use_block(self):
        entry = {
            "type": "assistant",
            "isSidechain": False,
            "isMeta": False,
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Read", "input": {"path": "foo.py"}}],
            },
            "timestamp": "2026-01-01T00:02:00Z",
        }
        turn = _parse_session_entry(entry)
        assert turn is not None
        assert turn.content == "[tool: Read]"

    def test_assistant_thinking_skipped(self):
        entry = {
            "type": "assistant",
            "isSidechain": False,
            "isMeta": False,
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "hmm..."}],
            },
            "timestamp": "2026-01-01T00:00:00Z",
        }
        assert _parse_session_entry(entry) is None

    def test_non_user_assistant_type_skipped(self):
        entry = {"type": "system", "message": {"content": "something"}}
        assert _parse_session_entry(entry) is None

    def test_progress_type_skipped(self):
        entry = {"type": "progress", "data": {}}
        assert _parse_session_entry(entry) is None


class TestParseSessionFile:
    def _make_entry(self, type_, content, ts, is_meta=False, is_sidechain=False):
        if type_ == "user":
            msg_content = content
        else:
            msg_content = content  # already a list for assistant
        return json.dumps({
            "type": type_,
            "isSidechain": is_sidechain,
            "isMeta": is_meta,
            "message": {"role": type_, "content": msg_content},
            "timestamp": ts,
        })

    def test_basic_parsing(self, tmp_path):
        session = tmp_path / "session.jsonl"
        lines = [
            self._make_entry("user", "hello", "2026-01-01T00:00:00Z"),
            self._make_entry("assistant", [{"type": "text", "text": "hi"}], "2026-01-01T00:01:00Z"),
        ]
        session.write_text("\n".join(lines))
        turns = parse_session_file(session)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[1].role == "assistant"

    def test_filters_by_since(self, tmp_path):
        session = tmp_path / "session.jsonl"
        lines = [
            self._make_entry("user", "old", "2026-01-01T00:00:00Z"),
            self._make_entry("user", "new", "2026-01-02T00:00:00Z"),
        ]
        session.write_text("\n".join(lines))

        since = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        turns = parse_session_file(session, since=since)
        assert len(turns) == 1
        assert turns[0].content == "new"

    def test_skips_malformed_lines(self, tmp_path):
        session = tmp_path / "session.jsonl"
        session.write_text("not json\n" + self._make_entry("user", "ok", "2026-01-01T00:00:00Z"))
        turns = parse_session_file(session)
        assert len(turns) == 1

    def test_skips_meta_and_sidechain(self, tmp_path):
        session = tmp_path / "session.jsonl"
        lines = [
            self._make_entry("user", "meta", "2026-01-01T00:00:00Z", is_meta=True),
            self._make_entry("assistant", [{"type": "text", "text": "side"}], "2026-01-01T00:01:00Z", is_sidechain=True),
            self._make_entry("user", "real", "2026-01-01T00:02:00Z"),
        ]
        session.write_text("\n".join(lines))
        turns = parse_session_file(session)
        assert len(turns) == 1
        assert turns[0].content == "real"


class TestReadClaudeSessions:
    def test_no_session_dir(self, tmp_path):
        with patch("plumb.claude_session.find_session_dir", return_value=None):
            result = read_claude_sessions(tmp_path)
        assert result == []

    def test_merges_multiple_sessions(self, tmp_path):
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()

        # Session 1
        s1 = session_dir / "s1.jsonl"
        s1.write_text(json.dumps({
            "type": "user",
            "isSidechain": False,
            "isMeta": False,
            "message": {"role": "user", "content": "first"},
            "timestamp": "2026-01-01T00:00:00Z",
        }))

        # Session 2
        s2 = session_dir / "s2.jsonl"
        s2.write_text(json.dumps({
            "type": "user",
            "isSidechain": False,
            "isMeta": False,
            "message": {"role": "user", "content": "second"},
            "timestamp": "2026-01-01T00:01:00Z",
        }))

        with patch("plumb.claude_session.find_session_dir", return_value=session_dir):
            result = read_claude_sessions(tmp_path)

        assert len(result) == 2
        # Should be sorted by timestamp
        assert result[0].content == "first"
        assert result[1].content == "second"

    def test_sha_to_datetime_filtering(self, tmp_path):
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()

        s1 = session_dir / "s1.jsonl"
        s1.write_text(json.dumps({
            "type": "user",
            "isSidechain": False,
            "isMeta": False,
            "message": {"role": "user", "content": "hello"},
            "timestamp": "2026-01-02T00:00:00Z",
        }))

        commit_dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        with patch("plumb.claude_session.find_session_dir", return_value=session_dir):
            with patch("plumb.claude_session._commit_sha_to_datetime", return_value=commit_dt):
                result = read_claude_sessions(tmp_path, since_commit="abc123")

        assert len(result) == 1

    def test_unreachable_sha_reads_all(self, tmp_path):
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()

        s1 = session_dir / "s1.jsonl"
        s1.write_text(json.dumps({
            "type": "user",
            "isSidechain": False,
            "isMeta": False,
            "message": {"role": "user", "content": "hello"},
            "timestamp": "2026-01-01T00:00:00Z",
        }))

        with patch("plumb.claude_session.find_session_dir", return_value=session_dir):
            with patch("plumb.claude_session._commit_sha_to_datetime", return_value=None):
                result = read_claude_sessions(tmp_path, since_commit="deadbeef")

        assert len(result) == 1
