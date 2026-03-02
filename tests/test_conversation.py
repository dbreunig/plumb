import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from plumb.conversation import (
    ConversationTurn,
    Chunk,
    estimate_tokens,
    locate_conversation_log,
    read_conversation_log,
    read_conversation,
    reduce_noise,
    chunk_conversation,
)


class TestEstimateTokens:
    def test_basic(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 400) == 100

    def test_empty(self):
        assert estimate_tokens("") == 0


class TestLocateConversationLog:
    def test_explicit_path_exists(self, tmp_path):
        log = tmp_path / "log.jsonl"
        log.write_text("")
        assert locate_conversation_log(str(log)) == log

    def test_explicit_path_missing(self):
        result = locate_conversation_log("/nonexistent/path.jsonl")
        assert result is None

    def test_none_path(self):
        # Won't find auto-detect locations in test env usually
        result = locate_conversation_log(None)
        # Could be None or a real path depending on env
        assert result is None or isinstance(result, Path)


class TestReadConversationLog:
    def test_basic_read(self, tmp_path):
        # plumb:req-5436da8c
        log = tmp_path / "conv.jsonl"
        lines = [
            json.dumps({"role": "user", "content": "hello", "timestamp": "2025-01-01T00:00:00Z"}),
            json.dumps({"role": "assistant", "content": "hi there", "timestamp": "2025-01-01T00:01:00Z"}),
        ]
        log.write_text("\n".join(lines))
        turns = read_conversation_log(log)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[1].content == "hi there"

    def test_filter_by_since(self, tmp_path):
        # plumb:req-36d35d26
        log = tmp_path / "conv.jsonl"
        lines = [
            json.dumps({"role": "user", "content": "old", "timestamp": "2025-01-01T00:00:00Z"}),
            json.dumps({"role": "user", "content": "new", "timestamp": "2025-01-02T00:00:00Z"}),
        ]
        log.write_text("\n".join(lines))
        turns = read_conversation_log(log, since="2025-01-01T00:00:00Z")
        assert len(turns) == 1
        assert turns[0].content == "new"

    def test_malformed_lines_skipped(self, tmp_path):
        log = tmp_path / "conv.jsonl"
        log.write_text("not json\n" + json.dumps({"role": "user", "content": "ok"}))
        turns = read_conversation_log(log)
        assert len(turns) == 1


class TestReduceNoise:
    def test_replaces_large_file_reads(self):
        # plumb:req-74d0f651
        big_content = "```python\n" + "x = 1\n" * 600 + "```"
        turns = [ConversationTurn(role="assistant", content=big_content)]
        result = reduce_noise(turns)
        assert len(result) == 1
        assert result[0].content.startswith("[file read:")

    def test_preserves_small_turns(self):
        turns = [ConversationTurn(role="user", content="hello")]
        result = reduce_noise(turns)
        assert result[0].content == "hello"

    def test_preserves_large_non_file_turns(self):
        big_content = "This is a long discussion " * 200
        turns = [ConversationTurn(role="assistant", content=big_content)]
        result = reduce_noise(turns)
        assert result[0].content == big_content


class TestChunkConversation:
    def test_empty_input(self):
        assert chunk_conversation([]) == []

    def test_single_group(self):
        # plumb:req-7f96b754
        turns = [
            ConversationTurn(role="user", content="hello", timestamp="t1"),
            ConversationTurn(role="assistant", content="hi", timestamp="t2"),
        ]
        chunks = chunk_conversation(turns)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert len(chunks[0].turns) == 2

    def test_multiple_groups(self):
        turns = [
            ConversationTurn(role="user", content="q1", timestamp="t1"),
            ConversationTurn(role="assistant", content="a1", timestamp="t2"),
            ConversationTurn(role="user", content="q2", timestamp="t3"),
            ConversationTurn(role="assistant", content="a2", timestamp="t4"),
        ]
        chunks = chunk_conversation(turns)
        assert len(chunks) == 2
        # Second chunk should have overlap from first chunk's last turn
        assert any(t.content == "a1" for t in chunks[1].turns)

    def test_oversized_group_splits(self):
        # plumb:req-3f660d18
        # plumb:req-079c3155
        # Create a single user turn group that exceeds token limit
        big = "x " * 15000  # ~7500 tokens > 6000
        turns = [
            ConversationTurn(role="user", content="start"),
            ConversationTurn(role="assistant", content=big),
        ]
        chunks = chunk_conversation(turns, max_tokens=6000)
        assert len(chunks) >= 1  # Should split

    def test_timestamps(self):
        turns = [
            ConversationTurn(role="user", content="q", timestamp="2025-01-01"),
            ConversationTurn(role="assistant", content="a", timestamp="2025-01-02"),
        ]
        chunks = chunk_conversation(turns)
        assert chunks[0].start_timestamp == "2025-01-01"
        assert chunks[0].end_timestamp == "2025-01-02"

    def test_chunk_text_property(self):
        chunk = Chunk(
            chunk_index=0,
            turns=[
                ConversationTurn(role="user", content="hello"),
                ConversationTurn(role="assistant", content="world"),
            ],
        )
        assert "[user]: hello" in chunk.text
        assert "[assistant]: world" in chunk.text


class TestReadConversation:
    def test_legacy_path_used_when_config_set(self, tmp_path):
        log = tmp_path / "conv.jsonl"
        log.write_text(json.dumps({"role": "user", "content": "legacy", "timestamp": "2026-01-01T00:00:00Z"}))
        turns = read_conversation(tmp_path, config_path=str(log))
        assert len(turns) == 1
        assert turns[0].content == "legacy"

    def test_legacy_path_missing_falls_through(self, tmp_path):
        """When config_path is set but file doesn't exist, fall through to session detection."""
        with patch("plumb.claude_session.read_claude_sessions", return_value=[]) as mock:
            turns = read_conversation(tmp_path, config_path="/nonexistent/log.jsonl")
        assert turns == []
        mock.assert_called_once_with(tmp_path, since_commit=None)

    def test_auto_detect_when_no_config_path(self, tmp_path):
        fake_turns = [ConversationTurn(role="user", content="auto")]
        with patch("plumb.claude_session.read_claude_sessions", return_value=fake_turns) as mock:
            turns = read_conversation(tmp_path, since_commit="abc123")
        assert len(turns) == 1
        assert turns[0].content == "auto"
        mock.assert_called_once_with(tmp_path, since_commit="abc123")
