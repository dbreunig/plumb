import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from plumb.decision_log import (
    Decision,
    FileRef,
    generate_decision_id,
    read_decisions,
    append_decision,
    append_decisions,
    update_decision_status,
    filter_decisions,
    delete_decisions_by_commit,
    deduplicate_decisions,
    _normalize_text,
    _text_similarity,
)


class TestDecisionModel:
    def test_defaults(self):
        d = Decision(id="dec-123")
        assert d.status == "pending"
        assert d.file_refs == []
        assert d.ref_status == "ok"

    def test_full_schema(self):
        d = Decision(
            id="dec-abc",
            status="approved",
            question="Q?",
            decision="A.",
            made_by="user",
            commit_sha="abc123",
            branch="main",
            ref_status="ok",
            conversation_available=True,
            file_refs=[FileRef(file="src/a.py", lines=[1, 2])],
            related_requirement_ids=["req-001"],
            confidence=0.95,
            chunk_index=0,
        )
        assert d.file_refs[0].file == "src/a.py"
        assert d.confidence == 0.95


class TestGenerateDecisionId:
    def test_format(self):
        did = generate_decision_id()
        assert did.startswith("dec-")
        assert len(did) == 16  # dec- + 12 hex chars

    def test_uniqueness(self):
        ids = {generate_decision_id() for _ in range(100)}
        assert len(ids) == 100


class TestReadWriteDecisions:
    def test_empty_log(self, initialized_repo):
        assert read_decisions(initialized_repo) == []

    def test_append_and_read(self, initialized_repo, sample_decisions):
        # plumb:req-fa14efa8
        # plumb:req-bf22567e
        append_decision(initialized_repo, sample_decisions[0])
        result = read_decisions(initialized_repo)
        assert len(result) == 1
        assert result[0].id == "dec-aaa111"

    def test_append_multiple(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions)
        result = read_decisions(initialized_repo)
        assert len(result) == 2

    def test_latest_line_wins(self, initialized_repo, sample_decisions):
        # plumb:req-b2576545
        append_decision(initialized_repo, sample_decisions[0])
        # Append same id with updated status
        updated = sample_decisions[0].model_copy(update={"status": "approved"})
        append_decision(initialized_repo, updated)
        result = read_decisions(initialized_repo)
        assert len(result) == 1
        assert result[0].status == "approved"


class TestUpdateDecisionStatus:
    def test_update_existing(self, initialized_repo, sample_decisions):
        # plumb:req-2aaba138
        append_decision(initialized_repo, sample_decisions[0])
        result = update_decision_status(
            initialized_repo, "dec-aaa111", status="approved"
        )
        assert result is not None
        assert result.status == "approved"
        # Verify in log
        decisions = read_decisions(initialized_repo)
        assert decisions[0].status == "approved"

    def test_update_nonexistent(self, initialized_repo):
        result = update_decision_status(
            initialized_repo, "dec-nonexist", status="approved"
        )
        assert result is None


class TestFilterDecisions:
    def test_filter_by_status(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions)
        update_decision_status(initialized_repo, "dec-aaa111", status="approved")
        pending = filter_decisions(initialized_repo, status="pending")
        assert len(pending) == 1
        assert pending[0].id == "dec-bbb222"

    def test_filter_by_branch(self, initialized_repo, sample_decisions):
        d1 = sample_decisions[0].model_copy(update={"branch": "feat"})
        d2 = sample_decisions[1].model_copy(update={"branch": "main"})
        append_decisions(initialized_repo, [d1, d2])
        feat = filter_decisions(initialized_repo, branch="feat")
        assert len(feat) == 1
        assert feat[0].id == "dec-aaa111"


class TestDeleteDecisionsByCommit:
    def test_delete_matching(self, initialized_repo):
        d1 = Decision(id="dec-1", commit_sha="sha111")
        d2 = Decision(id="dec-2", commit_sha="sha222")
        append_decisions(initialized_repo, [d1, d2])
        removed = delete_decisions_by_commit(initialized_repo, "sha111")
        assert removed == 1
        remaining = read_decisions(initialized_repo)
        assert len(remaining) == 1
        assert remaining[0].id == "dec-2"

    def test_delete_none_matching(self, initialized_repo):
        d = Decision(id="dec-1", commit_sha="sha111")
        append_decision(initialized_repo, d)
        removed = delete_decisions_by_commit(initialized_repo, "sha999")
        assert removed == 0
        assert len(read_decisions(initialized_repo)) == 1

    def test_delete_on_empty(self, initialized_repo):
        removed = delete_decisions_by_commit(initialized_repo, "sha111")
        assert removed == 0


class TestDeduplicateDecisions:
    def test_dedup_same_question_and_decision(self):
        d1 = Decision(id="dec-1", question="Q?", decision="A.", chunk_index=0)
        d2 = Decision(id="dec-2", question="Q?", decision="A.", chunk_index=2)
        result = deduplicate_decisions([d1, d2])
        assert len(result) == 1
        assert result[0].chunk_index == 0

    def test_no_dedup_different_decisions(self):
        d1 = Decision(id="dec-1", question="Q?", decision="A.", chunk_index=0)
        d2 = Decision(id="dec-2", question="Q?", decision="B.", chunk_index=1)
        result = deduplicate_decisions([d1, d2])
        assert len(result) == 2

    def test_case_insensitive(self):
        d1 = Decision(id="dec-1", question="Should we?", decision="Yes", chunk_index=0)
        d2 = Decision(id="dec-2", question="should we?", decision="yes", chunk_index=1)
        result = deduplicate_decisions([d1, d2])
        assert len(result) == 1

    def test_filters_similar_to_existing_approved(self):
        new = [Decision(id="dec-new", question="Use sync or async?", decision="Use sync for simplicity")]
        existing = [Decision(id="dec-old", status="approved", question="Use sync or async?", decision="Use sync for simplicity")]
        result = deduplicate_decisions(new, existing_decisions=existing)
        assert len(result) == 0

    def test_keeps_novel_decisions(self):
        new = [Decision(id="dec-new", question="What cache strategy?", decision="Use Redis")]
        existing = [Decision(id="dec-old", status="approved", question="Use sync or async?", decision="Use sync")]
        result = deduplicate_decisions(new, existing_decisions=existing)
        assert len(result) == 1

    def test_filters_pending_existing(self):
        new = [Decision(id="dec-new", question="Use sync?", decision="Yes sync")]
        existing = [Decision(id="dec-old", status="pending", question="Use sync?", decision="Yes sync")]
        result = deduplicate_decisions(new, existing_decisions=existing)
        assert len(result) == 0  # pending existing SHOULD filter

    def test_no_existing_decisions(self):
        new = [Decision(id="dec-1", question="Q?", decision="A.")]
        result = deduplicate_decisions(new, existing_decisions=None)
        assert len(result) == 1

    def test_similarity_threshold(self):
        """Near-duplicate with slight wording difference should still be filtered."""
        new = [Decision(id="dec-new", question="Should we use synchronous processing?", decision="Use sync for code simplicity")]
        existing = [Decision(id="dec-old", status="approved", question="Should we use sync or async processing?", decision="Use sync for simplicity")]
        result = deduplicate_decisions(new, existing_decisions=existing, similarity_threshold=0.5)
        assert len(result) == 0

    def test_within_batch_similarity_dedup(self):
        """Similar decisions in the same batch should be collapsed."""
        d1 = Decision(id="dec-1", question="Use sync or async?", decision="Use sync for simplicity", chunk_index=0)
        d2 = Decision(id="dec-2", question="Should we use sync or async?", decision="Use sync for simplicity reasons", chunk_index=1)
        result = deduplicate_decisions([d1, d2])
        assert len(result) == 1
        assert result[0].id == "dec-1"  # keeps earliest

    def test_within_batch_different_decisions_kept(self):
        """Genuinely different decisions in the same batch should both survive."""
        d1 = Decision(id="dec-1", question="Use sync or async?", decision="Use sync")
        d2 = Decision(id="dec-2", question="What cache strategy?", decision="Use Redis")
        result = deduplicate_decisions([d1, d2])
        assert len(result) == 2

    def test_llm_dedup_filters_semantic_duplicates(self):
        """use_llm=True calls _llm_dedup and filters by returned indices."""
        d1 = Decision(id="dec-1", question="Use sync?", decision="Yes, use sync")
        d2 = Decision(id="dec-2", question="Go synchronous?", decision="Synchronous is best")
        d3 = Decision(id="dec-3", question="Cache strategy?", decision="Use Redis")

        # Mock _llm_dedup to keep only first and third candidates
        def fake_llm_dedup(candidates, existing):
            return [candidates[0], candidates[2]]

        with patch("plumb.decision_log._llm_dedup", side_effect=fake_llm_dedup):
            result = deduplicate_decisions([d1, d2, d3], existing_decisions=[], use_llm=True)

        assert len(result) == 2
        assert result[0].id == "dec-1"
        assert result[1].id == "dec-3"

    def test_llm_dedup_called_even_for_single_candidate(self):
        """use_llm=True should still call LLM with 1 candidate to catch cross-ref semantic dups."""
        d1 = Decision(id="dec-1", question="Use sync?", decision="Yes")

        def fake_llm_dedup(candidates, existing):
            return candidates  # keep it

        with patch("plumb.decision_log._llm_dedup", side_effect=fake_llm_dedup) as mock_fn:
            result = deduplicate_decisions([d1], existing_decisions=[], use_llm=True)
        mock_fn.assert_called_once()
        assert len(result) == 1


class TestNormalizeText:
    def test_removes_stop_words(self):
        result = _normalize_text("the system should use a cache")
        assert "the" not in result
        assert "should" not in result
        assert "cache" in result
        assert "system" in result

    def test_empty_string(self):
        result = _normalize_text("")
        assert result == set()


class TestTextSimilarity:
    def test_identical(self):
        assert _text_similarity("use sync for simplicity", "use sync for simplicity") == 1.0

    def test_completely_different(self):
        sim = _text_similarity("redis cache strategy", "database migration plan")
        assert sim < 0.3

    def test_both_empty(self):
        assert _text_similarity("", "") == 1.0

    def test_one_empty(self):
        assert _text_similarity("hello", "") == 0.0
