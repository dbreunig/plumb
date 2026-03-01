import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb.decision_log import Decision, append_decision, read_decisions
from plumb.sync import (
    _generate_requirement_id,
    _atomic_write,
    find_spec_section,
    sync_decisions,
    parse_spec_files,
)


class TestGenerateRequirementId:
    def test_format(self):
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
        f = tmp_path / "test.txt"
        _atomic_write(f, "hello")
        assert f.read_text() == "hello"

    def test_overwrites_existing(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("old")
        _atomic_write(f, "new")
        assert f.read_text() == "new"


class TestFindSpecSection:
    def test_single_section(self):
        content = "# Title\n\nSome content here."
        text, start, end = find_spec_section(content, "content")
        assert "Some content" in text

    def test_multiple_sections(self):
        content = "# Intro\n\nGeneral stuff.\n\n## Auth\n\nLogin tokens expire.\n\n## API\n\nREST endpoints."
        text, start, end = find_spec_section(content, "tokens expire login")
        assert "Login tokens" in text or "tokens expire" in text

    def test_empty_content(self):
        text, start, end = find_spec_section("", "anything")
        assert text == ""


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
        append_decision(initialized_repo, d)

        mock_updater = MagicMock(return_value="## Features\n\nThe system uses in-memory dict cache.\n")
        mock_parser_result = []

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", side_effect=[mock_updater.return_value, mock_parser_result]):
            result = sync_decisions(initialized_repo)

        assert result["spec_updated"] == 1
        # Decision should be marked synced
        decisions = read_decisions(initialized_repo)
        synced = [d for d in decisions if d.id == "dec-sync1" and d.synced_at]
        assert len(synced) == 1

    def test_skips_already_synced(self, initialized_repo):
        d = Decision(
            id="dec-sync2",
            status="approved",
            synced_at=datetime.now(timezone.utc).isoformat(),
        )
        append_decision(initialized_repo, d)
        result = sync_decisions(initialized_repo)
        assert result["spec_updated"] == 0

    def test_filters_by_decision_ids(self, initialized_repo):
        d1 = Decision(id="dec-yes", status="approved", decision="A")
        d2 = Decision(id="dec-no", status="approved", decision="B")
        append_decision(initialized_repo, d1)
        append_decision(initialized_repo, d2)

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value="updated"):
            result = sync_decisions(initialized_repo, decision_ids=["dec-yes"])

        decisions = read_decisions(initialized_repo)
        yes_synced = [d for d in decisions if d.id == "dec-yes" and d.synced_at]
        no_synced = [d for d in decisions if d.id == "dec-no" and d.synced_at]
        assert len(yes_synced) == 1
        assert len(no_synced) == 0


class TestParseSpecFiles:
    def test_parses_spec(self, initialized_repo):
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
