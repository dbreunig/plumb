"""Extended git hook tests for coverage improvement."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from git import Repo

from plumb.config import PlumbConfig, save_config, load_config, ensure_plumb_dir
from plumb.decision_log import Decision, append_decision, read_decisions
from plumb.git_hook import (
    run_hook,
    run_post_commit,
    _detect_amend,
    _format_tty_output,
    _format_json_output,
)


class TestDetectAmendExtended:
    def test_amend_detected(self, tmp_repo):
        repo = Repo(tmp_repo)
        # Initial commit is already there, make second
        f = tmp_repo / "a.py"
        f.write_text("a = 1\n")
        repo.index.add(["a.py"])
        repo.index.commit("second")

        # HEAD parent is the initial commit
        initial_sha = str(list(repo.iter_commits())[-1])
        assert _detect_amend(repo, initial_sha) is True


class TestHookWithConversation:
    def test_with_conversation_log(self, initialized_repo):
        """Test that conversation log is read when available."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "code.py"
        f.write_text("hello = True\n")
        repo.index.add(["code.py"])

        # Create a fake conversation log
        log_path = initialized_repo / "conv.jsonl"
        log_data = [
            json.dumps({"role": "user", "content": "add hello", "timestamp": "2025-01-02T00:00:00Z"}),
            json.dumps({"role": "assistant", "content": "done", "timestamp": "2025-01-02T00:01:00Z"}),
        ]
        log_path.write_text("\n".join(log_data))

        config = load_config(initialized_repo)
        config.claude_log_path = str(log_path)
        config.last_commit = None
        save_config(initialized_repo, config)

        mock_decisions = [
            Decision(
                id="dec-conv1",
                status="pending",
                question="Add hello?",
                decision="Yes.",
                made_by="user",
                confidence=0.95,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        ]

        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="feature: hello"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=mock_decisions), \
             patch("plumb.git_hook._synthesize_questions", return_value=mock_decisions):
            result = run_hook(initialized_repo)
            assert result == 1  # Should block due to pending

    def test_json_output_structure(self):
        """Verify JSON output has correct structure."""
        decisions = [
            Decision(
                id="dec-j1",
                question="Q1?",
                decision="A1.",
                made_by="llm",
                confidence=0.9,
            ),
            Decision(
                id="dec-j2",
                question="Q2?",
                decision="A2.",
                made_by="user",
                confidence=0.7,
            ),
        ]
        output = _format_json_output(decisions)
        data = json.loads(output)
        assert data["pending_decisions"] == 2
        assert len(data["decisions"]) == 2
        assert data["decisions"][0]["id"] == "dec-j1"
        assert data["decisions"][1]["made_by"] == "user"

    def test_tty_output_multiple_decisions(self):
        decisions = [
            Decision(id="dec-t1", question="Q?", decision="A.", made_by="llm", confidence=0.8),
            Decision(id="dec-t2", decision="B.", made_by="user"),
        ]
        output = _format_tty_output(decisions)
        assert "2 pending" in output
        assert "dec-t1" in output
        assert "dec-t2" in output
        assert "plumb review" in output


class TestHookEdgeCases:
    def test_repo_root_none_default(self):
        """If find_repo_root returns None, hook returns 0."""
        with patch("plumb.git_hook.find_repo_root", return_value=None):
            assert run_hook() == 0

    def test_empty_diff_returns_0(self, initialized_repo):
        """No staged changes = nothing to do."""
        result = run_hook(initialized_repo)
        assert result == 0

    def test_hook_does_not_update_config(self, initialized_repo):
        """Pre-commit hook should not update last_commit (post-commit does that)."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "x.py"
        f.write_text("x=1\n")
        repo.index.add(["x.py"])

        config_before = load_config(initialized_repo)
        old_last_commit = config_before.last_commit

        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="ok"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=[]), \
             patch("plumb.git_hook._extract_decisions_from_diff", return_value=[]), \
             patch("plumb.coverage_reporter.print_coverage_report"):
            result = run_hook(initialized_repo)
            assert result == 0

        config_after = load_config(initialized_repo)
        assert config_after.last_commit == old_last_commit


class TestPostCommitHook:
    def test_updates_config_to_head(self, initialized_repo):
        """Post-commit should set last_commit to current HEAD."""
        repo = Repo(initialized_repo)
        head_sha = str(repo.head.commit)

        run_post_commit(initialized_repo)

        config = load_config(initialized_repo)
        assert config.last_commit == head_sha

    def test_updates_after_new_commit(self, initialized_repo):
        """After a new commit, post-commit should point to the new SHA."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "x.py"
        f.write_text("x=1\n")
        repo.index.add(["x.py"])
        repo.index.commit("new commit")
        new_sha = str(repo.head.commit)

        run_post_commit(initialized_repo)

        config = load_config(initialized_repo)
        assert config.last_commit == new_sha

    def test_clears_last_extracted_at(self, initialized_repo):
        """Post-commit should clear last_extracted_at so next run starts fresh."""
        config = load_config(initialized_repo)
        config.last_extracted_at = "2026-03-01T12:00:00+00:00"
        save_config(initialized_repo, config)

        run_post_commit(initialized_repo)

        config = load_config(initialized_repo)
        assert config.last_extracted_at is None

    def test_no_config_is_noop(self, tmp_repo):
        """If plumb not initialized, post-commit does nothing."""
        run_post_commit(tmp_repo)  # should not raise

    def test_no_repo_is_noop(self):
        """If not in a git repo, post-commit does nothing."""
        with patch("plumb.git_hook.find_repo_root", return_value=None):
            run_post_commit()  # should not raise


class TestLastExtractedAt:
    def test_set_after_writing_decisions(self, initialized_repo):
        """last_extracted_at should be set after decisions are written."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "x.py"
        f.write_text("x=1\n")
        repo.index.add(["x.py"])

        mock_decisions = [
            Decision(id="dec-1", status="pending", question="Q?",
                     decision="A.", made_by="llm", confidence=0.8)
        ]

        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="ok"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=mock_decisions), \
             patch("plumb.git_hook._synthesize_questions", return_value=mock_decisions):
            run_hook(initialized_repo)

        config = load_config(initialized_repo)
        assert config.last_extracted_at is not None

    def test_not_set_when_no_decisions(self, initialized_repo):
        """last_extracted_at should not change when no decisions are found."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "x.py"
        f.write_text("x=1\n")
        repo.index.add(["x.py"])

        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="ok"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=[]), \
             patch("plumb.git_hook._extract_decisions_from_diff", return_value=[]), \
             patch("plumb.coverage_reporter.print_coverage_report"):
            run_hook(initialized_repo)

        config = load_config(initialized_repo)
        assert config.last_extracted_at is None
