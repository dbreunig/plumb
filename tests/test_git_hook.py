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
from plumb.programs.decision_extractor import ExtractedDecision
from plumb.git_hook import (
    run_hook,
    _get_staged_diff,
    _get_staged_diff_filtered,
    _get_plumb_managed_paths,
    _get_branch_name,
    _detect_amend,
    _check_broken_refs,
    _extract_decisions_from_conversation,
    _extract_decisions_from_diff,
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


class TestGetStagedDiffFilteredWithIgnore:
    def test_ignored_files_excluded(self, initialized_repo):
        repo = Repo(initialized_repo)
        # Create .plumbignore
        (initialized_repo / ".plumbignore").write_text("README.md\n*.txt\n")
        # Stage a code file, a README, and a txt file
        (initialized_repo / "app.py").write_text("x = 1\n")
        (initialized_repo / "notes.txt").write_text("some notes\n")
        readme = initialized_repo / "README.md"
        readme.write_text("# Updated README\n")
        repo.index.add(["app.py", "notes.txt", "README.md"])

        from plumb.config import load_config
        config = load_config(initialized_repo)
        diff = _get_staged_diff_filtered(repo, config)
        assert "x = 1" in diff
        assert "some notes" not in diff
        assert "Updated README" not in diff

    def test_no_plumbignore_still_works(self, initialized_repo):
        repo = Repo(initialized_repo)
        (initialized_repo / "app.py").write_text("x = 1\n")
        repo.index.add(["app.py"])

        from plumb.config import load_config
        config = load_config(initialized_repo)
        diff = _get_staged_diff_filtered(repo, config)
        assert "x = 1" in diff

    def test_all_ignored_returns_empty(self, initialized_repo):
        repo = Repo(initialized_repo)
        (initialized_repo / ".plumbignore").write_text("*.txt\n")
        (initialized_repo / "notes.txt").write_text("hello\n")
        repo.index.add(["notes.txt"])

        from plumb.config import load_config
        config = load_config(initialized_repo)
        diff = _get_staged_diff_filtered(repo, config)
        assert diff == ""


class TestGetBranchName:
    def test_returns_main(self, tmp_repo):
        # plumb:req-c5fc9f66
        repo = Repo(tmp_repo)
        name = _get_branch_name(repo)
        assert name in ("main", "master")


class TestDetectAmend:
    def test_no_last_commit(self, tmp_repo):
        repo = Repo(tmp_repo)
        assert _detect_amend(repo, None) is False

    def test_not_amend(self, tmp_repo):
        # plumb:req-7fb50a59
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
        # plumb:req-280a71d8
        # plumb:req-1f885ef1
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
        # plumb:req-6f83d98c
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
        # plumb:req-cee2a552
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
        # plumb:req-eb649dd1
        """If plumb not initialized, exit 0."""
        assert run_hook(tmp_repo) == 0

    def test_no_staged_diff_returns_0(self, initialized_repo):
        # plumb:req-bafc9fa8
        """No staged changes means nothing to analyze."""
        assert run_hook(initialized_repo) == 0

    def test_error_returns_0(self, initialized_repo):
        # plumb:req-8e003e34
        # plumb:req-2699997e
        """Internal errors should never block commits."""
        with patch("plumb.git_hook._run_hook_inner", side_effect=RuntimeError("boom")):
            result = run_hook(initialized_repo)
            assert result == 0

    def test_auth_error_blocks_commit(self, initialized_repo):
        # plumb:req-b42c75c3
        """Auth errors should block commits (exit 1)."""
        with patch(
            "plumb.git_hook._run_hook_inner",
            side_effect=PlumbAuthError("ANTHROPIC_API_KEY is not set"),
        ):
            result = run_hook(initialized_repo)
            assert result == 1

    def test_missing_api_key_blocks_commit(self, initialized_repo):
        # plumb:req-3f212a0d
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
        # plumb:req-970aa4c2
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
        # plumb:req-bdfb0f18
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
        # plumb:req-124ad3e8
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


class TestSpecRelevantFiltering:
    """Non-spec-relevant decisions should be filtered out during extraction."""

    def test_conversation_extraction_filters_non_spec_relevant(self, initialized_repo):
        mixed_extracted = [
            ExtractedDecision(
                question="Use sync or async?",
                decision="Use sync",
                made_by="user",
                confidence=0.9,
                spec_relevant=True,
            ),
            ExtractedDecision(
                question="Should we commit now?",
                decision="Yes, commit now",
                made_by="user",
                confidence=0.8,
                spec_relevant=False,
            ),
            ExtractedDecision(
                question="Push to main?",
                decision="Push to main",
                made_by="user",
                confidence=0.7,
                spec_relevant=False,
            ),
        ]

        from plumb.conversation import Chunk
        mock_chunks = [Chunk(chunk_index=0, agent="claude", session_id="s1",
                             turn_start=0, turn_end=0, turns=[])]

        with patch("plumb.git_hook.read_conversation_with_refs", return_value=([MagicMock()], {})), \
             patch("plumb.git_hook.reduce_noise", return_value=[MagicMock()]), \
             patch("plumb.git_hook.chunk_conversation", return_value=mock_chunks), \
             patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=mixed_extracted):
            from plumb.config import load_config
            config = load_config(initialized_repo)
            decisions = _extract_decisions_from_conversation(
                initialized_repo, config, "diff summary"
            )
            assert len(decisions) == 1
            assert decisions[0].decision == "Use sync"

    def test_diff_extraction_filters_non_spec_relevant(self):
        mixed_extracted = [
            ExtractedDecision(
                question="Add caching layer?",
                decision="Add Redis cache",
                made_by="llm",
                confidence=0.85,
                spec_relevant=True,
            ),
            ExtractedDecision(
                question="Run with --dry-run?",
                decision="Use dry-run first",
                made_by="user",
                confidence=0.6,
                spec_relevant=False,
            ),
        ]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=mixed_extracted):
            decisions = _extract_decisions_from_diff("diff summary", "main")
            assert len(decisions) == 1
            assert decisions[0].decision == "Add Redis cache"

    def test_all_spec_relevant_keeps_all(self):
        all_relevant = [
            ExtractedDecision(decision="Use sync", spec_relevant=True),
            ExtractedDecision(decision="Add cache", spec_relevant=True),
        ]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=all_relevant):
            decisions = _extract_decisions_from_diff("diff summary", "main")
            assert len(decisions) == 2

    def test_all_non_spec_relevant_returns_empty(self):
        none_relevant = [
            ExtractedDecision(decision="commit now", spec_relevant=False),
            ExtractedDecision(decision="push to main", spec_relevant=False),
        ]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=none_relevant):
            decisions = _extract_decisions_from_diff("diff summary", "main")
            assert len(decisions) == 0


# --- Task 9: provenance + file_refs stamping -------------------------------

from plumb.traces import SessionRef, ToolCall, Turn


def test_extraction_stamps_provenance_and_file_refs(initialized_repo):
    from plumb.config import load_config

    # stage edits to src/a.py and src/other.py so hunks exist for both
    (initialized_repo / "src").mkdir()
    (initialized_repo / "src" / "a.py").write_text("x = 1\n")
    (initialized_repo / "src" / "other.py").write_text("y = 2\n")
    Repo(initialized_repo).index.add(["src/a.py", "src/other.py"])

    ref = SessionRef(agent="codex", session_id="019a", path="/tmp/r.jsonl", cwd=str(initialized_repo))
    sub = SessionRef(agent="codex", session_id="agent-k", path="/tmp/k.jsonl",
                     cwd=str(initialized_repo), parent_session_id="019a")
    turns = [
        # a preceding chunk in the same session; its last turn edits src/other.py
        # and becomes the one-turn overlap of the next chunk
        Turn(agent="codex", session_id="019a", ordinal=0, role="user", content="make y 2"),
        Turn(agent="codex", session_id="019a", ordinal=1, role="assistant", content="ok",
             tool_calls=[ToolCall(name="apply_patch", category="Edit", file_path="src/other.py")]),
        Turn(agent="codex", session_id="019a", ordinal=4, role="user", content="make x 1"),
        Turn(agent="codex", session_id="019a", ordinal=5, role="assistant", content="done",
             tool_calls=[ToolCall(name="apply_patch", category="Edit", file_path="src/a.py")]),
        Turn(agent="codex", session_id="agent-k", ordinal=0, role="user", content="subtask"),
    ]
    refs = {("codex", "019a"): ref, ("codex", "agent-k"): sub}
    extracted = [ExtractedDecision(question="x?", decision="x is 1", made_by="user", confidence=0.9)]

    with patch("plumb.git_hook.read_conversation_with_refs", return_value=(turns, refs)), \
         patch("plumb.programs.configure_dspy"), \
         patch("plumb.programs.run_with_retries", return_value=extracted):
        decisions = _extract_decisions_from_conversation(
            initialized_repo, load_config(initialized_repo), "summary")

    assert len(decisions) == 3  # one per chunk: 019a[0-1], 019a[4-5], agent-k[0-0]
    by_key = {(d.session_id, tuple(d.turn_range)): d for d in decisions}
    first = by_key[("019a", (0, 1))]
    assert [(r.file, r.lines) for r in first.file_refs] == [("src/other.py", [1, 1])]
    d = by_key[("019a", (4, 5))]
    assert (d.agent, d.turn_range, d.source_path) == ("codex", [4, 5], "/tmp/r.jsonl")
    assert d.parent_session_id is None
    assert len(d.evidence_digest) == 64
    # the overlap turn (ordinal 1, edits src/other.py) must NOT contribute file_refs
    assert [(r.file, r.lines) for r in d.file_refs] == [("src/a.py", [1, 1])]
    assert d.chunk_index is None
    k = by_key[("agent-k", (0, 0))]
    assert (k.parent_session_id, k.source_path, k.file_refs) == ("019a", "/tmp/k.jsonl", [])


def test_read_conversation_no_longer_accepts_config_path(tmp_repo):
    import inspect
    from plumb.conversation import read_conversation
    assert "config_path" not in inspect.signature(read_conversation).parameters
    assert "claude_log_path" not in PlumbConfig.model_fields
    # old config files with the key still load
    cfg = PlumbConfig(**{"spec_paths": ["s.md"], "claude_log_path": "/x"})
    assert cfg.spec_paths == ["s.md"]


class TestRunPostCommitStampsSha:
    def _seed(self, repo_root, dec_id, created_at):
        append_decision(repo_root, Decision(
            id=dec_id, status="approved", decision="x", branch="main",
            commit_sha=None, created_at=created_at,
        ), branch="main")

    def _new_commit(self, repo_root):
        repo = Repo(repo_root)
        (repo_root / "f.txt").write_text("f\n")
        repo.index.add(["f.txt"])
        return str(repo.index.commit("second"))

    def test_stamps_decisions_created_after_previous_commit(self, initialized_repo):
        from plumb.config import load_config
        from plumb.git_hook import run_post_commit
        repo = Repo(initialized_repo)
        cfg = load_config(initialized_repo)
        cfg.last_commit = str(repo.head.commit)
        save_config(initialized_repo, cfg)
        self._seed(initialized_repo, "dec-new", datetime.now(timezone.utc).isoformat())
        self._seed(initialized_repo, "dec-old", "2020-01-01T00:00:00Z")

        new_sha = self._new_commit(initialized_repo)
        run_post_commit(initialized_repo)

        by_id = {d.id: d for d in read_decisions(initialized_repo, branch="main")}
        assert by_id["dec-new"].commit_sha == new_sha
        assert by_id["dec-old"].commit_sha is None
        assert load_config(initialized_repo).last_commit == new_sha

    def test_no_previous_commit_stamps_nothing(self, initialized_repo):
        from plumb.config import load_config
        from plumb.git_hook import run_post_commit
        cfg = load_config(initialized_repo)
        cfg.last_commit = None
        save_config(initialized_repo, cfg)
        self._seed(initialized_repo, "dec-new", datetime.now(timezone.utc).isoformat())

        new_sha = self._new_commit(initialized_repo)
        run_post_commit(initialized_repo)

        assert read_decisions(initialized_repo, branch="main")[0].commit_sha is None
        assert load_config(initialized_repo).last_commit == new_sha

    def test_skips_other_branch_and_ignored(self, initialized_repo):
        from plumb.config import load_config
        from plumb.git_hook import run_post_commit
        repo = Repo(initialized_repo)
        cfg = load_config(initialized_repo)
        cfg.last_commit = str(repo.head.commit)
        save_config(initialized_repo, cfg)
        now = datetime.now(timezone.utc).isoformat()
        append_decision(initialized_repo, Decision(
            id="dec-other", status="approved", decision="x", branch="feature", commit_sha=None, created_at=now,
        ), branch="feature")
        append_decision(initialized_repo, Decision(
            id="dec-ign", status="ignored", decision="x", branch="main", commit_sha=None, created_at=now,
        ), branch="main")

        self._new_commit(initialized_repo)
        run_post_commit(initialized_repo)

        assert read_decisions(initialized_repo, branch="feature")[0].commit_sha is None
        assert read_decisions(initialized_repo, branch="main")[0].commit_sha is None
