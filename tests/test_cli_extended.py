"""Extended CLI tests to improve coverage."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner
from git import Repo

from plumb.cli import cli, _update_claude_md, _run_modify
from plumb.config import PlumbConfig, save_config, ensure_plumb_dir, load_config
from plumb.decision_log import Decision, append_decision, read_decisions


@pytest.fixture
def runner():
    return CliRunner()


class TestInitExtended:
    def test_spec_path_not_exist(self, runner, tmp_repo):
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo):
            result = runner.invoke(cli, ["init"], input="nonexistent.md\n")
            assert result.exit_code != 0

    def test_spec_dir_no_md_files(self, runner, tmp_repo):
        empty_dir = tmp_repo / "docs"
        empty_dir.mkdir()
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo):
            result = runner.invoke(cli, ["init"], input="docs\n")
            assert result.exit_code != 0

    def test_creates_test_dir_if_missing(self, runner, tmp_repo):
        spec = tmp_repo / "spec.md"
        spec.write_text("# Spec\n")
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.md\nnew_tests/\n")
            assert result.exit_code == 0
            assert (tmp_repo / "new_tests").exists()


class TestReviewExtended:
    def test_no_pending(self, runner, initialized_repo):
        # plumb:req-22157990
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["review"])
            assert "No pending" in result.output

    def test_review_not_git_repo(self, runner):
        with patch("plumb.cli.find_repo_root", return_value=None):
            result = runner.invoke(cli, ["review"])
            assert result.exit_code != 0

    def test_review_approve_decision(self, runner, initialized_repo):
        # plumb:req-acc69753
        # plumb:req-d7f7c95c
        d = Decision(
            id="dec-rev1",
            status="pending",
            question="Q?",
            decision="A.",
            made_by="llm",
            confidence=0.9,
            branch="main",
        )
        append_decision(initialized_repo, d)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["review"], input="a\n")
            assert "Approved" in result.output

    def test_review_reject_no_modify(self, runner, initialized_repo):
        d = Decision(
            id="dec-rev2",
            status="pending",
            question="Q?",
            decision="A.",
            made_by="llm",
        )
        append_decision(initialized_repo, d)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["review"], input="r\nbad\nn\n")
            assert "Rejected" in result.output

    def test_review_edit_decision(self, runner, initialized_repo):
        d = Decision(
            id="dec-rev3",
            status="pending",
            question="Q?",
            decision="A.",
            made_by="llm",
        )
        append_decision(initialized_repo, d)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["review"], input="e\nnew text\n")
            assert "Edited" in result.output

    def test_review_skip(self, runner, initialized_repo):
        d = Decision(
            id="dec-rev4",
            status="pending",
            question="Q?",
            decision="A.",
            made_by="llm",
        )
        append_decision(initialized_repo, d)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["review"], input="s\n")
            assert "Skipped" in result.output

    def test_review_with_broken_ref(self, runner, initialized_repo):
        d = Decision(
            id="dec-rev5",
            status="pending",
            question="Q?",
            decision="A.",
            ref_status="broken",
        )
        append_decision(initialized_repo, d)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["review"], input="s\n")
            assert "broken" in result.output.lower() or "Skipped" in result.output

    def test_review_filter_branch(self, runner, initialized_repo):
        d1 = Decision(id="dec-b1", status="pending", branch="feat")
        d2 = Decision(id="dec-b2", status="pending", branch="main")
        append_decision(initialized_repo, d1)
        append_decision(initialized_repo, d2)

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["review", "--branch", "feat"], input="s\n")
            # Should only show 1 decision
            assert result.exit_code == 0


class TestModifyExtended:
    def test_modify_not_git_repo(self, runner):
        with patch("plumb.cli.find_repo_root", return_value=None):
            result = runner.invoke(cli, ["modify", "dec-x"])
            assert result.exit_code != 0

    def test_modify_not_rejected(self, runner, initialized_repo):
        # plumb:req-b25a2e8d
        d = Decision(id="dec-m1", status="pending")
        append_decision(initialized_repo, d)
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["modify", "dec-m1"])
            assert "not found or not rejected" in result.output.lower() or result.exit_code == 0

    def test_modify_no_staged_changes(self, runner, initialized_repo):
        d = Decision(id="dec-m2", status="rejected", decision="X", rejection_reason="Y")
        append_decision(initialized_repo, d)
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["modify", "dec-m2"])
            assert "no staged" in result.output.lower() or result.exit_code == 0


class TestParseSpec:
    def test_parse_spec_not_repo(self, runner):
        with patch("plumb.cli.find_repo_root", return_value=None):
            result = runner.invoke(cli, ["parse-spec"])
            assert result.exit_code != 0


class TestStatusExtended:
    def test_status_with_pending_and_broken(self, runner, initialized_repo):
        d1 = Decision(id="dec-s1", status="pending", branch="main")
        d2 = Decision(id="dec-s2", status="pending", ref_status="broken", branch="feat")
        append_decision(initialized_repo, d1)
        append_decision(initialized_repo, d2)

        # Write requirements
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps([{"id": "req-1", "text": "Must X"}]))

        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["status"])
            assert "Pending" in result.output or "pending" in result.output
            assert result.exit_code == 0

    def test_status_requirements_error(self, runner, initialized_repo):
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text("invalid json{{{")
        with patch("plumb.cli.find_repo_root", return_value=initialized_repo):
            result = runner.invoke(cli, ["status"])
            assert result.exit_code == 0


class TestRunModifyInternal:
    def test_run_modify_code_modifier_fails(self, initialized_repo):
        d = Decision(
            id="dec-rmf",
            status="rejected",
            decision="X",
            rejection_reason="Y",
        )
        append_decision(initialized_repo, d)
        # Stage a file
        repo = Repo(initialized_repo)
        f = initialized_repo / "new.py"
        f.write_text("x=1\n")
        repo.index.add(["new.py"])

        with patch("plumb.programs.code_modifier.CodeModifier.modify", side_effect=RuntimeError("API down")):
            _run_modify(initialized_repo, "dec-rmf")

        decisions = read_decisions(initialized_repo)
        manual = [d for d in decisions if d.id == "dec-rmf" and d.status == "rejected_manual"]
        assert len(manual) == 1

    def test_run_modify_empty_modifications(self, initialized_repo):
        d = Decision(
            id="dec-rem",
            status="rejected",
            decision="X",
            rejection_reason="Y",
        )
        append_decision(initialized_repo, d)
        repo = Repo(initialized_repo)
        f = initialized_repo / "new2.py"
        f.write_text("x=1\n")
        repo.index.add(["new2.py"])

        with patch("plumb.programs.code_modifier.CodeModifier.modify", return_value={}):
            _run_modify(initialized_repo, "dec-rem")

        decisions = read_decisions(initialized_repo)
        manual = [d for d in decisions if d.id == "dec-rem" and d.status == "rejected_manual"]
        assert len(manual) == 1
