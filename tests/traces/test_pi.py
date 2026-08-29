import json
import subprocess
from datetime import datetime, timezone

from plumb.traces import SessionRef
from plumb.traces.pi import PiSource


def _msg(id, parent, role, content, ts="2026-06-01T00:00:00Z", **extra):
    m = {"role": role, "content": content}
    m.update(extra)
    return {"type": "message", "id": id, "parentId": parent, "timestamp": ts, "message": m}


def _session(tmp_path, cwd, sid="abc", extra_header=None, lines=None):
    d = tmp_path / "sessions" / "--enc--"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"2026-06-01T00-00-00-000Z_{sid}.jsonl"
    header = {"type": "session", "version": 3, "id": sid, "timestamp": "2026-06-01T00:00:00Z", "cwd": cwd}
    header.update(extra_header or {})
    body = lines if lines is not None else [
        {"type": "model_change", "id": "m0", "parentId": None, "timestamp": "2026-06-01T00:00:00Z"},
        _msg("u1", "m0", "user", [{"type": "text", "text": "hello"}]),
        _msg("a1", "u1", "assistant", [{"type": "thinking", "thinking": "hmm"},
                                        {"type": "text", "text": "hi"},
                                        {"type": "toolCall", "id": "t1", "name": "read", "arguments": {"path": "a.py"}}]),
        _msg("r1", "a1", "toolResult", [{"type": "text", "text": "file body"}], toolCallId="t1", toolName="read"),
        # abandoned branch (rewound): child of u1 that nothing continues from
        _msg("dead", "u1", "assistant", [{"type": "text", "text": "WRONG BRANCH"}]),
        {"type": "compaction", "id": "c1", "parentId": "r1", "timestamp": "2026-06-01T00:00:00Z", "summary": "..."},
        _msg("u2", "c1", "user", [{"type": "text", "text": "thanks"}]),
    ]
    f.write_text("\n".join(json.dumps(l) for l in [header] + body) + "\n")
    return f


def test_discover_by_header_cwd(tmp_repo, tmp_path):
    f = _session(tmp_path, str(tmp_repo))
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    _session(tmp_path, str(other), sid="zzz")
    refs = PiSource(root=tmp_path / "sessions").discover(tmp_repo, None)
    assert [(r.agent, r.session_id, r.path, r.parent_session_id) for r in refs] == [("pi", "abc", str(f), None)]


def test_discover_branched_from_sets_parent(tmp_repo, tmp_path):
    _session(tmp_path, str(tmp_repo), sid="child", extra_header={"branchedFrom": "abc"})
    refs = {r.session_id: r for r in PiSource(root=tmp_path / "sessions").discover(tmp_repo, None)}
    assert refs["child"].parent_session_id == "abc"


def test_discover_subagent_gets_parent(tmp_repo, tmp_path):
    _session(tmp_path, str(tmp_repo))
    sub = tmp_path / "sessions" / "--enc--" / "2026-06-01T00-00-00-000Z_abc" / "worker.jsonl"
    sub.parent.mkdir()
    sub.write_text(json.dumps({"type": "session", "version": 3, "id": "w1", "cwd": str(tmp_repo)}) + "\n")
    refs = {r.session_id: r for r in PiSource(root=tmp_path / "sessions").discover(tmp_repo, None)}
    assert refs["w1"].parent_session_id == "abc"
    assert refs["abc"].parent_session_id is None


def test_discover_filters_by_mtime(tmp_repo, tmp_path):
    import os, time
    f = _session(tmp_path, str(tmp_repo))
    old = time.time() - 3600
    os.utime(f, (old, old))
    assert PiSource(root=tmp_path / "sessions").discover(tmp_repo, datetime.now(timezone.utc)) == []


def test_parse_walks_active_ancestry_only(tmp_repo, tmp_path):
    f = _session(tmp_path, str(tmp_repo))
    ref = SessionRef(agent="pi", session_id="abc", path=str(f), cwd=str(tmp_repo))
    turns = PiSource().parse(ref, None)
    assert [t.content for t in turns] == ["hello", "hi", "thanks"]
    assert "WRONG BRANCH" not in [t.content for t in turns]
    tc = turns[1].tool_calls[0]
    assert (tc.name, tc.category, tc.file_path, tc.result_summary) == ("read", "Read", "a.py", "file body")
    assert [t.ordinal for t in turns] == [0, 1, 2]


def test_parse_error_result_prefixed_and_since_keeps_ordinals(tmp_repo, tmp_path):
    lines = [
        {"type": "model_change", "id": "m0", "parentId": None, "timestamp": "2026-01-01T00:00:00Z"},
        _msg("u1", "m0", "user", [{"type": "text", "text": "old"}], ts="2026-01-01T00:00:00Z"),
        _msg("a1", "u1", "assistant", [{"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "false"}}],
             ts="2026-06-01T00:00:00Z"),
        _msg("r1", "a1", "toolResult", [{"type": "text", "text": "exit 1"}], toolCallId="t1", toolName="bash", isError=True,
             ts="2026-06-01T00:00:01Z"),
    ]
    f = _session(tmp_path, str(tmp_repo), lines=lines)
    ref = SessionRef(agent="pi", session_id="abc", path=str(f), cwd=str(tmp_repo))
    turns = PiSource().parse(ref, datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert [(t.role, t.ordinal) for t in turns] == [("assistant", 1)]
    assert turns[0].tool_calls[0].result_summary == "ERROR: exit 1"


def test_parse_tolerates_missing_header_and_garbage(tmp_repo, tmp_path):
    f = _session(tmp_path, str(tmp_repo), lines=[{"type": "message", "id": "x", "parentId": None, "message": "garbage"}])
    ref = SessionRef(agent="pi", session_id="abc", path=str(f), cwd=str(tmp_repo))
    assert PiSource().parse(ref, None) == []


def test_parse_parent_id_cycle_does_not_hang(tmp_repo, tmp_path):
    lines = [
        _msg("u1", "a1", "user", [{"type": "text", "text": "hello"}]),
        _msg("a1", "u1", "assistant", [{"type": "text", "text": "hi"}]),
    ]
    f = _session(tmp_path, str(tmp_repo), lines=lines)
    ref = SessionRef(agent="pi", session_id="abc", path=str(f), cwd=str(tmp_repo))
    assert [t.content for t in PiSource().parse(ref, None)] == ["hello", "hi"]


def test_all_sources_includes_pi():
    from plumb.traces import all_sources
    assert [s.name for s in all_sources()] == ["claude", "codex", "pi", "copilot"]


def test_parse_strips_assistant_text(tmp_repo, tmp_path):
    lines = [
        _msg("u1", None, "user", [{"type": "text", "text": "hello"}]),
        _msg("a1", "u1", "assistant", [{"type": "text", "text": "\n\nLet me look.\n\n"}]),
    ]
    f = _session(tmp_path, str(tmp_repo), lines=lines)
    ref = SessionRef(agent="pi", session_id="abc", path=str(f), cwd=str(tmp_repo))
    assert PiSource().parse(ref, None)[1].content == "Let me look."


def test_parse_compaction_keeps_pre_compaction_turns_and_never_emits_summary(tmp_repo, tmp_path):
    lines = [
        _msg("u1", None, "user", [{"type": "text", "text": "first"}]),
        _msg("a1", "u1", "assistant", [{"type": "text", "text": "one"}]),
        _msg("u2", "a1", "user", [{"type": "text", "text": "second"}]),
        _msg("a2", "u2", "assistant", [{"type": "text", "text": "two"}]),
        {"type": "compaction", "id": "c1", "parentId": "a2", "timestamp": "2026-06-01T00:00:00Z",
         "summary": "SUMMARY OF EARLIER WORK", "firstKeptEntryId": "u2"},
        _msg("u3", "c1", "user", [{"type": "text", "text": "third"}]),
    ]
    f = _session(tmp_path, str(tmp_repo), lines=lines)
    ref = SessionRef(agent="pi", session_id="abc", path=str(f), cwd=str(tmp_repo))
    turns = PiSource().parse(ref, None)
    assert [t.content for t in turns] == ["first", "one", "second", "two", "third"]
    assert [t.ordinal for t in turns] == [0, 1, 2, 3, 4]
    assert not any("SUMMARY OF EARLIER WORK" in t.content for t in turns)


def test_discover_subagent_yielded_before_parent(tmp_repo, tmp_path, monkeypatch):
    parent = _session(tmp_path, str(tmp_repo))
    sub = tmp_path / "sessions" / "--enc--" / "2026-06-01T00-00-00-000Z_abc" / "worker.jsonl"
    sub.parent.mkdir()
    sub.write_text(json.dumps({"type": "session", "version": 3, "id": "w1", "cwd": str(tmp_repo)}) + "\n")
    monkeypatch.setattr(PiSource, "_candidates", lambda self: iter([(sub, parent.stem), (parent, None)]))
    refs = {r.session_id: r for r in PiSource(root=tmp_path / "sessions").discover(tmp_repo, None)}
    assert refs["w1"].parent_session_id == "abc"
    assert refs["abc"].parent_session_id is None


def test_parse_header_only_file_returns_empty(tmp_repo, tmp_path):
    f = _session(tmp_path, str(tmp_repo), lines=[])
    ref = SessionRef(agent="pi", session_id="abc", path=str(f), cwd=str(tmp_repo))
    assert PiSource().parse(ref, None) == []
    assert PiSource()._active_chain(f) == []
