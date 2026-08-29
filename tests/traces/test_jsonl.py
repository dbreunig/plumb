import json
from plumb.traces.jsonl import iter_jsonl, sniff_head


def test_iter_jsonl_skips_bad_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"a":1}\nnot json\n\n{"a":2}\n')
    assert [e["a"] for e in iter_jsonl(p)] == [1, 2]


def test_iter_jsonl_missing_file(tmp_path):
    assert list(iter_jsonl(tmp_path / "nope.jsonl")) == []


def test_sniff_head_returns_first_match(tmp_path):
    p = tmp_path / "s.jsonl"
    lines = [{"type": "meta"}, {"type": "user", "cwd": "/a"}, {"type": "user", "cwd": "/b"}]
    p.write_text("\n".join(json.dumps(l) for l in lines))
    assert sniff_head(p, lambda e: e.get("cwd")) == "/a"


def test_sniff_head_respects_max_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps({"i": i}) for i in range(100)))
    assert sniff_head(p, lambda e: e["i"] if e["i"] > 50 else None, max_lines=10) is None
