import json
import time
from datetime import datetime, timezone, timedelta

import pytest

from plumb.decision_log import Decision, FileRef, append_decisions
from plumb.search import search_decisions, bm25_scores, tokenize


def _seed(repo):
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [
        Decision(id="d1", status="recorded", approved_by="auto", agent="claude", made_by="user", confidence=0.9, branch="main",
                 question="Cache strategy?", decision="Use an in-memory dict cache with TTL.",
                 file_refs=[FileRef(file="src/cache.py", lines=[1, 9])], commit_sha="aaa",
                 session_id="019a0000abcd", turn_range=[4, 5],
                 created_at=(t0 + timedelta(days=1)).isoformat()),
        Decision(id="d2", status="approved", agent="codex", made_by="agent", confidence=0.6, branch="main",
                 question="Auth tokens?", decision="Tokens expire after 30 minutes of inactivity.",
                 file_refs=[FileRef(file="src/auth.py", lines=[42, 58])], commit_sha="bbb",
                 created_at=(t0 + timedelta(days=2)).isoformat()),
        Decision(id="d3", status="pending", agent="pi", made_by="agent", confidence=None, branch="feat",
                 question="Cache eviction?", decision="Evict least-recently-used cache entries.",
                 created_at=(t0 + timedelta(days=3)).isoformat()),
        Decision(id="d4", status="ignored", decision="cache cache cache", created_at=t0.isoformat()),
        Decision(id="d5", status="rejected", agent="claude", made_by="agent", confidence=0.7, branch="main",
                 question="Cache backend?", decision="Use redis as the cache backend.",
                 created_at=(t0 + timedelta(days=4)).isoformat()),
    ]
    append_decisions(repo, rows[:2], branch="main")
    append_decisions(repo, rows[2:3], branch="feat")
    append_decisions(repo, rows[3:], branch="main")


def test_tokenize_and_bm25_prefer_matching_docs():
    assert tokenize("Use an In-Memory dict cache, with TTL.") == ["use", "an", "in", "memory", "dict", "cache", "with", "ttl"]
    docs = ["cache with ttl", "auth tokens expire", "cache eviction lru"]
    scores = bm25_scores(docs, "cache ttl")
    assert scores[0] > scores[2] > scores[1] == 0


def test_search_empty_log(initialized_repo):
    assert search_decisions(initialized_repo, query="cache") == []
    assert search_decisions(initialized_repo) == []


def test_search_relevance_default_and_ignored_rejected_excluded(initialized_repo):
    _seed(initialized_repo)
    hits = search_decisions(initialized_repo, query="cache")
    # d4 ignored, d5 rejected by default. Both survivors have tf=2 for "cache";
    # BM25 length normalization puts the shorter d3 first.
    assert sorted(h.decision.id for h in hits) == ["d1", "d3"]
    assert hits[0].score >= hits[1].score > 0


def test_search_any_token_prefilter_ranks_partial_matches(initialized_repo):
    """No SQL token prefilter: BM25 runs over every row passing the non-text
    filters, so a doc matching only some query tokens still ranks (below full
    matches) instead of vanishing, and idf reflects the whole corpus."""
    _seed(initialized_repo)
    hits = search_decisions(initialized_repo, query="cache ttl")
    assert [h.decision.id for h in hits] == ["d1", "d3"]        # TTL decision first; eviction still present
    assert hits[0].score > hits[1].score


def test_search_date_sort_and_filters(initialized_repo):
    _seed(initialized_repo)
    assert [h.decision.id for h in search_decisions(initialized_repo)] == ["d3", "d2", "d1"]   # newest first
    assert [h.decision.id for h in search_decisions(initialized_repo, agent=["codex"])] == ["d2"]
    assert [h.decision.id for h in search_decisions(initialized_repo, status=["recorded", "pending"])] == ["d3", "d1"]
    assert [h.decision.id for h in search_decisions(initialized_repo, branch="feat")] == ["d3"]
    assert [h.decision.id for h in search_decisions(initialized_repo, file="src/auth.py")] == ["d2"]
    assert [h.decision.id for h in search_decisions(initialized_repo, made_by="user")] == ["d1"]
    assert [h.decision.id for h in search_decisions(initialized_repo, since="2026-08-02T12:00:00Z")] == ["d3", "d2"]
    assert [h.decision.id for h in search_decisions(initialized_repo, since="2026-08-03T00:00:00Z")] == ["d3", "d2"]  # inclusive, no tz shift
    assert [h.decision.id for h in search_decisions(initialized_repo, since="2026-08-03T12:00:00Z")] == ["d3"]
    assert [h.decision.id for h in search_decisions(initialized_repo, status=["ignored"])] == ["d4"]
    assert [h.decision.id for h in search_decisions(initialized_repo, status=["rejected"])] == ["d5"]


def test_search_confidence_sort_and_limit(initialized_repo):
    _seed(initialized_repo)
    assert [h.decision.id for h in search_decisions(initialized_repo, sort="confidence")] == ["d1", "d2", "d3"]  # nulls last
    assert len(search_decisions(initialized_repo, limit=2)) == 2


def test_search_since_git_ref(initialized_repo):
    _seed(initialized_repo)
    hits = search_decisions(initialized_repo, since="HEAD")   # HEAD is the fixture's initial commit (today) -> nothing seeded after it
    assert hits == []


def test_search_since_bad_ref_raises(initialized_repo):
    _seed(initialized_repo)
    with pytest.raises(ValueError):
        search_decisions(initialized_repo, since="no-such-ref")


def test_search_pre_provenance_shard(initialized_repo):
    """Old shards lack agent/session/approved_by columns and have only empty file_refs."""
    old = {
        "id": "old-1", "status": "approved", "question": "Framework?", "decision": "Use DSPy for LLM programs.",
        "made_by": "llm", "commit_sha": None, "branch": "main", "ref_status": "ok", "conversation_available": False,
        "file_refs": [], "related_requirement_ids": [], "confidence": 0.9, "chunk_index": None,
        "conversation_truncated": False, "rejection_reason": None, "user_note": None, "synced_at": None,
        "reviewed_at": None, "created_at": "2026-03-01T18:11:19.639876+00:00",
    }
    import json
    (initialized_repo / ".plumb" / "decisions" / "main.jsonl").write_text(json.dumps(old) + "\n")
    hits = search_decisions(initialized_repo, query="dspy")
    assert [h.decision.id for h in hits] == ["old-1"]
    assert hits[0].decision.file_refs == [] and hits[0].decision.agent is None
    # Filters on columns the shard does not have match nothing rather than erroring.
    assert search_decisions(initialized_repo, agent=["claude"]) == []
    assert search_decisions(initialized_repo, file="src/x.py") == []


def test_bm25_idf_over_full_corpus_and_no_match_yields_nothing(initialized_repo):
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [Decision(id=f"n{i}", status="approved", branch="main", decision=f"Unrelated note number {i} about auth tokens.",
                     created_at=(t0 + timedelta(hours=i)).isoformat()) for i in range(8)]
    rows += [
        Decision(id="c1", status="approved", branch="main", decision="Use a cache with TTL.", created_at=(t0 + timedelta(days=1)).isoformat()),
        Decision(id="c2", status="approved", branch="main", decision="Evict cache entries by LRU.", created_at=(t0 + timedelta(days=2)).isoformat()),
    ]
    append_decisions(initialized_repo, rows, branch="main")
    hits = search_decisions(initialized_repo, query="cache")
    # idf = log(1 + (10 - 2 + 0.5) / (2 + 0.5)) > 1 only if idf sees all 10 docs, not just the 2 containing the token.
    assert sorted(h.decision.id for h in hits) == ["c1", "c2"]
    assert hits[0].score > 1.0
    assert search_decisions(initialized_repo, query="zzzz") == []
    assert len(search_decisions(initialized_repo, query="cache", limit=1)) == 1


def test_search_sql_sort_and_limit_on_large_shard(initialized_repo):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [Decision(id=f"big-{i:05d}", status="approved", branch="main", decision=f"decision {i}",
                     confidence=(i % 100) / 100, created_at=(t0 + timedelta(minutes=(i * 7919) % 5000)).isoformat())
            for i in range(5000)]
    path = initialized_repo / ".plumb" / "decisions" / "main.jsonl"
    path.write_text("".join(json.dumps(r.model_dump()) + "\n" for r in rows))

    t = time.perf_counter()
    hits = search_decisions(initialized_repo, sort="date", limit=10)
    assert time.perf_counter() - t < 1.0
    assert len(hits) == 10
    expected = sorted(rows, key=lambda r: (r.created_at, r.id), reverse=True)[:10]
    assert [h.decision.id for h in hits] == [r.id for r in expected]

    hits = search_decisions(initialized_repo, sort="confidence", limit=5)
    assert [h.decision.confidence for h in hits] == [0.99] * 5
    assert len(search_decisions(initialized_repo, limit=0)) == 5000
