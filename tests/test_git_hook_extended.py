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

        with patch("plumb.git_hook._analyze_diff", return_value="feature: hello"), \
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

    def test_hook_updates_config_on_success(self, initialized_repo):
        """When no pending decisions, config should be updated."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "x.py"
        f.write_text("x=1\n")
        repo.index.add(["x.py"])

        with patch("plumb.git_hook._analyze_diff", return_value="ok"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=[]), \
             patch("plumb.git_hook._extract_decisions_from_diff", return_value=[]), \
             patch("plumb.coverage_reporter.print_coverage_report"):
            result = run_hook(initialized_repo)
            assert result == 0

        config = load_config(initialized_repo)
        assert config.last_commit is not None
