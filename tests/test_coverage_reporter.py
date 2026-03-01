import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb.coverage_reporter import (
    _get_code_coverage_pct,
    check_spec_to_test_coverage,
    check_spec_to_code_coverage,
    print_coverage_report,
)


class TestGetCodeCoveragePct:
    def test_valid_data(self):
        data = {"totals": {"percent_covered": 85.5}}
        assert _get_code_coverage_pct(data) == 85.5

    def test_none_data(self):
        assert _get_code_coverage_pct(None) is None

    def test_missing_key(self):
        assert _get_code_coverage_pct({"other": 1}) is None


class TestSpecToTestCoverage:
    def test_no_config(self, tmp_repo):
        assert check_spec_to_test_coverage(tmp_repo) == (0, 0)

    def test_no_requirements(self, initialized_repo):
        assert check_spec_to_test_coverage(initialized_repo) == (0, 0)

    def test_with_requirements_and_tests(self, initialized_repo):
        # Write requirements
        reqs = [
            {"id": "req-abc123", "text": "Must do X"},
            {"id": "req-def456", "text": "Must do Y"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        # Write a test file referencing one requirement
        test_dir = initialized_repo / "tests"
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "test_something.py"
        test_file.write_text("# req-abc123\ndef test_x(): pass\n")

        covered, total = check_spec_to_test_coverage(initialized_repo)
        assert total == 2
        assert covered == 1


class TestSpecToCodeCoverage:
    def test_no_config(self, tmp_repo):
        assert check_spec_to_code_coverage(tmp_repo) == (0, 0)

    def test_with_requirements_and_code(self, initialized_repo):
        reqs = [
            {"id": "req-abc123", "text": "Must do X"},
            {"id": "req-def456", "text": "Must do Y"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        # Write source with one requirement referenced
        src = initialized_repo / "src"
        src.mkdir()
        (src / "main.py").write_text("# req-abc123\ndef do_x(): pass\n")

        covered, total = check_spec_to_code_coverage(initialized_repo)
        assert total == 2
        assert covered == 1


class TestPrintCoverageReport:
    def test_prints_without_error(self, initialized_repo, capsys):
        with patch("plumb.coverage_reporter.run_pytest_coverage", return_value=None):
            print_coverage_report(initialized_repo)
        # Should not raise and should produce output
        captured = capsys.readouterr()
        assert "Coverage" in captured.out or "N/A" in captured.out
