import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner
from git import Repo

from plumb.cli import cli, _update_claude_md
from plumb.config import PlumbConfig, save_config, ensure_plumb_dir, load_config
from plumb.decision_log import Decision, append_decision, read_decisions


@pytest.fixture
def runner():
    return CliRunner()


class TestInit:
    def test_not_git_repo(self, runner, tmp_path):
        # plumb:req-fedab03e
        # plumb:req-dc5b8f48
        with patch("plumb.cli.find_repo_root", return_value=None):
            result = runner.invoke(cli, ["init"])
            assert result.exit_code != 0

    def test_successful_init(self, runner, tmp_repo):
        # plumb:req-1a094799
        # plumb:req-26d23d84
        spec = tmp_repo / "spec.md"
        spec.write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)

        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0
            assert "initialized" in result.output.lower()

        # Verify artifacts
        assert (tmp_repo / ".plumb" / "config.json").exists()
        assert (tmp_repo / ".git" / "hooks" / "pre-commit").exists()
        assert (tmp_repo / ".claude" / "skills" / "plumb" / "SKILL.md").exists()
        assert (tmp_repo / "CLAUDE.md").exists()

        # Verify hook is executable
        hook = tmp_repo / ".git" / "hooks" / "pre-commit"
        assert os.access(str(hook), os.X_OK)


class TestInitPlumbignore:
    def test_init_creates_plumbignore(self, runner, tmp_repo):
        spec = tmp_repo / "spec.md"
        spec.write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)

        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0

        plumbignore = tmp_repo / ".plumbignore"
        assert plumbignore.exists()
        content = plumbignore.read_text()
        assert "README.md" in content
        assert "docs/" in content
        assert ".plumbignore" in result.output

    def test_reinit_preserves_existing_plumbignore(self, runner, tmp_repo):
        spec = tmp_repo / "spec.md"
        spec.write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        custom = "my-custom-pattern\n"
        (tmp_repo / ".plumbignore").write_text(custom)

        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0

        assert (tmp_repo / ".plumbignore").read_text() == custom


class TestClaudeMdIntegration:
    def test_creates_claude_md(self, tmp_repo):
        cfg = PlumbConfig(spec_paths=["spec.md"], test_paths=["tests/"])
        _update_claude_md(tmp_repo, cfg)
        claude_md = tmp_repo / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert "<!-- plumb:start -->" in content
        assert "<!-- plumb:end -->" in content
        assert "spec.md" in content

    def test_idempotent_update(self, tmp_repo):
        cfg = PlumbConfig(spec_paths=["spec.md"], test_paths=["tests/"])
        _update_claude_md(tmp_repo, cfg)
        _update_claude_md(tmp_repo, cfg)
        content = (tmp_repo / "CLAUDE.md").read_text()
        assert content.count("<!-- plumb:start -->") == 1

    def test_preserves_existing_content(self, tmp_repo):
        claude_md = tmp_repo / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nExisting content.\n")
        cfg = PlumbConfig(spec_paths=["spec.md"], test_paths=["tests/"])
        _update_claude_md(tmp_repo, cfg)
        content = claude_md.read_text()
        assert "Existing content" in content
        assert "<!-- plumb:start -->" in content


class TestHook:
    def test_hook_command(self, runner, initialized_repo):
        # plumb:req-87dd4040
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo), \
             patch("plumb.git_hook.run_hook", return_value=0):
            result = runner.invoke(cli, ["hook"])
            assert result.exit_code == 0

    def test_hook_dry_run(self, runner, initialized_repo):
        # plumb:req-b0b19348
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo), \
             patch("plumb.git_hook.run_hook", return_value=0) as mock_hook:
            result = runner.invoke(cli, ["hook", "--dry-run"])
            assert result.exit_code == 0
            mock_hook.assert_called_once_with(initialized_repo, dry_run=True)


class TestApprove:
    def test_approve_existing(self, runner, initialized_repo):
        # plumb:req-42c8fd3f
        # plumb:req-3a769972
        d = Decision(id="dec-test1", status="pending", decision="A")
        append_decision(initialized_repo, d)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["approve", "dec-test1"])
            assert result.exit_code == 0
            assert "Approved" in result.output

        decisions = read_decisions(initialized_repo)
        approved = [d for d in decisions if d.id == "dec-test1" and d.status == "approved"]
        assert len(approved) == 1

    def test_approve_nonexistent(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["approve", "dec-nope"])
            assert result.exit_code != 0

    def test_approve_all(self, runner, initialized_repo):
        d1 = Decision(id="dec-all1", status="pending", decision="A")
        d2 = Decision(id="dec-all2", status="pending", decision="B")
        d3 = Decision(id="dec-done", status="approved", decision="C")
        append_decision(initialized_repo, d1)
        append_decision(initialized_repo, d2)
        append_decision(initialized_repo, d3)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["approve", "--all"])
            assert result.exit_code == 0
            assert "Approved 2 decision(s)" in result.output

        decisions = read_decisions(initialized_repo)
        approved = [d for d in decisions if d.status == "approved"]
        assert len(approved) == 3  # 2 newly approved + 1 already approved

    def test_approve_all_no_pending(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["approve", "--all"])
            assert result.exit_code == 0
            assert "No pending decisions" in result.output

    def test_approve_all_with_id_errors(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["approve", "dec-123", "--all"])
            assert result.exit_code != 0
            assert "Cannot use --all with a specific decision ID" in result.output

    def test_approve_no_id_no_all_errors(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["approve"])
            assert result.exit_code != 0
            assert "Provide a decision ID or use --all" in result.output


class TestReject:
    def test_reject_existing(self, runner, initialized_repo):
        # plumb:req-74db9086
        # plumb:req-4e20343f
        d = Decision(id="dec-test2", status="pending", decision="B")
        append_decision(initialized_repo, d)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["reject", "dec-test2", "--reason", "bad idea"])
            assert result.exit_code == 0
            assert "Rejected" in result.output

        decisions = read_decisions(initialized_repo)
        rejected = [d for d in decisions if d.id == "dec-test2" and d.status == "rejected"]
        assert len(rejected) == 1
        assert rejected[0].rejection_reason == "bad idea"


class TestEdit:
    def test_edit_existing(self, runner, initialized_repo):
        # plumb:req-127001f3
        # plumb:req-b6f2c3c1
        # plumb:req-5d3f1baf
        d = Decision(id="dec-test3", status="pending", decision="C")
        append_decision(initialized_repo, d)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["edit", "dec-test3", "new text"])
            assert result.exit_code == 0
            assert "Edited" in result.output

        decisions = read_decisions(initialized_repo)
        edited = [d for d in decisions if d.id == "dec-test3" and d.status == "edited"]
        assert len(edited) == 1
        assert edited[0].decision == "new text"


class TestStatus:
    def test_not_initialized(self, runner, tmp_repo):
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo):
            result = runner.invoke(cli, ["status"])
            assert "not initialized" in result.output.lower() or "plumb init" in result.output.lower()

    def test_initialized(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["status"])
            assert result.exit_code == 0
            assert "spec" in result.output.lower()


class TestSync:
    def test_sync_command(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo), \
             patch("plumb.sync.sync_decisions", return_value={"spec_updated": 0, "tests_generated": 0}):
            result = runner.invoke(cli, ["sync"])
            assert result.exit_code == 0
            assert "Synced" in result.output


class TestCoverage:
    def test_coverage_command(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo), \
             patch("plumb.coverage_reporter.print_coverage_report"):
            result = runner.invoke(cli, ["coverage"])
            assert result.exit_code == 0


class TestDiff:
    def test_diff_command(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo), \
             patch("plumb.git_hook.run_hook", return_value=0):
            result = runner.invoke(cli, ["diff"])
            assert result.exit_code == 0
