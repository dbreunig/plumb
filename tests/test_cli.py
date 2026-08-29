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
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
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
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
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
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
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

    def test_update_claude_md_writes_agents_md_too(self, tmp_repo):
        (tmp_repo / "AGENTS.md").write_text("# Existing\n")
        cfg = PlumbConfig(spec_paths=["spec.md"], test_paths=["tests/"])
        _update_claude_md(tmp_repo, cfg)
        for name in ("CLAUDE.md", "AGENTS.md"):
            text = (tmp_repo / name).read_text()
            assert "<!-- plumb:start -->" in text and "<!-- plumb:end -->" in text
        assert (tmp_repo / "AGENTS.md").read_text().startswith("# Existing\n")
        _update_claude_md(tmp_repo, cfg)   # idempotent
        assert (tmp_repo / "AGENTS.md").read_text().count("plumb:start") == 1
        assert (tmp_repo / "CLAUDE.md").read_text().count("plumb:start") == 1


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
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
            assert result.exit_code == 0
            assert "pytest was not detected" in result.output
            assert "pip install pytest" in result.output

    def test_no_warning_when_pytest_installed(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            # Don't mock find_spec — pytest IS installed in test env
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
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
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
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
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
            assert result.exit_code != 0
            assert "pytest failed to collect tests" in result.output
            assert not (tmp_repo / ".plumb" / "config.json").exists()

    def test_collect_only_skipped_for_empty_test_dir(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]), \
             patch("plumb.cli.subprocess.run") as mock_run:
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
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
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
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
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
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
            result = runner.invoke(cli, ["init"], input="spec.txt\ntests/\n\n")
            assert result.exit_code != 0
            assert "not a markdown file" in result.output.lower()

    def test_shows_spec_suggestions(self, runner, tmp_repo):
        """Init should display found .md files."""
        (tmp_repo / "my_spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="1\ntests/\n\n")
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
            result = runner.invoke(cli, ["init"], input="spec.md\n1\n\n")
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


class TestLog:
    def test_log_groups_and_flags(self, runner, initialized_repo, monkeypatch):
        from plumb.decision_log import append_decisions
        append_decisions(initialized_repo, [
            Decision(id="dec-unc1", status="pending", decision="Use [bold] brackets literally",
                     commit_sha=None, created_at="2026-06-01T00:00:00Z"),
            Decision(id="dec-cmt1", status="approved", decision="Prefer codex", made_by="user",
                     commit_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", agent="codex",
                     session_id="019abcdef", source_path="/nope/session.jsonl", turn_range=[3, 7],
                     evidence_digest="0" * 64, confidence=0.9, created_at="2026-06-02T00:00:00Z"),
        ], branch="main")
        monkeypatch.chdir(initialized_repo)

        result = runner.invoke(cli, ["log"])
        assert result.exit_code == 0, result.output
        assert "uncommitted" in result.output
        assert "codex" in result.output
        assert "session 019abcde" in result.output
        assert "turns 3-7" in result.output
        assert "[bold] brackets" in result.output
        assert "deadbeefdead" in result.output

        result = runner.invoke(cli, ["log", "--verify"])
        assert result.exit_code == 0, result.output
        assert "missing" in result.output            # /nope/session.jsonl does not exist
        assert read_decisions(initialized_repo, branch="main")[1].ref_status == "ok"   # missing never persists

        result = runner.invoke(cli, ["log", "--since", "HEAD"])
        assert result.exit_code == 0, result.output
        assert "uncommitted" in result.output
        assert "uncommitted decisions are always shown" in result.output
        assert "deadbeefdead" not in result.output   # fake sha is not in HEAD..HEAD

    def test_log_verify_unverifiable_without_provenance(self, runner, initialized_repo, monkeypatch):
        from plumb.decision_log import append_decisions
        append_decisions(initialized_repo, [
            Decision(id="dec-noprov", status="approved", decision="x", commit_sha="abc123",
                     created_at="2026-06-02T00:00:00Z"),
        ], branch="main")
        monkeypatch.chdir(initialized_repo)
        result = runner.invoke(cli, ["log", "--verify"])
        assert result.exit_code == 0, result.output
        assert "unverifiable" in result.output

    def test_log_verify_clears_stale(self, runner, initialized_repo, monkeypatch, tmp_path):
        from plumb.conversation import evidence_digest_for, reduce_noise
        from plumb.decision_log import append_decisions
        from plumb.traces import Turn
        transcript = tmp_path / "s.jsonl"
        transcript.write_text("")
        turns = [Turn(agent="claude", session_id="S", ordinal=i, role="user", content=f"t{i}") for i in range(3)]
        append_decisions(initialized_repo, [
            Decision(id="dec-clear1", status="approved", decision="x", commit_sha="abc123", ref_status="stale",
                     agent="claude", session_id="S", source_path=str(transcript), turn_range=[0, 2],
                     evidence_digest=evidence_digest_for(reduce_noise(turns), 0, 2),
                     created_at="2026-06-02T00:00:00Z"),
        ], branch="main")
        monkeypatch.chdir(initialized_repo)

        class FakeSource:
            name = "claude"
            def parse(self, ref, since): return turns
        monkeypatch.setattr("plumb.log_view._source_for", lambda agent: FakeSource())

        result = runner.invoke(cli, ["log", "--verify"])
        assert result.exit_code == 0, result.output
        assert "ok" in result.output
        assert read_decisions(initialized_repo, branch="main")[0].ref_status == "ok"

    def test_log_verify_missing_leaves_ref_status(self, runner, initialized_repo, monkeypatch):
        from plumb.decision_log import append_decisions
        append_decisions(initialized_repo, [
            Decision(id="dec-miss1", status="approved", decision="x", commit_sha="abc123", ref_status="stale",
                     agent="claude", session_id="S", source_path="/nope/x.jsonl", turn_range=[0, 0],
                     evidence_digest="0" * 64, created_at="2026-06-02T00:00:00Z"),
        ], branch="main")
        monkeypatch.chdir(initialized_repo)
        result = runner.invoke(cli, ["log", "--verify"])
        assert result.exit_code == 0, result.output
        assert "missing" in result.output
        rows = read_decisions(initialized_repo, branch="main")
        assert len(rows) == 1 and rows[0].ref_status == "stale"   # nothing appended, nothing changed

    def test_log_verify_marks_stale(self, runner, initialized_repo, monkeypatch):
        from plumb.decision_log import append_decisions
        append_decisions(initialized_repo, [
            Decision(id="dec-stale1", status="approved", decision="x", commit_sha="abc123",
                     agent="claude", session_id="S", source_path="/x.jsonl", turn_range=[0, 0],
                     evidence_digest="0" * 64, created_at="2026-06-02T00:00:00Z"),
        ], branch="main")
        monkeypatch.chdir(initialized_repo)
        monkeypatch.setattr("plumb.log_view.verify_evidence", lambda d: "stale")

        result = runner.invoke(cli, ["log", "--verify"])
        assert result.exit_code == 0, result.output
        assert "stale" in result.output
        assert read_decisions(initialized_repo, branch="main")[0].ref_status == "stale"

    def test_log_groups_under_sha_stamped_by_post_commit(self, runner, initialized_repo, monkeypatch):
        from plumb.decision_log import append_decisions
        from plumb.git_hook import run_post_commit
        repo = Repo(initialized_repo)
        cfg = load_config(initialized_repo)
        cfg.last_commit = str(repo.head.commit)
        save_config(initialized_repo, cfg)
        append_decisions(initialized_repo, [
            Decision(id="dec-stamp1", status="approved", decision="stamped", branch="main",
                     commit_sha=None, created_at=datetime.now(timezone.utc).isoformat()),
        ], branch="main")
        (initialized_repo / "g.txt").write_text("g\n")
        repo.index.add(["g.txt"])
        new_sha = str(repo.index.commit("second"))
        run_post_commit(initialized_repo)
        monkeypatch.chdir(initialized_repo)

        result = runner.invoke(cli, ["log"])
        assert result.exit_code == 0, result.output
        assert new_sha[:12] in result.output
        assert "uncommitted" not in result.output

        result = runner.invoke(cli, ["log", "--since", "HEAD~1"])
        assert new_sha[:12] in result.output
        result = runner.invoke(cli, ["log", "--since", "HEAD"])
        assert new_sha[:12] not in result.output


class TestStatusStale:
    def test_status_counts_stale_evidence(self, runner, initialized_repo, monkeypatch):
        from plumb.decision_log import append_decisions
        append_decisions(initialized_repo, [
            Decision(id="dec-st1", status="approved", decision="x", ref_status="stale"),
            Decision(id="dec-st2", status="approved", decision="y", ref_status="stale"),
            Decision(id="dec-ok1", status="approved", decision="z"),
        ], branch="main")
        monkeypatch.chdir(initialized_repo)
        with patch("plumb.coverage_reporter.check_spec_to_test_coverage", return_value=[]), \
             patch("plumb.coverage_reporter.check_spec_to_code_coverage", return_value=[]):
            result = runner.invoke(cli, ["status"])
        assert "Stale evidence" in result.output and "2" in result.output.split("Stale evidence")[1][:6]


class TestModeCommand:
    def test_mode_command_prints_and_sets(self, initialized_repo, monkeypatch):
        from click.testing import CliRunner
        from plumb.cli import cli
        from plumb.config import load_config
        monkeypatch.chdir(initialized_repo)
        r = CliRunner().invoke(cli, ["mode"])
        assert r.exit_code == 0 and "review" in r.output and "config" in r.output
        r = CliRunner().invoke(cli, ["mode", "record"])
        assert r.exit_code == 0, r.output
        assert load_config(initialized_repo).mode == "record"
        hook = initialized_repo / ".git" / "hooks" / "pre-commit"
        assert hook.exists() and "plumb hook" in hook.read_text()
        assert (initialized_repo / ".git" / "hooks" / "post-commit").exists()
        r = CliRunner().invoke(cli, ["mode", "gate"])
        assert r.exit_code != 0

    def test_mode_command_reports_env_override(self, initialized_repo, monkeypatch):
        from click.testing import CliRunner
        from plumb.cli import cli
        monkeypatch.chdir(initialized_repo)
        monkeypatch.setenv("PLUMB_MODE", "record")
        r = CliRunner().invoke(cli, ["mode"])
        assert "record" in r.output and "env" in r.output
        r = CliRunner().invoke(cli, ["mode", "review"])
        assert r.exit_code == 0 and "PLUMB_MODE=record overrides" in r.output

    def test_mode_command_not_initialized(self, runner, tmp_repo, monkeypatch):
        monkeypatch.chdir(tmp_repo)
        result = runner.invoke(cli, ["mode"])
        assert "not initialized" in result.output.lower()


class TestInitMode:
    def test_init_prompts_for_mode(self, runner, tmp_repo):
        from plumb.config import load_config
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\nrecord\n")
            assert result.exit_code == 0, result.output
            assert "How should Plumb handle decisions" in result.output
            assert "review  — stop each commit until you approve/ignore/reject (default)" in result.output
            assert "record  — record decisions after each commit; review later with plumb log/search" in result.output
            assert "Mode (review, record) [review]" in result.output
        assert load_config(tmp_repo).mode == "record"

    def test_init_defaults_mode_to_review(self, runner, tmp_repo):
        from plumb.config import load_config
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n\n")
            assert result.exit_code == 0, result.output
        assert load_config(tmp_repo).mode == "review"


class TestStatusMode:
    def test_status_shows_effective_mode(self, runner, initialized_repo, monkeypatch):
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["status"])
            assert result.exit_code == 0
            assert "Mode: review (from config)" in result.output
            monkeypatch.setenv("PLUMB_MODE", "record")
            result = runner.invoke(cli, ["status"])
            assert "Mode: record (from env)" in result.output


def test_status_shows_recorded_unsynced(initialized_repo, monkeypatch):
    from plumb.decision_log import append_decisions
    monkeypatch.chdir(initialized_repo)
    append_decisions(initialized_repo, [
        Decision(id="dec-a", status="recorded", decision="a", branch="main"),
        Decision(id="dec-b", status="recorded", decision="b", branch="main", synced_at="2026-01-01T00:00:00Z"),
        Decision(id="dec-c", status="recorded", decision="c", branch="main"),
    ], branch="main")
    r = CliRunner().invoke(cli, ["status"])
    assert r.exit_code == 0, r.output
    assert "2 recorded, unsynced" in r.output


def test_status_omits_recorded_line_when_none(initialized_repo, monkeypatch):
    monkeypatch.chdir(initialized_repo)
    r = CliRunner().invoke(cli, ["status"])
    assert r.exit_code == 0, r.output
    assert "recorded, unsynced" not in r.output


def test_approve_sets_approved_by_user(initialized_repo, monkeypatch):
    from plumb.decision_log import append_decisions
    monkeypatch.chdir(initialized_repo)
    append_decisions(initialized_repo, [Decision(id="dec-p", status="pending", decision="p", branch="main")], branch="main")
    r = CliRunner().invoke(cli, ["approve", "dec-p"])
    assert r.exit_code == 0, r.output
    d = {x.id: x for x in read_all_decisions(initialized_repo)}["dec-p"]
    assert (d.status, d.approved_by) == ("approved", "user")


def test_approve_all_sets_approved_by_user(initialized_repo, monkeypatch):
    from plumb.decision_log import append_decisions
    monkeypatch.chdir(initialized_repo)
    append_decisions(initialized_repo, [
        Decision(id="dec-p1", status="pending", decision="p1", branch="main"),
        Decision(id="dec-p2", status="pending", decision="p2", branch="main"),
    ], branch="main")
    r = CliRunner().invoke(cli, ["approve", "--all"])
    assert r.exit_code == 0, r.output
    by_id = {x.id: x for x in read_all_decisions(initialized_repo)}
    assert all(by_id[i].approved_by == "user" for i in ("dec-p1", "dec-p2"))


def test_edit_sets_approved_by_user(initialized_repo, monkeypatch):
    from plumb.decision_log import append_decisions
    monkeypatch.chdir(initialized_repo)
    append_decisions(initialized_repo, [Decision(id="dec-e", status="pending", decision="e", branch="main")], branch="main")
    r = CliRunner().invoke(cli, ["edit", "dec-e", "new text"])
    assert r.exit_code == 0, r.output
    d = {x.id: x for x in read_all_decisions(initialized_repo)}["dec-e"]
    assert (d.status, d.decision, d.approved_by) == ("edited", "new text", "user")


def test_review_approve_sets_approved_by_user(initialized_repo, monkeypatch):
    from plumb.decision_log import append_decisions
    monkeypatch.chdir(initialized_repo)
    append_decisions(initialized_repo, [Decision(id="dec-rv", status="pending", decision="rv", branch="main")], branch="main")
    r = CliRunner().invoke(cli, ["review"], input="a\n")
    assert r.exit_code == 0, r.output
    d = {x.id: x for x in read_all_decisions(initialized_repo)}["dec-rv"]
    assert (d.status, d.approved_by) == ("approved", "user")


def test_sync_cli_precheck_counts_recorded(initialized_repo, monkeypatch):
    from plumb.decision_log import append_decisions
    monkeypatch.chdir(initialized_repo)
    append_decisions(initialized_repo, [Decision(id="dec-rs", status="recorded", decision="rs", branch="main")], branch="main")
    stub = MagicMock(return_value={"spec_updated": 0, "tests_generated": 0})
    with patch("plumb.sync.sync_decisions", stub):
        r = CliRunner().invoke(cli, ["sync"])
    assert r.exit_code == 0, r.output
    assert "No unsynced decisions to sync." not in r.output
    stub.assert_called_once()


def test_search_cli(initialized_repo, monkeypatch):
    from tests.test_search import _seed
    _seed(initialized_repo)
    monkeypatch.chdir(initialized_repo)
    runner = CliRunner()

    r = runner.invoke(cli, ["search", "cache", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert sorted(d["id"] for d in data) == ["d1", "d3"]
    by_id = {d["id"]: d for d in data}
    assert by_id["d1"]["score"] > 0 and by_id["d1"]["file_refs"] == [{"file": "src/cache.py", "lines": [1, 9]}]

    r = runner.invoke(cli, ["search", "--sort", "date", "--limit", "1"])
    assert r.exit_code == 0, r.output
    assert "d3" in r.output and "d2" not in r.output

    r = runner.invoke(cli, ["search", "--file", "src/auth.py"])
    assert r.exit_code == 0, r.output
    assert "d2" in r.output and "files: src/auth.py:42-58" in r.output and "d1" not in r.output

    # Human output: recorded rows show approved_by; score only under relevance sort.
    r = runner.invoke(cli, ["search", "cache"])
    assert r.exit_code == 0, r.output
    assert "recorded" in r.output and "auto" in r.output and "score" in r.output
    assert "session 019a0000 turns 4-5" in r.output
    r = runner.invoke(cli, ["search", "--status", "recorded"])
    assert "score" not in r.output

    r = runner.invoke(cli, ["search", "--since", "no-such-ref"])
    assert r.exit_code == 1 and "no-such-ref" in r.output

    r = runner.invoke(cli, ["search", "zzzz-nothing"])
    assert r.exit_code == 0 and "No decisions." in r.output
