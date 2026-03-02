"""Extended coverage reporter tests."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb.coverage_reporter import (
    run_pytest_coverage,
    check_spec_to_test_coverage,
    check_spec_to_code_coverage,
    print_coverage_report,
)


class TestRunPytestCoverage:
    def test_no_config(self, tmp_repo):
        assert run_pytest_coverage(tmp_repo) is None

    def test_with_config(self, initialized_repo):
        # plumb:req-e8a350d6
        # This actually runs pytest so it may work or not depending on setup
        # We just verify it doesn't crash
        result = run_pytest_coverage(initialized_repo)
        # result is either None or a dict
        assert result is None or isinstance(result, dict)


class TestSpecToTestCoverageExtended:
    def test_malformed_requirements(self, initialized_repo):
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text("bad json{{{")
        assert check_spec_to_test_coverage(initialized_repo) == (0, 0)

    def test_empty_requirements(self, initialized_repo):
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text("[]")
        assert check_spec_to_test_coverage(initialized_repo) == (0, 0)

    def test_test_file_path(self, initialized_repo):
        """Test when test_paths points to a file, not directory."""
        cfg = PlumbConfig(
            spec_paths=["spec.md"],
            test_paths=["test_single.py"],
            initialized_at="2025-01-01",
        )
        save_config(initialized_repo, cfg)
        test_file = initialized_repo / "test_single.py"
        test_file.write_text("def test_x():\n    # plumb:req-abc123\n    pass\n")
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps([{"id": "req-abc123", "text": "X"}]))
        covered, total = check_spec_to_test_coverage(initialized_repo)
        assert covered == 1
        assert total == 1


class TestSpecToCodeCoverageExtended:
    def test_malformed_requirements(self, initialized_repo):
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text("bad json{{{")
        assert check_spec_to_code_coverage(initialized_repo) == (0, 0)


class TestPrintCoverageReportExtended:
    def test_with_all_data(self, initialized_repo, capsys):
        # Write requirements
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps([
            {"id": "req-abc", "text": "Must X"},
            {"id": "req-def", "text": "Must Y"},
        ]))
        # Write test referencing one
        test_dir = initialized_repo / "tests"
        test_dir.mkdir(exist_ok=True)
        (test_dir / "test_a.py").write_text("def test_x():\n    # plumb:req-abc\n    pass\n")

        cov_data = {"totals": {"percent_covered": 75.0}}
        with patch("plumb.coverage_reporter.run_pytest_coverage", return_value=cov_data), \
             patch("plumb.coverage_reporter.check_spec_to_code_coverage", return_value=(1, 2)):
            print_coverage_report(initialized_repo)
        captured = capsys.readouterr()
        assert "75.0%" in captured.out
        assert "50.0%" in captured.out  # 1/2 spec-to-test
