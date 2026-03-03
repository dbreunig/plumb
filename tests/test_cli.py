import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner
from git import Repo

from plumb.cli import cli, _update_claude_md, _find_spec_suggestions, _find_test_suggestions, _prompt_with_suggestions
from plumb.config import PlumbConfig, save_config, ensure_plumb_dir, load_config
from plumb.decision_log import Decision, append_decision, read_decisions, read_all_decisions


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
        append_decision(initialized_repo, d, branch="main")

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["approve", "dec-test1"])
            assert result.exit_code == 0
            assert "Approved" in result.output

        decisions = read_decisions(initialized_repo, branch="main")
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
        append_decision(initialized_repo, d1, branch="main")
        append_decision(initialized_repo, d2, branch="main")
        append_decision(initialized_repo, d3, branch="main")

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["approve", "--all"])
            assert result.exit_code == 0
            assert "Approved 2 decision(s)" in result.output

        decisions = read_decisions(initialized_repo, branch="main")
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
        append_decision(initialized_repo, d, branch="main")

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo), \
             patch("plumb.cli._run_modify") as mock_modify:
            result = runner.invoke(cli, ["reject", "dec-test2", "--reason", "bad idea"])
            assert result.exit_code == 0
            assert "Rejected" in result.output
            mock_modify.assert_called_once_with(initialized_repo, "dec-test2")

        decisions = read_decisions(initialized_repo, branch="main")
        rejected = [d for d in decisions if d.id == "dec-test2" and d.status == "rejected"]
        assert len(rejected) == 1
        assert rejected[0].rejection_reason == "bad idea"


class TestIgnore:
    def test_ignore_existing(self, runner, initialized_repo):
        d = Decision(id="dec-ign1", status="pending", decision="X")
        append_decision(initialized_repo, d, branch="main")

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["ignore", "dec-ign1"])
            assert result.exit_code == 0
            assert "Ignored" in result.output

        decisions = read_decisions(initialized_repo, branch="main")
        ignored = [d for d in decisions if d.id == "dec-ign1" and d.status == "ignored"]
        assert len(ignored) == 1

    def test_ignore_nonexistent(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["ignore", "dec-nope"])
            assert result.exit_code != 0


class TestEdit:
    def test_edit_existing(self, runner, initialized_repo):
        # plumb:req-127001f3
        # plumb:req-b6f2c3c1
        # plumb:req-5d3f1baf
        d = Decision(id="dec-test3", status="pending", decision="C")
        append_decision(initialized_repo, d, branch="main")

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["edit", "dec-test3", "new text"])
            assert result.exit_code == 0
            assert "Edited" in result.output

        decisions = read_decisions(initialized_repo, branch="main")
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
    def test_sync_no_decisions(self, runner, initialized_repo):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["sync"])
            assert result.exit_code == 0
            assert "No unsynced decisions" in result.output

    def test_sync_with_decisions(self, runner, initialized_repo):
        d = Decision(id="dec-sync1", status="approved", decision="A")
        append_decision(initialized_repo, d, branch="main")
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


class TestInitPytestDetection:
    def test_warns_when_pytest_missing(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]), \
             patch("plumb.cli.importlib.util") as mock_importlib:
            mock_importlib.find_spec.return_value = None
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0
            assert "pytest was not detected" in result.output
            assert "pip install pytest" in result.output

    def test_no_warning_when_pytest_installed(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            # Don't mock find_spec — pytest IS installed in test env
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0
            assert "pytest was not detected" not in result.output

    def test_collect_only_succeeds_with_valid_tests(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        tests_dir = tmp_repo / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_foo.py").write_text("def test_foo(): pass\n")
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]), \
             patch("plumb.cli.subprocess.run", return_value=MagicMock(returncode=0)):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0

    def test_collect_only_fails_aborts_init(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        tests_dir = tmp_repo / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_bad.py").write_text("def test_bad(): pass\n")
        mock_result = MagicMock(returncode=1, stdout="ERRORS!\n", stderr="ImportError\n")
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]), \
             patch("plumb.cli.subprocess.run", return_value=mock_result):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code != 0
            assert "pytest failed to collect tests" in result.output
            assert not (tmp_repo / ".plumb" / "config.json").exists()

    def test_collect_only_skipped_for_empty_test_dir(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]), \
             patch("plumb.cli.subprocess.run") as mock_run:
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0
            mock_run.assert_not_called()

    def test_collect_only_skipped_when_pytest_missing(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        tests_dir = tmp_repo / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_foo.py").write_text("def test_foo(): pass\n")
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]), \
             patch("plumb.cli.importlib.util") as mock_importlib, \
             patch("plumb.cli.subprocess.run") as mock_run:
            mock_importlib.find_spec.return_value = None
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0
            mock_run.assert_not_called()

    def test_collect_only_timeout_is_warning(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        tests_dir = tmp_repo / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_foo.py").write_text("def test_foo(): pass\n")
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]), \
             patch("plumb.cli.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=30)):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0
            assert "timed out" in result.output


class TestInitValidation:
    def test_non_md_file_rejected(self, runner, tmp_repo):
        """Single file that's not .md should hard-fail."""
        (tmp_repo / "spec.txt").write_text("not markdown\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.cli._find_spec_suggestions", return_value=[]), \
             patch("plumb.cli._find_test_suggestions", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.txt\ntests/\n")
            assert result.exit_code != 0
            assert "not a markdown file" in result.output.lower()

    def test_shows_spec_suggestions(self, runner, tmp_repo):
        """Init should display found .md files."""
        (tmp_repo / "my_spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="1\ntests/\n")
            assert result.exit_code == 0
            assert "my_spec.md" in result.output

    def test_shows_test_suggestions(self, runner, tmp_repo):
        """Init should display found test directories."""
        (tmp_repo / "spec.md").write_text("# Spec\n")
        tests_dir = tmp_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text("def test_foo(): pass\n")
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.md\n1\n")
            assert result.exit_code == 0
            assert "tests/" in result.output


class TestFindSpecSuggestions:
    def test_finds_md_files(self, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "design.md").write_text("# Design\n")
        suggestions = _find_spec_suggestions(tmp_repo)
        assert "spec.md" in suggestions
        assert "design.md" in suggestions

    def test_finds_dirs_with_md_files(self, tmp_repo):
        specs_dir = tmp_repo / "specs"
        specs_dir.mkdir()
        (specs_dir / "a.md").write_text("# A\n")
        (specs_dir / "b.md").write_text("# B\n")
        suggestions = _find_spec_suggestions(tmp_repo)
        assert any("specs/" in s for s in suggestions)

    def test_excludes_plumbignored_files(self, tmp_repo):
        (tmp_repo / "README.md").write_text("# Readme\n")
        (tmp_repo / "spec.md").write_text("# Spec\n")
        suggestions = _find_spec_suggestions(tmp_repo)
        assert not any("README.md" in s for s in suggestions)
        assert any("spec.md" in s for s in suggestions)

    def test_empty_repo_no_suggestions(self, tmp_repo):
        suggestions = _find_spec_suggestions(tmp_repo)
        assert suggestions == []


class TestFindTestSuggestions:
    def test_finds_tests_dir(self, tmp_repo):
        tests_dir = tmp_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text("def test_foo(): pass\n")
        (tests_dir / "test_bar.py").write_text("def test_bar(): pass\n")
        suggestions = _find_test_suggestions(tmp_repo)
        assert any("tests/" in s for s in suggestions)

    def test_finds_test_dir(self, tmp_repo):
        test_dir = tmp_repo / "test"
        test_dir.mkdir()
        (test_dir / "test_a.py").write_text("def test_a(): pass\n")
        suggestions = _find_test_suggestions(tmp_repo)
        assert any("test/" in s for s in suggestions)

    def test_no_test_dirs(self, tmp_repo):
        suggestions = _find_test_suggestions(tmp_repo)
        assert suggestions == []


class TestPromptWithSuggestions:
    def test_pick_by_number(self):
        suggestions = ["spec.md", "docs/  (3 .md files)"]
        with patch("click.prompt", return_value="1"):
            result = _prompt_with_suggestions("Pick a spec", suggestions, default_no_suggestions=".")
            assert result == "spec.md"

    def test_pick_second_option(self):
        suggestions = ["spec.md", "docs/  (3 .md files)"]
        with patch("click.prompt", return_value="2"):
            result = _prompt_with_suggestions("Pick a spec", suggestions, default_no_suggestions=".")
            # For dirs, strip the count suffix
            assert result == "docs/"

    def test_custom_path(self):
        suggestions = ["spec.md"]
        with patch("click.prompt", return_value="my_spec.md"):
            result = _prompt_with_suggestions("Pick a spec", suggestions, default_no_suggestions=".")
            assert result == "my_spec.md"

    def test_no_suggestions_uses_default(self):
        with patch("click.prompt", return_value="."):
            result = _prompt_with_suggestions("Pick a spec", [], default_no_suggestions=".")
            assert result == "."

    def test_default_is_first_suggestion(self):
        suggestions = ["spec.md"]
        with patch("click.prompt", return_value="1") as mock_prompt:
            _prompt_with_suggestions("Pick a spec", suggestions, default_no_suggestions=".")
            mock_prompt.assert_called_once()
            assert mock_prompt.call_args[1].get("default") == "1"
