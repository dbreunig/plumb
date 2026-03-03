from unittest.mock import MagicMock, patch

from plumb.programs import estimate_tokens, chunk_items, run_chunked_mapper


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_short_string(self):
        assert estimate_tokens("hello world") == len("hello world") // 4

    def test_longer_string(self):
        text = "a" * 400
        assert estimate_tokens(text) == 100


class TestChunkItems:
    def test_single_item_under_budget(self):
        items = [("a.py", "short text")]
        chunks = chunk_items(items, budget=1000)
        assert len(chunks) == 1
        assert chunks[0] == items

    def test_two_items_fit_one_chunk(self):
        items = [("a.py", "x" * 40), ("b.py", "y" * 40)]
        chunks = chunk_items(items, budget=100)
        assert len(chunks) == 1

    def test_items_split_across_chunks(self):
        items = [("a.py", "x" * 400), ("b.py", "y" * 400), ("c.py", "z" * 400)]
        chunks = chunk_items(items, budget=150)
        assert len(chunks) == 3
        assert chunks[0] == [items[0]]
        assert chunks[1] == [items[1]]
        assert chunks[2] == [items[2]]

    def test_oversized_item_gets_own_chunk(self):
        items = [("big.py", "x" * 4000), ("small.py", "y" * 40)]
        chunks = chunk_items(items, budget=500)
        assert len(chunks) == 2
        assert chunks[0] == [items[0]]
        assert chunks[1] == [items[1]]

    def test_empty_items(self):
        chunks = chunk_items([], budget=1000)
        assert chunks == []

    def test_greedy_packing(self):
        items = [("a.py", "x" * 80), ("b.py", "y" * 80), ("c.py", "z" * 80)]
        chunks = chunk_items(items, budget=50)
        assert len(chunks) == 2
        assert chunks[0] == [items[0], items[1]]
        assert chunks[1] == [items[2]]


class TestRunChunkedMapper:
    def test_single_chunk_no_threading(self):
        mapper = MagicMock()
        combine_fn = MagicMock(return_value="combined_text")
        req_json = '{"reqs": "short"}'
        items = [("a.py", "x" * 40)]

        with patch("plumb.programs.run_with_retries", return_value=["result_a", "result_b"]) as mock_retry:
            results = run_chunked_mapper(
                mapper, req_json, items, budget=60000,
                combine_fn=combine_fn,
            )

        assert results == ["result_a", "result_b"]
        mock_retry.assert_called_once()
        combine_fn.assert_called_once()

    def test_multiple_chunks_concurrent(self):
        mapper = MagicMock()
        call_results = [["r1"], ["r2"]]

        combine_fn = lambda chunk: "\n".join(t for _, t in chunk)
        req_json = "reqs"
        items = [("a.py", "x" * 400), ("b.py", "y" * 400)]

        with patch("plumb.programs.run_with_retries", side_effect=call_results):
            results = run_chunked_mapper(
                mapper, req_json, items, budget=200,
                combine_fn=combine_fn,
            )

        assert sorted(results) == ["r1", "r2"]

    def test_merge_fn_called(self):
        mapper = MagicMock()
        call_results = [["a", "b"], ["c"]]

        combine_fn = lambda chunk: "text"
        merge_fn = MagicMock(return_value=["merged"])
        req_json = "reqs"
        items = [("a.py", "x" * 400), ("b.py", "y" * 400)]

        with patch("plumb.programs.run_with_retries", side_effect=call_results):
            results = run_chunked_mapper(
                mapper, req_json, items, budget=200,
                combine_fn=combine_fn, merge_fn=merge_fn,
            )

        assert results == ["merged"]
        merge_fn.assert_called_once()
        args = merge_fn.call_args[0][0]
        assert args == [["a", "b"], ["c"]]

    def test_empty_items_returns_empty(self):
        mapper = MagicMock()
        results = run_chunked_mapper(
            mapper, "reqs", [], budget=60000,
            combine_fn=lambda c: "",
        )
        assert results == []


from plumb.coverage_reporter import merge_coverage_results
from plumb.programs.code_coverage_mapper import RequirementCoverage


class TestMergeCoverageResults:
    def test_or_semantics(self):
        """implemented=True in any chunk wins."""
        chunk1 = [
            RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py:foo"),
            RequirementCoverage(requirement_id="req-bbb", implemented=False, evidence=""),
        ]
        chunk2 = [
            RequirementCoverage(requirement_id="req-aaa", implemented=False, evidence=""),
            RequirementCoverage(requirement_id="req-bbb", implemented=True, evidence="b.py:bar"),
        ]
        merged = merge_coverage_results([chunk1, chunk2])

        by_id = {r.requirement_id: r for r in merged}
        assert by_id["req-aaa"].implemented is True
        assert by_id["req-aaa"].evidence == "a.py:foo"
        assert by_id["req-bbb"].implemented is True
        assert by_id["req-bbb"].evidence == "b.py:bar"

    def test_false_in_all_stays_false(self):
        chunk1 = [RequirementCoverage(requirement_id="req-aaa", implemented=False, evidence="")]
        chunk2 = [RequirementCoverage(requirement_id="req-aaa", implemented=False, evidence="")]
        merged = merge_coverage_results([chunk1, chunk2])
        assert len(merged) == 1
        assert merged[0].implemented is False

    def test_evidence_dedup(self):
        """Same evidence from multiple chunks is not duplicated."""
        chunk1 = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py:foo")]
        chunk2 = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py:foo")]
        merged = merge_coverage_results([chunk1, chunk2])
        assert merged[0].evidence == "a.py:foo"

    def test_evidence_joined(self):
        """Different evidence strings are joined."""
        chunk1 = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py:foo")]
        chunk2 = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="b.py:bar")]
        merged = merge_coverage_results([chunk1, chunk2])
        assert "a.py:foo" in merged[0].evidence
        assert "b.py:bar" in merged[0].evidence

    def test_single_chunk_passthrough(self):
        chunk = [RequirementCoverage(requirement_id="req-aaa", implemented=True, evidence="a.py")]
        merged = merge_coverage_results([chunk])
        assert merged == chunk


import json as json_mod


class TestTestMapperChunking:
    def test_test_summaries_combine_fn(self):
        """Verify the combine_fn produces valid JSON array from chunked items."""
        test_summaries = [
            {"file": "tests/test_a.py", "name": "test_foo", "docstring": "Tests foo", "preview": "def test_foo(): ..."},
            {"file": "tests/test_b.py", "name": "test_bar", "docstring": "Tests bar", "preview": "def test_bar(): ..."},
        ]

        items = [(s["name"], json_mod.dumps(s)) for s in test_summaries]
        combine_fn = lambda chunk: json_mod.dumps([json_mod.loads(t) for _, t in chunk])
        result = combine_fn(items)
        parsed = json_mod.loads(result)
        assert len(parsed) == 2
        assert parsed[0]["name"] == "test_foo"
        assert parsed[1]["name"] == "test_bar"
