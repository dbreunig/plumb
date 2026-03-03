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
