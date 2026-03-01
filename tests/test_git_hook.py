import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from git import Repo

from plumb import PlumbAuthError
from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb.decision_log import Decision, append_decision, read_decisions
from plumb.git_hook import (
    run_hook,
    _get_staged_diff,
    _get_staged_diff_filtered,
    _get_plumb_managed_paths,
    _get_branch_name,
    _detect_amend,
    _check_broken_refs,
    _format_tty_output,
    _format_json_output,
)


class TestGetStagedDiff:
    def test_returns_diff(self, tmp_repo):
        repo = Repo(tmp_repo)
        f = tmp_repo / "new.py"
        f.write_text("x = 1\n")
        repo.index.add(["new.py"])
        diff = _get_staged_diff(repo)
        assert "x = 1" in diff

    def test_empty_when_nothing_staged(self, tmp_repo):
        repo = Repo(tmp_repo)
        diff = _get_staged_diff(repo)
        assert diff == ""


class TestGetPlumbManagedPaths:
    def test_includes_plumb_dir_and_spec(self, sample_config):
        paths = _get_plumb_managed_paths(sample_config)
        assert ".plumb/" in paths
        assert "spec.md" in paths

    def test_multiple_spec_paths(self):
        from plumb.config import PlumbConfig
        cfg = PlumbConfig(spec_paths=["spec.md", "docs/spec/"])
        paths = _get_plumb_managed_paths(cfg)
        assert len(paths) == 3  # .plumb/ + 2 spec paths


class TestGetStagedDiffFiltered:
    def test_excludes_spec_file(self, initialized_repo):
        repo = Repo(initialized_repo)
        # Stage changes to both a code file and the spec file
        code = initialized_repo / "app.py"
        code.write_text("x = 1\n")
        spec = initialized_repo / "spec.md"
        spec.write_text("# Updated Spec\n")
        repo.index.add(["app.py", "spec.md"])

        from plumb.config import load_config
        config = load_config(initialized_repo)
        diff = _get_staged_diff_filtered(repo, config)
        assert "x = 1" in diff
        assert "Updated Spec" not in diff

    def test_excludes_plumb_dir(self, initialized_repo):
        repo = Repo(initialized_repo)
        code = initialized_repo / "app.py"
        code.write_text("x = 1\n")
        plumb_file = initialized_repo / ".plumb" / "decisions.jsonl"
        plumb_file.write_text('{"id": "dec-1"}\n')
        repo.index.add(["app.py", ".plumb/decisions.jsonl"])

        from plumb.config import load_config
        config = load_config(initialized_repo)
        diff = _get_staged_diff_filtered(repo, config)
        assert "x = 1" in diff
        assert "dec-1" not in diff

    def test_empty_when_only_managed_files(self, initialized_repo):
        repo = Repo(initialized_repo)
        spec = initialized_repo / "spec.md"
        spec.write_text("# Updated\n")
        repo.index.add(["spec.md"])

        from plumb.config import load_config
        config = load_config(initialized_repo)
        diff = _get_staged_diff_filtered(repo, config)
        assert diff == ""

    def test_empty_when_nothing_staged(self, initialized_repo):
        repo = Repo(initialized_repo)
        from plumb.config import load_config
        config = load_config(initialized_repo)
        diff = _get_staged_diff_filtered(repo, config)
        assert diff == ""


class TestGetBranchName:
    def test_returns_main(self, tmp_repo):
        repo = Repo(tmp_repo)
        name = _get_branch_name(repo)
        assert name in ("main", "master")


class TestDetectAmend:
    def test_no_last_commit(self, tmp_repo):
        repo = Repo(tmp_repo)
        assert _detect_amend(repo, None) is False

    def test_not_amend(self, tmp_repo):
        repo = Repo(tmp_repo)
        # Make a second commit
        f = tmp_repo / "a.py"
        f.write_text("a = 1\n")
        repo.index.add(["a.py"])
        repo.index.commit("second")
        # last_commit is the initial commit — HEAD parent matches, so it IS an amend
        initial_sha = str(list(repo.iter_commits())[-1])
        assert _detect_amend(repo, "nonexistent_sha") is False


class TestCheckBrokenRefs:
    def test_ok_ref(self, tmp_repo):
        repo = Repo(tmp_repo)
        sha = str(repo.head.commit)
        d = Decision(id="dec-1", commit_sha=sha)
        result = _check_broken_refs(repo, [d])
        assert result[0].ref_status == "ok"

    def test_broken_ref(self, tmp_repo):
        repo = Repo(tmp_repo)
        d = Decision(id="dec-1", commit_sha="deadbeef" * 5)
        result = _check_broken_refs(repo, [d])
        assert result[0].ref_status == "broken"

    def test_no_commit_sha(self, tmp_repo):
        repo = Repo(tmp_repo)
        d = Decision(id="dec-1")
        result = _check_broken_refs(repo, [d])
        assert result[0].ref_status == "ok"


class TestFormatOutput:
    def test_tty_output(self):
        pending = [
            Decision(
                id="dec-abc",
                question="Q?",
                decision="A.",
                made_by="user",
                confidence=0.9,
            )
        ]
        output = _format_tty_output(pending)
        assert "dec-abc" in output
        assert "Q?" in output
        assert "plumb review" in output

    def test_json_output(self):
        pending = [
            Decision(
                id="dec-abc",
                question="Q?",
                decision="A.",
                made_by="llm",
                confidence=0.85,
            )
        ]
        output = _format_json_output(pending)
        data = json.loads(output)
        assert data["pending_decisions"] == 1
        assert data["decisions"][0]["id"] == "dec-abc"


class TestRunHook:
    def test_no_config_returns_0(self, tmp_repo):
        """If plumb not initialized, exit 0."""
        assert run_hook(tmp_repo) == 0

    def test_no_staged_diff_returns_0(self, initialized_repo):
        """No staged changes means nothing to analyze."""
        assert run_hook(initialized_repo) == 0

    def test_error_returns_0(self, initialized_repo):
        """Internal errors should never block commits."""
        with patch("plumb.git_hook._run_hook_inner", side_effect=RuntimeError("boom")):
            result = run_hook(initialized_repo)
            assert result == 0

    def test_auth_error_blocks_commit(self, initialized_repo):
        """Auth errors should block commits (exit 1)."""
        with patch(
            "plumb.git_hook._run_hook_inner",
            side_effect=PlumbAuthError("ANTHROPIC_API_KEY is not set"),
        ):
            result = run_hook(initialized_repo)
            assert result == 1

    def test_missing_api_key_blocks_commit(self, initialized_repo):
        """Missing ANTHROPIC_API_KEY should block commit when there's a staged diff."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "new.py"
        f.write_text("x = 1\n")
        repo.index.add(["new.py"])

        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {}, clear=True), \
             patch.dict("os.environ", {"HOME": "/tmp"}):
            # Remove ANTHROPIC_API_KEY if present
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = run_hook(initialized_repo)
            assert result == 1

    def test_dry_run_returns_0(self, initialized_repo):
        """Dry run always returns 0."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "new.py"
        f.write_text("x = 1\n")
        repo.index.add(["new.py"])

        # Mock the DSPy calls
        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="summary"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=[]), \
             patch("plumb.git_hook._extract_decisions_from_diff", return_value=[]):
            result = run_hook(initialized_repo, dry_run=True)
            assert result == 0

    def test_pending_decisions_block_commit(self, initialized_repo):
        """Pending decisions should cause exit 1."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "new.py"
        f.write_text("x = 1\n")
        repo.index.add(["new.py"])

        mock_decisions = [
            Decision(
                id="dec-test1",
                status="pending",
                question="Q?",
                decision="A.",
                made_by="llm",
                confidence=0.8,
            )
        ]

        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="summary"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=mock_decisions), \
             patch("plumb.git_hook._synthesize_questions", return_value=mock_decisions):
            result = run_hook(initialized_repo)
            assert result == 1

    def test_no_pending_decisions_allow_commit(self, initialized_repo):
        """No pending decisions should allow commit (exit 0)."""
        repo = Repo(initialized_repo)
        f = initialized_repo / "new.py"
        f.write_text("x = 1\n")
        repo.index.add(["new.py"])

        with patch("plumb.programs.validate_api_access"), \
             patch("plumb.git_hook._analyze_diff", return_value="summary"), \
             patch("plumb.git_hook._extract_decisions_from_conversation", return_value=[]), \
             patch("plumb.git_hook._extract_decisions_from_diff", return_value=[]), \
             patch("plumb.coverage_reporter.print_coverage_report"):
            result = run_hook(initialized_repo)
            assert result == 0
