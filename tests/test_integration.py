"""Integration tests for the full Plumb workflow."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner
from git import Repo

from plumb.cli import cli, _update_claude_md
from plumb.config import PlumbConfig, save_config, load_config, ensure_plumb_dir
from plumb.decision_log import (
    Decision,
    append_decision,
    append_decisions,
    read_decisions,
    update_decision_status,
    filter_decisions,
)
from plumb.git_hook import run_hook


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def full_repo(tmp_path):
    """A fully initialized repo with spec, tests, and plumb config."""
    repo = Repo.init(tmp_path)
    # Create initial files
    (tmp_path / "spec.md").write_text("# Spec\n\n## Auth\n\nUsers must log in.\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_auth.py").write_text(
        "def test_login():\n    pass\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login(): pass\n")
    repo.index.add(["spec.md", "tests/test_auth.py", "src/auth.py"])
    repo.index.commit("Initial commit")

    # Initialize plumb
    ensure_plumb_dir(tmp_path)
    cfg = PlumbConfig(
        spec_paths=["spec.md"],
        test_paths=["tests/"],
        initialized_at=datetime.now(timezone.utc).isoformat(),
        last_commit=str(repo.head.commit),
    )
    save_config(tmp_path, cfg)

    return tmp_path


class TestFullHookFlow:
    """Test: stage -> hook -> pending decisions -> approve -> hook -> commit succeeds"""

    def test_stage_hook_approve_commit(self, full_repo):
        repo = Repo(full_repo)

        # Stage a new change
        auth = full_repo / "src" / "auth.py"
        auth.write_text("def login():\n    return True  # auto-approve\n")
        repo.index.add(["src/auth.py"])

        mock_decisions = [
            Decision(
                id="dec-int001",
                status="pending",
                question="Should login always return True?",
                decision="Login returns True for now.",
                made_by="llm",
                confidence=0.8,
                branch="master",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        ]

        # First hook run — should block (pending decisions)
        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="feature: auth change"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=mock_decisions), \
             patch("plumb.git_hook._synthesize_questions", return_value=mock_decisions):
            result = run_hook(full_repo)
            assert result == 1

        # Verify decisions were written
        decisions = read_decisions(full_repo)
        pending = [d for d in decisions if d.status == "pending"]
        assert len(pending) >= 1

        # Approve the decision
        for d in pending:
            update_decision_status(full_repo, d.id, status="approved",
                                   reviewed_at=datetime.now(timezone.utc).isoformat())

        # Second hook run — should allow (no pending decisions)
        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="feature: auth change"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=[]), \
             patch("plumb.git_hook._extract_decisions_from_diff", return_value=[]), \
             patch("plumb.coverage_reporter.print_coverage_report"):
            result = run_hook(full_repo)
            assert result == 0

    def test_reject_flow(self, full_repo):
        """Test rejection creates correct status."""
        d = Decision(
            id="dec-rej001",
            status="pending",
            question="Use sync or async?",
            decision="Use async",
            made_by="llm",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        append_decision(full_repo, d)

        # Reject
        update_decision_status(
            full_repo, "dec-rej001",
            status="rejected",
            rejection_reason="Too complex for v1",
        )

        decisions = read_decisions(full_repo)
        rejected = [d for d in decisions if d.id == "dec-rej001"]
        assert rejected[0].status == "rejected"
        assert rejected[0].rejection_reason == "Too complex for v1"


class TestAmendFlow:
    def test_amend_deletes_old_decisions(self, full_repo):
        # plumb:req-c5b53da1
        """When amending, old decisions for that commit should be removed."""
        repo = Repo(full_repo)
        initial_sha = str(repo.head.commit)

        # Add a decision tied to the initial commit
        d = Decision(
            id="dec-amend1",
            status="approved",
            commit_sha=initial_sha,
            decision="Old decision",
        )
        append_decision(full_repo, d)

        # Make a new commit
        f = full_repo / "src" / "new.py"
        f.write_text("x = 1\n")
        repo.index.add(["src/new.py"])
        repo.index.commit("second commit")

        # Now config's last_commit = initial_sha, and HEAD parent = initial_sha
        # This simulates an amend
        config = load_config(full_repo)
        config.last_commit = initial_sha
        save_config(full_repo, config)

        # Stage more changes for the "amend"
        f.write_text("x = 2\n")
        repo.index.add(["src/new.py"])

        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="change"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=[]), \
             patch("plumb.git_hook._extract_decisions_from_diff", return_value=[]), \
             patch("plumb.coverage_reporter.print_coverage_report"):
            run_hook(full_repo)

        # Old decision should be removed
        decisions = read_decisions(full_repo)
        old = [d for d in decisions if d.id == "dec-amend1"]
        assert len(old) == 0


class TestClaudeMdIdempotency:
    def test_multiple_updates(self, full_repo):
        cfg = PlumbConfig(spec_paths=["spec.md"], test_paths=["tests/"])
        _update_claude_md(full_repo, cfg)
        _update_claude_md(full_repo, cfg)
        _update_claude_md(full_repo, cfg)

        content = (full_repo / "CLAUDE.md").read_text()
        assert content.count("<!-- plumb:start -->") == 1
        assert content.count("<!-- plumb:end -->") == 1

    def test_update_changes_paths(self, full_repo):
        cfg1 = PlumbConfig(spec_paths=["old.md"], test_paths=["tests/"])
        _update_claude_md(full_repo, cfg1)

        cfg2 = PlumbConfig(spec_paths=["new.md"], test_paths=["tests/"])
        _update_claude_md(full_repo, cfg2)

        content = (full_repo / "CLAUDE.md").read_text()
        assert "new.md" in content
        assert "old.md" not in content


class TestCLIEndToEnd:
    def test_init_then_status(self, runner, tmp_path):
        """init + status should work without errors."""
        repo = Repo.init(tmp_path)
        (tmp_path / "spec.md").write_text("# Spec\n")
        (tmp_path / "tests").mkdir()
        repo.index.add(["spec.md"])
        repo.index.commit("init")

        with patch("plumb.cli.find_repo_root", return_value=tmp_path), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0

        with patch("plumb.cli.find_repo_root", return_value=tmp_path):
            result = runner.invoke(cli, ["status"])
            assert result.exit_code == 0

    def test_approve_then_status(self, runner, full_repo):
        """Approve a decision then check status shows no pending."""
        d = Decision(id="dec-e2e1", status="pending", decision="Test")
        append_decision(full_repo, d)

        with patch("plumb.cli.find_repo_root", return_value=full_repo):
            result = runner.invoke(cli, ["approve", "dec-e2e1"])
            assert result.exit_code == 0

        with patch("plumb.cli.find_repo_root", return_value=full_repo):
            result = runner.invoke(cli, ["status"])
            assert result.exit_code == 0
            assert "0" in result.output  # 0 pending decisions


class TestHookNeverBlocks:
    """The hook must never exit non-zero due to internal errors."""

    def test_crash_in_diff_analysis(self, full_repo):
        repo = Repo(full_repo)
        f = full_repo / "src" / "crash.py"
        f.write_text("crash = True\n")
        repo.index.add(["src/crash.py"])

        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", side_effect=RuntimeError("LLM down")):
            result = run_hook(full_repo)
            assert result == 0  # Must not block

    def test_crash_in_decision_extraction(self, full_repo):
        repo = Repo(full_repo)
        f = full_repo / "src" / "crash2.py"
        f.write_text("crash2 = True\n")
        repo.index.add(["src/crash2.py"])

        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="ok"), \
             patch("plumb.git_hook._extract_decisions_from_conversation",
                   side_effect=RuntimeError("boom")):
            result = run_hook(full_repo)
            assert result == 0
