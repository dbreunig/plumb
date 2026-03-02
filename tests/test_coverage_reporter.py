import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb.coverage_reporter import (
    _get_code_coverage_pct,
    _extract_test_req_ids,
    _extract_source_files_from_evidence,
    _compute_per_file_hashes,
    _compute_requirements_hash,
    _collect_source_summaries,
    _combine_summaries,
    check_spec_to_test_coverage,
    check_spec_to_code_coverage,
    print_coverage_report,
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


def _build_v2_cache(file_hashes, req_hash, results):
    """Helper to build a v2 cache dict."""
    return {
        "version": 2,
        "source_hashes": file_hashes,
        "requirements_hash": req_hash,
        "results": results,
    }


class TestEvidenceSourceFileExtraction:
    def test_single_file_match(self):
        evidence = "src/main.py:do_x implements the feature"
        known = {"src/main.py", "src/utils.py"}
        assert _extract_source_files_from_evidence(evidence, known) == ["src/main.py"]

    def test_multiple_file_matches(self):
        evidence = "src/main.py:do_x and src/utils.py:helper"
        known = {"src/main.py", "src/utils.py", "src/other.py"}
        assert _extract_source_files_from_evidence(evidence, known) == [
            "src/main.py", "src/utils.py",
        ]

    def test_no_match(self):
        evidence = "some general evidence"
        known = {"src/main.py"}
        assert _extract_source_files_from_evidence(evidence, known) == []

    def test_empty_evidence(self):
        assert _extract_source_files_from_evidence("", {"src/main.py"}) == []

    def test_empty_known_files(self):
        assert _extract_source_files_from_evidence("src/main.py", set()) == []


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

    def test_cache_hit_v2(self, initialized_repo):
        """V2 cache with matching hashes returns cached results without LLM."""
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        # Create a source file
        src = initialized_repo / "src"
        src.mkdir()
        (src / "main.py").write_text("def do_x():\n    '''Does X'''\n    pass\n")

        per_file = _collect_source_summaries(initialized_repo)
        file_hashes = _compute_per_file_hashes(per_file)
        req_hash = _compute_requirements_hash(reqs)

        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        cache_data = _build_v2_cache(file_hashes, req_hash, {
            "req-abc12345": {
                "implemented": True,
                "evidence": "src/main.py:do_x",
                "source_files": ["src/main.py"],
            },
        })
        cache_path.write_text(json.dumps(cache_data))

        covered, total = check_spec_to_code_coverage(initialized_repo, use_llm=False)
        assert covered == 1
        assert total == 1

    def test_cache_invalidation_stale_v1(self, initialized_repo):
        """A v1 cache (no version key) should be treated as a miss."""
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        cache_data = {
            "cache_key": "stale-key",
            "covered": 1,
            "total": 1,
            "implemented_ids": ["req-abc12345"],
        }
        cache_path.write_text(json.dumps(cache_data))

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

        # Verify v2 cache was written
        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        assert cache_path.exists()
        cache = json.loads(cache_path.read_text())
        assert cache["version"] == 2
        assert "req-abc12345" in cache["results"]
        assert cache["results"]["req-abc12345"]["implemented"] is True
        assert cache["results"]["req-def45678"]["implemented"] is False

    def test_use_llm_false_never_triggers_llm(self, initialized_repo):
        reqs = [{"id": "req-abc12345", "text": "Must do X"}]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        with patch("plumb.programs.configure_dspy") as mock_configure:
            check_spec_to_code_coverage(initialized_repo, use_llm=False)

        mock_configure.assert_not_called()

    def test_no_file_changes_skips_llm(self, initialized_repo):
        """When file hashes match the v2 cache, no LLM call is made."""
        reqs = [
            {"id": "req-abc12345", "text": "Must do X"},
            {"id": "req-def45678", "text": "Must do Y"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        src = initialized_repo / "src"
        src.mkdir()
        (src / "main.py").write_text("def do_x():\n    '''Does X'''\n    pass\n")

        per_file = _collect_source_summaries(initialized_repo)
        file_hashes = _compute_per_file_hashes(per_file)
        req_hash = _compute_requirements_hash(reqs)

        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        cache_data = _build_v2_cache(file_hashes, req_hash, {
            "req-abc12345": {
                "implemented": True,
                "evidence": "src/main.py:do_x",
                "source_files": ["src/main.py"],
            },
            "req-def45678": {
                "implemented": False,
                "evidence": "",
                "source_files": [],
            },
        })
        cache_path.write_text(json.dumps(cache_data))

        with patch("plumb.programs.configure_dspy") as mock_configure:
            covered, total = check_spec_to_code_coverage(
                initialized_repo, use_llm=True,
            )

        # No LLM call should have been made
        mock_configure.assert_not_called()
        assert covered == 1
        assert total == 2

    def test_incremental_cache_only_remaps_dirty(self, initialized_repo):
        """When one source file changes, only dirty reqs are sent to the mapper."""
        reqs = [
            {"id": "req-aaa11111", "text": "Feature A"},
            {"id": "req-bbb22222", "text": "Feature B"},
            {"id": "req-ccc33333", "text": "Feature C"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        src = initialized_repo / "src"
        src.mkdir()
        (src / "alpha.py").write_text("def alpha():\n    '''Alpha'''\n    pass\n")
        (src / "beta.py").write_text("def beta():\n    '''Beta'''\n    pass\n")

        per_file = _collect_source_summaries(initialized_repo)
        file_hashes = _compute_per_file_hashes(per_file)
        req_hash = _compute_requirements_hash(reqs)

        # Pre-populate v2 cache: A mapped to alpha.py, B mapped to beta.py,
        # C is unimplemented
        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        cache_data = _build_v2_cache(file_hashes, req_hash, {
            "req-aaa11111": {
                "implemented": True,
                "evidence": "src/alpha.py:alpha",
                "source_files": ["src/alpha.py"],
            },
            "req-bbb22222": {
                "implemented": True,
                "evidence": "src/beta.py:beta",
                "source_files": ["src/beta.py"],
            },
            "req-ccc33333": {
                "implemented": False,
                "evidence": "",
                "source_files": [],
            },
        })
        cache_path.write_text(json.dumps(cache_data))

        # Now change only alpha.py
        (src / "alpha.py").write_text("def alpha_v2():\n    '''Alpha v2'''\n    pass\n")

        from plumb.programs.code_coverage_mapper import RequirementCoverage
        mock_results = [
            RequirementCoverage(
                requirement_id="req-aaa11111",
                implemented=True,
                evidence="src/alpha.py:alpha_v2",
            ),
            RequirementCoverage(
                requirement_id="req-ccc33333",
                implemented=False,
                evidence="",
            ),
        ]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=mock_results) as mock_run:
            covered, total = check_spec_to_code_coverage(
                initialized_repo, use_llm=True,
            )

        # Mapper should have been called with only the dirty reqs (A + C),
        # NOT the clean req B
        assert mock_run.call_count == 1
        call_args = mock_run.call_args
        req_json_sent = json.loads(call_args[0][1])
        sent_ids = {r["id"] for r in req_json_sent}
        assert sent_ids == {"req-aaa11111", "req-ccc33333"}
        assert "req-bbb22222" not in sent_ids

        # B should still be implemented (cached), A updated, C still not
        assert covered == 2
        assert total == 3

    def test_unimplemented_always_rechecked(self, initialized_repo):
        """Unimplemented requirements are always marked dirty when files change."""
        reqs = [
            {"id": "req-aaa11111", "text": "Feature A"},
            {"id": "req-bbb22222", "text": "Feature B (not yet)"},
        ]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        src = initialized_repo / "src"
        src.mkdir()
        (src / "main.py").write_text("def foo():\n    '''Foo'''\n    pass\n")

        per_file = _collect_source_summaries(initialized_repo)
        file_hashes = _compute_per_file_hashes(per_file)
        req_hash = _compute_requirements_hash(reqs)

        # Cache: A implemented, B not implemented
        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        cache_data = _build_v2_cache(file_hashes, req_hash, {
            "req-aaa11111": {
                "implemented": True,
                "evidence": "src/main.py:foo",
                "source_files": ["src/main.py"],
            },
            "req-bbb22222": {
                "implemented": False,
                "evidence": "",
                "source_files": [],
            },
        })
        cache_path.write_text(json.dumps(cache_data))

        # Change main.py — both A (mapped to it) and B (unimplemented) are dirty
        (src / "main.py").write_text(
            "def foo():\n    '''Foo'''\n    pass\n"
            "def bar():\n    '''Bar'''\n    pass\n"
        )

        from plumb.programs.code_coverage_mapper import RequirementCoverage
        mock_results = [
            RequirementCoverage(
                requirement_id="req-aaa11111",
                implemented=True,
                evidence="src/main.py:foo",
            ),
            RequirementCoverage(
                requirement_id="req-bbb22222",
                implemented=True,
                evidence="src/main.py:bar",
            ),
        ]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=mock_results) as mock_run:
            covered, total = check_spec_to_code_coverage(
                initialized_repo, use_llm=True,
            )

        req_json_sent = json.loads(mock_run.call_args[0][1])
        sent_ids = {r["id"] for r in req_json_sent}
        assert "req-bbb22222" in sent_ids  # unimplemented was rechecked
        assert covered == 2
        assert total == 2

    def test_legacy_v1_cache_triggers_full_remap(self, initialized_repo):
        """A v1 cache (missing version key) triggers a full re-map."""
        reqs = [{"id": "req-abc12345", "text": "Must do X"}]
        req_path = initialized_repo / ".plumb" / "requirements.json"
        req_path.write_text(json.dumps(reqs))

        src = initialized_repo / "src"
        src.mkdir()
        (src / "main.py").write_text("def do_x():\n    '''Does X'''\n    pass\n")

        # Write a v1-style cache
        cache_path = initialized_repo / ".plumb" / "code_coverage_map.json"
        cache_data = {
            "cache_key": "some-old-key",
            "covered": 1,
            "total": 1,
            "implemented_ids": ["req-abc12345"],
        }
        cache_path.write_text(json.dumps(cache_data))

        from plumb.programs.code_coverage_mapper import RequirementCoverage
        mock_results = [
            RequirementCoverage(
                requirement_id="req-abc12345",
                implemented=True,
                evidence="src/main.py:do_x",
            ),
        ]

        with patch("plumb.programs.configure_dspy"), \
             patch("plumb.programs.run_with_retries", return_value=mock_results) as mock_run:
            covered, total = check_spec_to_code_coverage(
                initialized_repo, use_llm=True,
            )

        # Full re-map should have been called (all requirements)
        assert mock_run.call_count == 1
        req_json_sent = json.loads(mock_run.call_args[0][1])
        assert len(req_json_sent) == 1

        # Cache should now be v2
        new_cache = json.loads(cache_path.read_text())
        assert new_cache["version"] == 2
        assert "results" in new_cache
        assert covered == 1
        assert total == 1


class TestPrintCoverageReport:
    def test_prints_without_error(self, initialized_repo, capsys):
        # plumb:req-a9b444e0
        with patch("plumb.coverage_reporter.run_pytest_coverage", return_value=None), \
             patch("plumb.coverage_reporter.check_spec_to_code_coverage", return_value=(0, 0)):
            print_coverage_report(initialized_repo)
        captured = capsys.readouterr()
        assert "Coverage" in captured.out or "N/A" in captured.out
