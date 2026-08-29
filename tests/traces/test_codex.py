import json
import subprocess
from datetime import datetime, timezone

from plumb.traces import SessionRef
from plumb.traces.codex import CodexSource


def _line(kind, payload, ts="2026-06-01T00:00:00Z"):
    return {"timestamp": ts, "type": kind, "payload": payload}


def _rollout(tmp_path, cwd, sid="01a0"):
    d = tmp_path / "sessions" / "2026" / "06" / "01"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-2026-06-01T00-00-00-{sid}.jsonl"
    lines = [
        _line("session_meta", {"id": sid, "cwd": cwd}),
        _line("turn_context", {"cwd": cwd, "git": {"branch": "feat"}}),
        _line("event_msg", {"type": "user_message", "message": "add x"}),
        _line("response_item", {"type": "message", "role": "developer",
                                "content": [{"type": "input_text", "text": "ctx"}]}),
        _line("response_item", {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "add x"}]}),
        _line("response_item", {"type": "reasoning", "summary": []}),
        _line("response_item", {"type": "function_call", "name": "shell_command", "call_id": "c1",
                                "arguments": json.dumps({"command": "pytest -x"})}),
        _line("response_item", {"type": "function_call_output", "call_id": "c1", "output": "3 passed"}),
        _line("event_msg", {"type": "agent_message", "message": "Patching."}),
        _line("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2",
                                "input": "*** Begin Patch\n*** Update File: src/x.py\n@@\n+1\n"
                                         "*** Add File: src/y.py\n+2\n*** End Patch"}),
        _line("response_item", {"type": "custom_tool_call_output", "call_id": "c2",
                                "output": [{"type": "input_text", "text": "Done"}]}),
        _line("event_msg", {"type": "token_count", "info": {}}),
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return f


def test_discover_by_session_meta_cwd(tmp_repo, tmp_path):
    f = _rollout(tmp_path, str(tmp_repo))
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    _rollout(tmp_path, str(other), sid="zz")
    refs = CodexSource(root=tmp_path / "sessions").discover(tmp_repo, None)
    assert [(r.session_id, r.path, r.branch, r.agent) for r in refs] == [("01a0", str(f), "feat", "codex")]


def test_discover_filters_by_mtime(tmp_repo, tmp_path):
    import os, time
    f = _rollout(tmp_path, str(tmp_repo))
    old = time.time() - 3600
    os.utime(f, (old, old))
    assert CodexSource(root=tmp_path / "sessions").discover(tmp_repo, datetime.now(timezone.utc)) == []


def test_parse_codex_turns(tmp_repo, tmp_path):
    f = _rollout(tmp_path, str(tmp_repo))
    ref = SessionRef(agent="codex", session_id="01a0", path=str(f), cwd=str(tmp_repo))
    turns = CodexSource().parse(ref, None)
    assert [t.role for t in turns] == ["user", "assistant", "assistant"]
    assert [t.ordinal for t in turns] == [0, 1, 2]
    assert turns[0].content == "add x"  # from event_msg, not duplicated by response_item/message
    first = turns[1].tool_calls[0]
    assert (first.name, first.category, first.input_summary, first.result_summary) == \
        ("shell_command", "Bash", "pytest -x", "3 passed")
    assert turns[1].content == ""
    assert turns[2].content == "Patching."
    patch = turns[2].tool_calls[0]
    assert (patch.category, patch.file_path, patch.file_paths, patch.result_summary) == \
        ("Edit", "src/x.py", ["src/x.py", "src/y.py"], "Done")


def test_parse_since_keeps_ordinals(tmp_repo, tmp_path):
    f = _rollout(tmp_path, str(tmp_repo))
    # rewrite with two user messages at different times
    lines = [
        _line("session_meta", {"id": "01a0", "cwd": str(tmp_repo)}),
        _line("event_msg", {"type": "user_message", "message": "old"}, ts="2026-01-01T00:00:00Z"),
        _line("event_msg", {"type": "user_message", "message": "new"}, ts="2026-06-01T00:00:00Z"),
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    ref = SessionRef(agent="codex", session_id="01a0", path=str(f), cwd=str(tmp_repo))
    turns = CodexSource().parse(ref, datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert [(t.content, t.ordinal) for t in turns] == [("new", 1)]


def test_all_sources_includes_codex():
    from plumb.traces import all_sources
    assert [s.name for s in all_sources()] == ["claude", "codex"]


def test_discover_subagent_parent_link(tmp_repo, tmp_path):
    f = _rollout(tmp_path, str(tmp_repo), sid="sub1")
    lines = [
        _line("session_meta", {"id": "sub1", "cwd": str(tmp_repo), "thread_source": "subagent",
                               "parent_thread_id": "01a0"}),
        _line("event_msg", {"type": "user_message", "message": "subtask"}),
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    refs = CodexSource(root=tmp_path / "sessions").discover(tmp_repo, None)
    assert [(r.session_id, r.parent_session_id) for r in refs] == [("sub1", "01a0")]


def test_discover_branch_falls_back_to_session_meta(tmp_repo, tmp_path):
    f = _rollout(tmp_path, str(tmp_repo))
    lines = [
        _line("session_meta", {"id": "01a0", "cwd": str(tmp_repo), "git": {"branch": "meta-br"}}),
        _line("event_msg", {"type": "user_message", "message": "hi"}),
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    refs = CodexSource(root=tmp_path / "sessions").discover(tmp_repo, None)
    assert [r.branch for r in refs] == ["meta-br"]


def test_discover_includes_archived_root(tmp_repo, tmp_path):
    archived = tmp_path / "archived_sessions"
    archived.mkdir()
    f = archived / "rollout-2026-06-01T00-00-00-arch.jsonl"
    lines = [
        _line("session_meta", {"id": "arch", "cwd": str(tmp_repo)}),
        _line("event_msg", {"type": "user_message", "message": "hi"}),
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    refs = CodexSource(root=tmp_path / "sessions", archived=archived).discover(tmp_repo, None)
    assert [(r.session_id, r.path) for r in refs] == [("arch", str(f))]


def test_parse_non_json_function_call_arguments(tmp_repo, tmp_path):
    f = _rollout(tmp_path, str(tmp_repo))
    lines = [
        _line("session_meta", {"id": "01a0", "cwd": str(tmp_repo)}),
        _line("response_item", {"type": "function_call", "name": "shell_command", "call_id": "c1",
                                "arguments": "not json"}),
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    ref = SessionRef(agent="codex", session_id="01a0", path=str(f), cwd=str(tmp_repo))
    turns = CodexSource().parse(ref, None)
    tc = turns[0].tool_calls[0]
    assert (tc.name, tc.input_summary, tc.file_path) == ("shell_command", "not json", None)
