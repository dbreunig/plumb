import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb.decision_log import Decision, append_decision, read_decisions
from plumb.sync import (
    _generate_requirement_id,
    _atomic_write,
    sync_decisions,
    parse_spec_files,
)


class TestGenerateRequirementId:
    def test_format(self):
        # plumb:req-dda066fd
        rid = _generate_requirement_id("The system must do X.")
        assert rid.startswith("req-")
        assert len(rid) == 12  # req- + 8 hex

    def test_stability(self):
        r1 = _generate_requirement_id("Hello world")
        r2 = _generate_requirement_id("Hello world")
        assert r1 == r2

    def test_case_insensitive(self):
        r1 = _generate_requirement_id("Hello World")
        r2 = _generate_requirement_id("hello world")
        assert r1 == r2

    def test_strips_whitespace(self):
        r1 = _generate_requirement_id("  hello  ")
        r2 = _generate_requirement_id("hello")
        assert r1 == r2


class TestAtomicWrite:
    def test_writes_file(self, tmp_path):
        # plumb:req-06185a82
        f = tmp_path / "test.txt"
        _atomic_write(f, "hello")
        assert f.read_text() == "hello"

    def test_overwrites_existing(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("old")
        _atomic_write(f, "new")
        assert f.read_text() == "new"


class TestSyncDecisions:
    def test_no_config(self, tmp_repo):
        result = sync_decisions(tmp_repo)
        assert result == {"spec_updated": 0, "tests_generated": 0}

    def test_no_decisions_to_sync(self, initialized_repo):
        result = sync_decisions(initialized_repo)
        assert result["spec_updated"] == 0

    def test_syncs_approved_decision(self, initialized_repo):
        d = Decision(
            id="dec-sync1",
            status="approved",
            question="How to cache?",
            decision="Use in-memory dict.",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        append_decision(initialized_repo, d, branch="main")

        call_count = [0]

        def mock_run(fn, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # WholeFileSpecUpdater returns (updates, new_sections)
                return [{"header": "## Features", "content": "The system uses in-memory dict cache.\n"}], []
            # parse_spec_files parser
            return []

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", side_effect=mock_run):
            result = sync_decisions(initialized_repo)

        assert result["spec_updated"] == 1
        # Decision should be marked synced
        decisions = read_decisions(initialized_repo, branch="main")
        synced = [d for d in decisions if d.id == "dec-sync1" and d.synced_at]
        assert len(synced) == 1

    def test_skips_already_synced(self, initialized_repo):
        d = Decision(
            id="dec-sync2",
            status="approved",
            synced_at=datetime.now(timezone.utc).isoformat(),
        )
        append_decision(initialized_repo, d, branch="main")
        result = sync_decisions(initialized_repo)
        assert result["spec_updated"] == 0

    def test_filters_by_decision_ids(self, initialized_repo):
        d1 = Decision(id="dec-yes", status="approved", decision="A")
        d2 = Decision(id="dec-no", status="approved", decision="B")
        append_decision(initialized_repo, d1, branch="main")
        append_decision(initialized_repo, d2, branch="main")

        call_count = [0]

        def mock_run(fn, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # WholeFileSpecUpdater returns (updates, new_sections)
                return [{"header": "## Features", "content": "Updated.\n"}], []
            return []

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", side_effect=mock_run):
            result = sync_decisions(initialized_repo, decision_ids=["dec-yes"])

        decisions = read_decisions(initialized_repo, branch="main")
        yes_synced = [d for d in decisions if d.id == "dec-yes" and d.synced_at]
        no_synced = [d for d in decisions if d.id == "dec-no" and d.synced_at]
        assert len(yes_synced) == 1
        assert len(no_synced) == 0


class TestParseSpecFiles:
    def test_parses_spec(self, initialized_repo):
        # plumb:req-b3844050
        # plumb:req-c76392d0
        # plumb:req-0256d633
        mock_reqs = [
            MagicMock(text="The system must do X.", ambiguous=False),
            MagicMock(text="The system should maybe Y.", ambiguous=True),
        ]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=mock_reqs):
            result = parse_spec_files(initialized_repo)

        assert len(result) == 2
        # Check requirements.json was written
        req_path = initialized_repo / ".plumb" / "requirements.json"
        assert req_path.exists()
        data = json.loads(req_path.read_text())
        assert len(data) == 2
        assert data[0]["id"].startswith("req-")

    def test_preserves_created_at_for_existing_requirements(self, initialized_repo):
        """Re-parsing spec should preserve created_at from existing requirements."""
        old_time = "2025-01-01T00:00:00+00:00"
        old_commit = "abc123"
        req_id = _generate_requirement_id("The system must do X.")
        existing_reqs = [
            {
                "id": req_id,
                "source_file": "spec.md",
                "source_section": "",
                "text": "The system must do X.",
                "ambiguous": False,
                "created_at": old_time,
                "last_seen_commit": old_commit,
            }
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        _atomic_write(req_path, json.dumps(existing_reqs, indent=2) + "\n")

        mock_reqs = [MagicMock(text="The system must do X.", ambiguous=False)]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=mock_reqs):
            result = parse_spec_files(initialized_repo)

        assert len(result) == 1
        assert result[0]["created_at"] == old_time
        assert result[0]["last_seen_commit"] == old_commit

    def test_new_requirement_gets_current_timestamp(self, initialized_repo):
        """New requirements (not in existing) should get fresh created_at."""
        mock_reqs = [MagicMock(text="Brand new requirement.", ambiguous=False)]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=mock_reqs):
            result = parse_spec_files(initialized_repo)

        assert len(result) == 1
        assert result[0]["created_at"] is not None
        assert result[0]["last_seen_commit"] is None


class TestSyncDecisionsWholeFile:
    """Tests for the whole-file spec update path."""

    def test_calls_whole_file_updater_once_per_file(self, initialized_repo):
        """Multiple decisions should result in one updater call, not N."""
        d1 = Decision(id="dec-wf1", status="approved",
                      question="Auth method?", decision="JWT tokens.",
                      created_at=datetime.now(timezone.utc).isoformat())
        d2 = Decision(id="dec-wf2", status="approved",
                      question="Cache strategy?", decision="In-memory dict.",
                      created_at=datetime.now(timezone.utc).isoformat())
        append_decision(initialized_repo, d1, branch="main")
        append_decision(initialized_repo, d2, branch="main")

        updater_call_count = [0]

        def mock_run(fn, *args, **kwargs):
            from plumb.programs.spec_updater import WholeFileSpecUpdater
            if isinstance(fn, WholeFileSpecUpdater):
                updater_call_count[0] += 1
                return [{"header": "## Features", "content": "Updated features.\n"}], []
            return []

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", side_effect=mock_run):
            result = sync_decisions(initialized_repo)

        assert updater_call_count[0] == 1  # One call, not two
        assert result["spec_updated"] >= 1

    def test_handles_new_sections_with_outline_merge(self, initialized_repo):
        """When new sections are returned, outline merger should be called."""
        d = Decision(id="dec-ns1", status="approved",
                     question="Add caching?", decision="Yes, Redis.",
                     created_at=datetime.now(timezone.utc).isoformat())
        append_decision(initialized_repo, d, branch="main")

        call_sequence = []

        def mock_run(fn, *args, **kwargs):
            from plumb.programs.spec_updater import WholeFileSpecUpdater, OutlineMerger
            if isinstance(fn, WholeFileSpecUpdater):
                call_sequence.append("updater")
                return [], [{"header": "## Cache", "content": "Redis cache.\n"}]
            elif isinstance(fn, OutlineMerger):
                call_sequence.append("merger")
                return ["# Spec", "## Features", "## Cache"]
            return []

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", side_effect=mock_run):
            result = sync_decisions(initialized_repo)

        assert "updater" in call_sequence
        assert "merger" in call_sequence

    def test_fallback_appends_when_merger_fails(self, initialized_repo):
        """When outline merger fails, new sections should be appended at end."""
        d = Decision(id="dec-fb1", status="approved",
                     question="Add logging?", decision="Yes, structured logging.",
                     created_at=datetime.now(timezone.utc).isoformat())
        append_decision(initialized_repo, d, branch="main")

        def mock_run(fn, *args, **kwargs):
            from plumb.programs.spec_updater import WholeFileSpecUpdater, OutlineMerger
            if isinstance(fn, WholeFileSpecUpdater):
                return [], [{"header": "## Logging", "content": "Structured logging.\n"}]
            elif isinstance(fn, OutlineMerger):
                raise Exception("LLM failed")
            return []

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", side_effect=mock_run):
            result = sync_decisions(initialized_repo)

        # Section should still be added (fallback appends at end)
        spec_content = (initialized_repo / "spec.md").read_text()
        assert "## Logging" in spec_content
        assert "Structured logging." in spec_content
