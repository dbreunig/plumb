import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from plumb.decision_log import (
    Decision,
    FileRef,
    generate_decision_id,
    read_decisions,
    read_all_decisions,
    append_decision,
    append_decisions,
    update_decision_status,
    filter_decisions,
    find_decision_branch,
    delete_decisions_by_commit,
    deduplicate_decisions,
    migrate_decisions,
    merge_branch_decisions,
    _sanitize_branch_name,
    _decisions_dir,
    _branch_decisions_path,
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
        append_decisions(initialized_repo, sample_decisions, branch="main")
        update_decision_status(initialized_repo, "dec-aaa111", branch="main", status="approved")
        pending = filter_decisions(initialized_repo, status="pending")
        assert len(pending) == 1
        assert pending[0].id == "dec-bbb222"

    def test_filter_by_branch(self, initialized_repo, sample_decisions):
        d1 = sample_decisions[0].model_copy(update={"branch": "feat"})
        d2 = sample_decisions[1].model_copy(update={"branch": "main"})
        append_decision(initialized_repo, d1, branch="feat")
        append_decision(initialized_repo, d2, branch="main")
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

    def test_no_existing_decisions(self):
        new = [Decision(id="dec-1", question="Q?", decision="A.")]
        result = deduplicate_decisions(new, existing_decisions=None)
        assert len(result) == 1

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


class TestBranchScopedRead:
    def test_read_empty_branch(self, initialized_repo):
        result = read_decisions(initialized_repo, branch="feature-x")
        assert result == []

    def test_read_specific_branch(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions, branch="feature-x")
        result = read_decisions(initialized_repo, branch="feature-x")
        assert len(result) == 2

    def test_read_branch_isolation(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, [sample_decisions[0]], branch="branch-a")
        append_decisions(initialized_repo, [sample_decisions[1]], branch="branch-b")
        a = read_decisions(initialized_repo, branch="branch-a")
        b = read_decisions(initialized_repo, branch="branch-b")
        assert len(a) == 1
        assert a[0].id == "dec-aaa111"
        assert len(b) == 1
        assert b[0].id == "dec-bbb222"

    def test_latest_line_wins_within_branch(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="main")
        updated = sample_decisions[0].model_copy(update={"status": "approved"})
        append_decision(initialized_repo, updated, branch="main")
        result = read_decisions(initialized_repo, branch="main")
        assert len(result) == 1
        assert result[0].status == "approved"


class TestBranchScopedWrite:
    def test_append_creates_decisions_dir(self, initialized_repo):
        d = Decision(id="dec-1", question="Q?", decision="A.")
        append_decision(initialized_repo, d, branch="new-branch")
        assert (initialized_repo / ".plumb" / "decisions" / "new-branch.jsonl").exists()

    def test_append_multiple_to_branch(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions, branch="feat")
        result = read_decisions(initialized_repo, branch="feat")
        assert len(result) == 2


class TestBranchScopedUpdate:
    def test_update_in_branch(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="feat")
        result = update_decision_status(
            initialized_repo, "dec-aaa111", branch="feat", status="approved"
        )
        assert result is not None
        assert result.status == "approved"
        decisions = read_decisions(initialized_repo, branch="feat")
        assert decisions[0].status == "approved"

    def test_update_not_found_in_branch(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="feat")
        result = update_decision_status(
            initialized_repo, "dec-aaa111", branch="other", status="approved"
        )
        assert result is None


class TestBranchScopedDelete:
    def test_delete_by_commit_in_branch(self, initialized_repo):
        d1 = Decision(id="dec-1", commit_sha="sha111")
        d2 = Decision(id="dec-2", commit_sha="sha222")
        append_decisions(initialized_repo, [d1, d2], branch="feat")
        removed = delete_decisions_by_commit(initialized_repo, "sha111", branch="feat")
        assert removed == 1
        remaining = read_decisions(initialized_repo, branch="feat")
        assert len(remaining) == 1
        assert remaining[0].id == "dec-2"


class TestPathHelpers:
    def test_sanitize_simple_branch(self):
        assert _sanitize_branch_name("main") == "main"

    def test_sanitize_slashes(self):
        assert _sanitize_branch_name("feature/foo") == "feature-foo"

    def test_sanitize_multiple_slashes(self):
        assert _sanitize_branch_name("feature/bar/baz") == "feature-bar-baz"

    def test_sanitize_special_chars(self):
        assert _sanitize_branch_name("fix/bug#123") == "fix-bug-123"

    def test_sanitize_head(self):
        assert _sanitize_branch_name("HEAD") == "HEAD"

    def test_decisions_dir(self, initialized_repo):
        d = _decisions_dir(initialized_repo)
        assert d == initialized_repo / ".plumb" / "decisions"

    def test_branch_decisions_path(self, initialized_repo):
        p = _branch_decisions_path(initialized_repo, "feature/foo")
        assert p == initialized_repo / ".plumb" / "decisions" / "feature-foo.jsonl"

    def test_branch_decisions_path_main(self, initialized_repo):
        p = _branch_decisions_path(initialized_repo, "main")
        assert p == initialized_repo / ".plumb" / "decisions" / "main.jsonl"


class TestReadAllDecisions:
    def test_empty_directory(self, initialized_repo):
        (initialized_repo / ".plumb" / "decisions").mkdir(parents=True, exist_ok=True)
        result = read_all_decisions(initialized_repo)
        assert result == []

    def test_reads_across_branches(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="branch-a")
        append_decision(initialized_repo, sample_decisions[1], branch="branch-b")
        result = read_all_decisions(initialized_repo)
        assert len(result) == 2
        ids = {d.id for d in result}
        assert ids == {"dec-aaa111", "dec-bbb222"}

    def test_dedup_across_branches(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="branch-a")
        updated = sample_decisions[0].model_copy(update={"status": "approved"})
        append_decision(initialized_repo, updated, branch="branch-a")
        result = read_all_decisions(initialized_repo)
        assert len(result) == 1
        assert result[0].status == "approved"

    def test_reads_single_branch(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions, branch="main")
        result = read_all_decisions(initialized_repo)
        assert len(result) == 2


class TestFilterDecisionsCrossShard:
    def test_filter_across_branches_by_status(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="branch-a")
        d2_approved = sample_decisions[1].model_copy(update={"status": "approved"})
        append_decision(initialized_repo, d2_approved, branch="branch-b")
        pending = filter_decisions(initialized_repo, status="pending")
        assert len(pending) == 1
        assert pending[0].id == "dec-aaa111"

    def test_filter_single_branch(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions, branch="feat")
        result = filter_decisions(initialized_repo, status="pending", branch="feat")
        assert len(result) == 2


class TestFindDecisionBranch:
    def test_find_in_branch(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="feat")
        result = find_decision_branch(initialized_repo, "dec-aaa111")
        assert result == "feat"

    def test_not_found(self, initialized_repo):
        (initialized_repo / ".plumb" / "decisions").mkdir(parents=True, exist_ok=True)
        result = find_decision_branch(initialized_repo, "dec-nonexist")
        assert result is None

    def test_find_across_multiple_branches(self, initialized_repo, sample_decisions):
        append_decision(initialized_repo, sample_decisions[0], branch="branch-a")
        append_decision(initialized_repo, sample_decisions[1], branch="branch-b")
        assert find_decision_branch(initialized_repo, "dec-aaa111") == "branch-a"
        assert find_decision_branch(initialized_repo, "dec-bbb222") == "branch-b"


class TestMigrateDecisions:
    def test_migrate_creates_sharded_dir(self, initialized_repo, sample_decisions):
        # Write to legacy monolithic file
        append_decisions(initialized_repo, sample_decisions)
        result = migrate_decisions(initialized_repo)
        assert result["migrated"] == 2
        assert (initialized_repo / ".plumb" / "decisions" / "main.jsonl").exists()
        assert not (initialized_repo / ".plumb" / "decisions.jsonl").exists()

    def test_migrate_deduplicates(self, initialized_repo, sample_decisions):
        # Write same decision twice (simulating update append)
        append_decision(initialized_repo, sample_decisions[0])
        updated = sample_decisions[0].model_copy(update={"status": "approved"})
        append_decision(initialized_repo, updated)
        append_decision(initialized_repo, sample_decisions[1])
        result = migrate_decisions(initialized_repo)
        assert result["migrated"] == 2
        decisions = read_decisions(initialized_repo, branch="main")
        approved = [d for d in decisions if d.id == "dec-aaa111"]
        assert approved[0].status == "approved"

    def test_migrate_idempotent(self, initialized_repo):
        (initialized_repo / ".plumb" / "decisions").mkdir(parents=True, exist_ok=True)
        result = migrate_decisions(initialized_repo)
        assert result["migrated"] == 0
        assert result["already_migrated"] is True

    def test_migrate_empty_legacy(self, initialized_repo):
        (initialized_repo / ".plumb" / "decisions.jsonl").write_text("")
        result = migrate_decisions(initialized_repo)
        assert result["migrated"] == 0


class TestMergeBranchDecisions:
    def test_merge_to_main(self, initialized_repo, sample_decisions):
        append_decisions(initialized_repo, sample_decisions, branch="feat")
        result = merge_branch_decisions(initialized_repo, "feat")
        assert result["merged"] == 2
        assert not (initialized_repo / ".plumb" / "decisions" / "feat.jsonl").exists()
        main_decisions = read_decisions(initialized_repo, branch="main")
        assert len(main_decisions) == 2

    def test_merge_appends_to_existing_main(self, initialized_repo, sample_decisions):
        d_main = Decision(id="dec-main1", question="Q?", decision="A.")
        append_decision(initialized_repo, d_main, branch="main")
        append_decisions(initialized_repo, sample_decisions, branch="feat")
        merge_branch_decisions(initialized_repo, "feat")
        main_decisions = read_decisions(initialized_repo, branch="main")
        assert len(main_decisions) == 3

    def test_merge_nonexistent_branch(self, initialized_repo):
        (initialized_repo / ".plumb" / "decisions").mkdir(parents=True, exist_ok=True)
        result = merge_branch_decisions(initialized_repo, "nonexistent")
        assert result["merged"] == 0

    def test_cannot_merge_main(self, initialized_repo):
        result = merge_branch_decisions(initialized_repo, "main")
        assert result["error"] == "cannot merge main into itself"
