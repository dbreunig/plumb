import pytest

from plumb.decision_log import Decision
from plumb.log_view import group_decisions, verify_evidence


def _d(id, sha, agent, **kw):
    return Decision(id=id, status="approved", decision=f"d{id}", commit_sha=sha, agent=agent,
                    created_at=kw.pop("created_at", f"2026-06-01T00:00:0{id[-1]}Z"), **kw)


@pytest.fixture
def transcript(tmp_path):
    """verify_evidence checks the transcript exists before parsing it."""
    p = tmp_path / "x.jsonl"
    p.write_text("")
    return str(p)


def test_group_by_commit_then_agent_uncommitted_first_then_newest():
    ds = [_d("1", "aaa", "claude", created_at="2026-01-01T00:00:00Z"),
          _d("2", "aaa", "codex", created_at="2026-01-01T00:00:00Z"),
          _d("3", None, "pi"),
          _d("4", "aaa", "claude", created_at="2026-01-01T00:00:00Z"),
          _d("5", "bbb", None, created_at="2026-05-01T00:00:00Z")]
    grouped = group_decisions(ds)
    assert list(grouped.keys()) == ["uncommitted", "bbb", "aaa"]   # uncommitted, then newest commit first
    assert [d.id for d in grouped["aaa"]["claude"]] == ["1", "4"]
    assert list(grouped["aaa"].keys()) == ["claude", "codex"]
    assert list(grouped["bbb"].keys()) == ["unknown"]               # agent None -> "unknown"


def test_verify_evidence_ok_stale_unverifiable(monkeypatch, transcript):
    from plumb.conversation import evidence_digest_for, reduce_noise
    from plumb.traces import Turn
    turns = [Turn(agent="claude", session_id="S", ordinal=i, role="user", content=f"t{i}") for i in range(4)]
    digest = evidence_digest_for(reduce_noise(turns), 1, 2)
    good = _d("1", "aaa", "claude", session_id="S", source_path=transcript, turn_range=[1, 2], evidence_digest=digest)
    bad = _d("2", "aaa", "claude", session_id="S", source_path=transcript, turn_range=[1, 2], evidence_digest="0" * 64)
    gone = _d("3", "aaa", "claude", session_id="S", source_path=transcript, turn_range=[10, 12], evidence_digest=digest)

    class FakeSource:
        name = "claude"
        def discover(self, repo_root, since): return []
        def parse(self, ref, since):
            assert since is None and ref.path == transcript and ref.session_id == "S"
            return turns
    monkeypatch.setattr("plumb.log_view._source_for", lambda agent: FakeSource())

    assert verify_evidence(good) == "ok"
    assert verify_evidence(bad) == "stale"
    assert verify_evidence(gone) == "stale"            # range no longer present
    assert verify_evidence(_d("4", "aaa", "claude")) == "unverifiable"   # no provenance
    monkeypatch.setattr("plumb.log_view._source_for", lambda agent: None)
    assert verify_evidence(good) == "unverifiable"     # unknown agent


def test_verify_evidence_missing_transcript(monkeypatch):
    d = _d("1", "aaa", "claude", session_id="S", source_path="/nope/x.jsonl", turn_range=[0, 0], evidence_digest="0" * 64)
    monkeypatch.setattr("plumb.log_view._source_for", lambda agent: pytest.fail("must not parse a missing file"))
    assert verify_evidence(d) == "missing"


def test_verify_evidence_parse_failure_is_unverifiable(monkeypatch, transcript):
    d = _d("1", "aaa", "claude", session_id="S", source_path=transcript, turn_range=[0, 0], evidence_digest="0" * 64)
    class Boom:
        name = "claude"
        def parse(self, ref, since): raise OSError("gone")
    monkeypatch.setattr("plumb.log_view._source_for", lambda agent: Boom())
    assert verify_evidence(d) == "unverifiable"


def test_verify_evidence_matches_hook_digest_for_oversized_turn(monkeypatch, transcript):
    """The hook digests the chunk's turns, which chunk_conversation truncates
    when a single turn exceeds the token budget. The verifier must reproduce
    that from the raw session without re-chunking."""
    from plumb.conversation import chunk_conversation, reduce_noise
    from plumb.traces import Turn
    big = "x " * 20000  # ~10k tokens, over the 6000-token default budget; not a file read
    turns = [Turn(agent="claude", session_id="S", ordinal=0, role="user", content="hi"),
             Turn(agent="claude", session_id="S", ordinal=1, role="assistant", content=big),
             Turn(agent="claude", session_id="S", ordinal=2, role="user", content="bye")]
    chunks = chunk_conversation(reduce_noise(turns))
    chunk = next(c for c in chunks if c.turn_start <= 1 <= c.turn_end)
    assert chunk.truncated
    d = _d("1", "aaa", "claude", session_id="S", source_path=transcript,
           turn_range=[chunk.turn_start, chunk.turn_end], evidence_digest=chunk.evidence_digest())

    class FakeSource:
        name = "claude"
        def parse(self, ref, since): return turns
    monkeypatch.setattr("plumb.log_view._source_for", lambda agent: FakeSource())
    assert verify_evidence(d) == "ok"
