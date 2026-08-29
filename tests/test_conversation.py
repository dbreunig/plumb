from plumb.conversation import (
    ConversationTurn,
    Chunk,
    estimate_tokens,
    reduce_noise,
    chunk_conversation,
)


def _t(role, content, ordinal=0, timestamp=None):
    return ConversationTurn(
        agent="claude", session_id="S", ordinal=ordinal,
        role=role, content=content, timestamp=timestamp,
    )


class TestEstimateTokens:
    def test_basic(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 400) == 100

    def test_empty(self):
        assert estimate_tokens("") == 0


class TestReduceNoise:
    def test_replaces_large_file_reads(self):
        # plumb:req-74d0f651
        big_content = "```python\n" + "x = 1\n" * 600 + "```"
        turns = [_t("assistant", big_content)]
        result = reduce_noise(turns)
        assert len(result) == 1
        assert result[0].content.startswith("[file read:")

    def test_preserves_small_turns(self):
        turns = [_t("user", "hello")]
        result = reduce_noise(turns)
        assert result[0].content == "hello"

    def test_preserves_large_non_file_turns(self):
        big_content = "This is a long discussion " * 200
        turns = [_t("assistant", big_content)]
        result = reduce_noise(turns)
        assert result[0].content == big_content


class TestChunkConversation:
    def test_empty_input(self):
        assert chunk_conversation([]) == []

    def test_single_group(self):
        # plumb:req-7f96b754
        turns = [
            _t("user", "hello", 0, timestamp="t1"),
            _t("assistant", "hi", 1, timestamp="t2"),
        ]
        chunks = chunk_conversation(turns)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert len(chunks[0].turns) == 2

    def test_multiple_groups(self):
        turns = [
            _t("user", "q1", 0, timestamp="t1"),
            _t("assistant", "a1", 1, timestamp="t2"),
            _t("user", "q2", 2, timestamp="t3"),
            _t("assistant", "a2", 3, timestamp="t4"),
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
            _t("user", "start", 0),
            _t("assistant", big, 1),
        ]
        chunks = chunk_conversation(turns, max_tokens=6000)
        assert chunks
        for c in chunks:
            assert estimate_tokens(c.text) <= 6000 or c.truncated

    def test_oversized_single_turn_is_truncated(self):
        big = "y " * 40000  # ~20k tokens in one user turn
        chunks = chunk_conversation([_t("user", big, 0)], max_tokens=6000)
        assert len(chunks) == 1
        assert chunks[0].truncated is True
        assert estimate_tokens(chunks[0].text) <= 6000 + 100  # header slack

    def test_timestamps(self):
        turns = [
            _t("user", "q", 0, timestamp="2025-01-01"),
            _t("assistant", "a", 1, timestamp="2025-01-02"),
        ]
        chunks = chunk_conversation(turns)
        assert chunks[0].start_timestamp == "2025-01-01"
        assert chunks[0].end_timestamp == "2025-01-02"

    def test_chunk_text_property(self):
        chunk = Chunk(
            chunk_index=0,
            agent="claude",
            session_id="S",
            turn_start=0,
            turn_end=1,
            turns=[
                _t("user", "hello", 0),
                _t("assistant", "world", 1),
            ],
        )
        assert "[user]: hello" in chunk.text
        assert "[assistant]: world" in chunk.text


class TestPrepareForDigest:
    def test_truncate_oversized_returns_same_object_when_small(self):
        from plumb.conversation import truncate_oversized
        t = ConversationTurn(agent="claude", session_id="S", ordinal=0, role="user", content="hi")
        assert truncate_oversized(t) is t

    def test_matches_what_chunk_conversation_digests(self):
        from plumb.conversation import (
            DEFAULT_MAX_TOKENS, chunk_conversation, prepare_for_digest, reduce_noise, render_turn,
        )
        big = "x " * (DEFAULT_MAX_TOKENS * 4)  # well over the budget; not a file read
        turns = [ConversationTurn(agent="claude", session_id="S", ordinal=2, role="user", content="bye"),
                 ConversationTurn(agent="claude", session_id="S", ordinal=0, role="user", content="hi"),
                 ConversationTurn(agent="claude", session_id="S", ordinal=1, role="assistant", content=big)]
        chunk = next(c for c in chunk_conversation(reduce_noise(turns)) if c.turn_start <= 1 <= c.turn_end)
        assert chunk.truncated
        prepared = prepare_for_digest(turns)
        assert [t.ordinal for t in prepared] == [0, 1, 2]
        by_ord = {t.ordinal: t for t in chunk.turns}
        for t in prepared:
            if t.ordinal in by_ord:
                assert render_turn(t) == render_turn(by_ord[t.ordinal])
        assert len(prepared[1].content) == DEFAULT_MAX_TOKENS * 4
