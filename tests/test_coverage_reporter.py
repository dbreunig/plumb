import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb.coverage_reporter import (
    _get_code_coverage_pct,
    _extract_test_req_ids,
    check_spec_to_test_coverage,
    check_spec_to_code_coverage,
    print_coverage_report,
    _compute_cache_key,
)


class TestGetCodeCoveragePct:
    def test_valid_data(self):
        # plumb:req-e8a350d6
        data = {"totals": {"percent_covered": 85.5}}
        assert _get_code_coverage_pct(data) == 85.5

    def test_none_data(self):
        assert _get_code_coverage_pct(None) is None

    def test_missing_key(self):
        assert _get_code_coverage_pct({"other": 1}) is None


class TestExtractTestReqIds:
    def test_marker_comment(self):
        # plumb:req-127f5115
        content = """\
def test_something():
    # plumb:req-abc12345
    assert True
"""
        assert _extract_test_req_ids(content) == {"req-abc12345"}

    def test_marker_with_spaces(self):
        content = "#  plumb:req-00112233\n"
        assert _extract_test_req_ids(content) == {"req-00112233"}

    def test_function_name_fallback(self):
        content = "def test_req_aabbccdd_does_something():\n    pass\n"
        assert _extract_test_req_ids(content) == {"req-aabbccdd"}

    def test_both_formats_no_double_count(self):
        content = """\
def test_req_abc12345_something():
    # plumb:req-abc12345
    assert True
"""
        ids = _extract_test_req_ids(content)
        assert ids == {"req-abc12345"}

    def test_multiple_markers_in_one_function(self):
        content = """\
def test_multi():
    # plumb:req-aaaaaaaa
    # plumb:req-bbbbbbbb
    assert True
"""
        ids = _extract_test_req_ids(content)
        assert ids == {"req-aaaaaaaa", "req-bbbbbbbb"}

    def test_no_markers(self):
        content = "def test_plain():\n    assert True\n"
        assert _extract_test_req_ids(content) == set()

    def test_mixed_formats(self):
        content = """\
def test_req_11111111_foo():
    pass

def test_bar():
    # plumb:req-22222222
    pass
"""
        ids = _extract_test_req_ids(content)
        assert ids == {"req-11111111", "req-22222222"}


class TestSpecToTestCoverage:
    def test_no_config(self, tmp_repo):
        assert check_spec_to_test_coverage(tmp_repo) == (0, 0)

    def test_no_requirements(self, initialized_repo):
        assert check_spec_to_test_coverage(initialized_repo) == (0, 0)

    def test_marker_based_detection(self, initialized_repo):
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
            {"id": "req-def45678", "text": "Must do Y"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        test_dir = initialized_repo / "tests"
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "test_something.py"
        test_file.write_text("""\
def test_feature_x():
    # plumb:req-abc12345
    assert True
""")

        covered, total = check_spec_to_test_coverage(initialized_repo)
        assert total == 2
        assert covered == 1

    def test_function_name_detection(self, initialized_repo):
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        test_dir = initialized_repo / "tests"
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "test_something.py"
        test_file.write_text("def test_req_abc12345_does_x():\n    pass\n")

        covered, total = check_spec_to_test_coverage(initialized_repo)
        assert total == 1
        assert covered == 1

    def test_no_double_counting(self, initialized_repo):
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        test_dir = initialized_repo / "tests"
        test_dir.mkdir(exist_ok=True)
        # Both marker and function name for same requirement
        test_file = test_dir / "test_something.py"
        test_file.write_text("""\
def test_req_abc12345_does_x():
    # plumb:req-abc12345
    pass
""")

        covered, total = check_spec_to_test_coverage(initialized_repo)
        assert total == 1
        assert covered == 1


class TestSpecToCodeCoverage:
    def test_no_config(self, tmp_repo):
        assert check_spec_to_code_coverage(tmp_repo) == (0, 0)

    def test_no_requirements(self, initialized_repo):
        assert check_spec_to_code_coverage(initialized_repo) == (0, 0)

    def test_use_llm_false_returns_zero_on_cache_miss(self, initialized_repo):
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        covered, total = check_spec_to_code_coverage(initialized_repo, use_llm=False)
        assert total == 1
        assert covered == 0  # cache miss, no LLM

    def test_cache_hit(self, initialized_repo):
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        # Build the cache key from the same data the function would use
        from plumb.coverage_reporter import _collect_source_summaries
        source_summaries = _collect_source_summaries(initialized_repo)
        cache_key = _compute_cache_key(reqs, source_summaries)

        # Pre-populate cache
        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        cache_data = {
            "cache_key": cache_key,
            "covered": 1,
            "total": 1,
            "implemented_ids": ["req-abc12345"],
        }
        cache_path.write_text(json.dumps(cache_data))

        covered, total = check_spec_to_code_coverage(initialized_repo, use_llm=False)
        assert covered == 1
        assert total == 1

    def test_cache_invalidation(self, initialized_repo):
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        # Write a stale cache
        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        cache_data = {
            "cache_key": "stale-key",
            "covered": 1,
            "total": 1,
            "implemented_ids": ["req-abc12345"],
        }
        cache_path.write_text(json.dumps(cache_data))

        # With use_llm=False, stale cache means cache miss → returns 0
        covered, total = check_spec_to_code_coverage(initialized_repo, use_llm=False)
        assert covered == 0
        assert total == 1

    def test_use_llm_true_calls_mapper(self, initialized_repo):
        # plumb:req-3ec563d5
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
            {"id": "req-def45678", "text": "Must do Y"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        # Create a source file so summaries aren't empty
        src = initialized_repo / "src"
        src.mkdir()
        (src / "main.py").write_text("def do_x():\n    '''Does X'''\n    pass\n")

        from plumb.programs.code_coverage_mapper import RequirementCoverage
        mock_results = [
            RequirementCoverage(
                requirement_id="req-abc12345",
                implemented=True,
                evidence="src/main.py:do_x",
            ),
            RequirementCoverage(
                requirement_id="req-def45678",
                implemented=False,
                evidence="",
            ),
        ]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=mock_results):
            covered, total = check_spec_to_code_coverage(
                initialized_repo, use_llm=True
            )

        assert covered == 1
        assert total == 2

        # Verify cache was written
        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        assert cache_path.exists()
        cache = json.loads(cache_path.read_text())
        assert cache["covered"] == 1
        assert cache["total"] == 2

    def test_use_llm_false_never_triggers_llm(self, initialized_repo):
        reqs = [{"id": "req-abc12345", "text": "Must do X"}]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        with patch("plumb.programs.configure_dspy") as mock_configure:
            check_spec_to_code_coverage(initialized_repo, use_llm=False)

        mock_configure.assert_not_called()


class TestPrintCoverageReport:
    def test_prints_without_error(self, initialized_repo, capsys):
        # plumb:req-a9b444e0
        with patch("plumb.coverage_reporter.run_pytest_coverage", return_value=None), \
             patch("plumb.coverage_reporter.check_spec_to_code_coverage", return_value=(0, 0)):
            print_coverage_report(initialized_repo)
        captured = capsys.readouterr()
        assert "Coverage" in captured.out or "N/A" in captured.out
