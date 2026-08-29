import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from plumb.traces import SessionRef
from plumb.traces.claude import ClaudeSource

TS = "2026-06-01T00:00:0{}Z"


def _entry(kind, cwd, session="S1", ts=TS.format(0), **extra):
    base = {"type": kind, "cwd": cwd, "gitBranch": "main", "sessionId": session,
            "timestamp": ts, "isSidechain": False, "isMeta": False}
    base.update(extra)
    return base


def _write(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _project(tmp_path, repo, session="S1", entries=None):
    proj = tmp_path / "projects" / "-whatever-encoding"
    f = proj / f"{session}.jsonl"
    _write(f, entries or [_entry("user", str(repo), session, message={"role": "user", "content": "hi"})])
    return f


def test_discover_matches_by_cwd_not_dirname(tmp_repo, tmp_path):
    f = _project(tmp_path, tmp_repo)
    src = ClaudeSource(root=tmp_path / "projects")
    refs = src.discover(tmp_repo, since=None)
    assert [r.path for r in refs] == [str(f)]
    assert refs[0].session_id == "S1"
    assert refs[0].branch == "main"
    assert refs[0].agent == "claude"


def test_discover_skips_other_repos(tmp_repo, tmp_path):
    # tmp_repo *is* tmp_path, so "elsewhere" must be its own git repo to be another repo.
    other = tmp_path / "elsewhere"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    _project(tmp_path, other)
    assert ClaudeSource(root=tmp_path / "projects").discover(tmp_repo, None) == []


def test_discover_includes_subagent_files_with_parent(tmp_repo, tmp_path):
    _project(tmp_path, tmp_repo)
    sub = tmp_path / "projects" / "-whatever-encoding" / "S1" / "subagents" / "agent-abc.jsonl"
    _write(sub, [_entry("user", str(tmp_repo), isSidechain=True, agentId="abc",
                        message={"role": "user", "content": "subtask"})])
    refs = ClaudeSource(root=tmp_path / "projects").discover(tmp_repo, None)
    by_id = {r.session_id: r for r in refs}
    assert by_id["agent-abc"].parent_session_id == "S1"
    assert by_id["S1"].parent_session_id is None


def test_discover_filters_by_mtime(tmp_repo, tmp_path):
    import os, time
    f = _project(tmp_path, tmp_repo)
    old = time.time() - 3600
    os.utime(f, (old, old))
    since = datetime.now(timezone.utc)
    assert ClaudeSource(root=tmp_path / "projects").discover(tmp_repo, since) == []


def test_parse_all_blocks_and_tool_results(tmp_repo, tmp_path):
    repo = str(tmp_repo)
    entries = [
        _entry("user", repo, ts=TS.format(1), message={"role": "user", "content": "fix it"}),
        _entry("assistant", repo, ts=TS.format(2), message={"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "On it."},
            {"type": "tool_use", "id": "toolu_1", "name": "Edit",
             "input": {"file_path": "plumb/x.py", "old_string": "a", "new_string": "b"}},
        ]}),
        _entry("user", repo, ts=TS.format(3), message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "OK edited", "is_error": False}]}),
        _entry("assistant", repo, ts=TS.format(4), message={"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {"command": "pytest -x"}}]}),
        _entry("user", repo, ts=TS.format(5), message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_2",
             "content": [{"type": "text", "text": "12 passed"}]}]}),
    ]
    f = _project(tmp_path, tmp_repo, entries=entries)
    ref = SessionRef(agent="claude", session_id="S1", path=str(f), cwd=repo)
    turns = ClaudeSource().parse(ref, since=None)

    assert [t.role for t in turns] == ["user", "assistant", "assistant"]
    assert [t.ordinal for t in turns] == [0, 1, 2]
    assert turns[1].content == "On it."
    tc = turns[1].tool_calls[0]
    assert (tc.name, tc.category, tc.file_path) == ("Edit", "Edit", "plumb/x.py")
    assert tc.result_summary == "OK edited"
    assert turns[2].tool_calls[0].input_summary == "pytest -x"
    assert turns[2].tool_calls[0].result_summary == "12 passed"


def test_parse_since_filters_but_keeps_ordinals(tmp_repo, tmp_path):
    repo = str(tmp_repo)
    entries = [
        _entry("user", repo, ts="2026-01-01T00:00:00Z", message={"role": "user", "content": "old"}),
        _entry("user", repo, ts="2026-06-01T00:00:00Z", message={"role": "user", "content": "new"}),
    ]
    f = _project(tmp_path, tmp_repo, entries=entries)
    ref = SessionRef(agent="claude", session_id="S1", path=str(f), cwd=repo)
    turns = ClaudeSource().parse(ref, since=datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert [(t.content, t.ordinal) for t in turns] == [("new", 1)]


def test_parse_skips_meta_but_keeps_sidechain(tmp_repo, tmp_path):
    repo = str(tmp_repo)
    entries = [
        _entry("user", repo, isMeta=True, message={"role": "user", "content": "meta"}),
        _entry("user", repo, isSidechain=True, message={"role": "user", "content": "side"}),
    ]
    f = _project(tmp_path, tmp_repo, entries=entries)
    ref = SessionRef(agent="claude", session_id="S1", path=str(f), cwd=repo)
    assert [t.content for t in ClaudeSource().parse(ref, None)] == ["side"]


def test_parse_error_result_is_prefixed(tmp_repo, tmp_path):
    repo = str(tmp_repo)
    entries = [
        _entry("assistant", repo, ts=TS.format(1), message={"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "false"}}]}),
        _entry("user", repo, ts=TS.format(2), message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_9", "content": "exit 1", "is_error": True}]}),
    ]
    f = _project(tmp_path, tmp_repo, entries=entries)
    ref = SessionRef(agent="claude", session_id="S1", path=str(f), cwd=repo)
    assert ClaudeSource().parse(ref, None)[0].tool_calls[0].result_summary == "ERROR: exit 1"


def test_parse_user_text_block_list_with_image(tmp_repo, tmp_path):
    repo = str(tmp_repo)
    entries = [_entry("user", repo, message={"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "data": "..."}},
        {"type": "text", "text": "center the badges"}]})]
    f = _project(tmp_path, tmp_repo, entries=entries)
    ref = SessionRef(agent="claude", session_id="S1", path=str(f), cwd=repo)
    turns = ClaudeSource().parse(ref, None)
    assert [(t.role, t.content) for t in turns] == [("user", "center the badges")]


def test_discover_includes_nested_workflow_subagent_files(tmp_repo, tmp_path):
    _project(tmp_path, tmp_repo)
    sub = (tmp_path / "projects" / "-whatever-encoding" / "S1" / "subagents"
           / "workflows" / "wf_1" / "agent-nest.jsonl")
    _write(sub, [_entry("user", str(tmp_repo), isSidechain=True, agentId="nest",
                        message={"role": "user", "content": "nested subtask"})])
    refs = ClaudeSource(root=tmp_path / "projects").discover(tmp_repo, None)
    by_id = {r.session_id: r for r in refs}
    assert by_id["agent-nest"].parent_session_id == "S1"
    assert by_id["agent-nest"].path == str(sub)


def test_parse_tolerates_non_dict_message(tmp_repo, tmp_path):
    repo = str(tmp_repo)
    entries = [
        _entry("user", repo, message="garbage"),
        _entry("user", repo, message=None),
        _entry("user", repo, message={"role": "user", "content": "valid"}),
    ]
    f = _project(tmp_path, tmp_repo, entries=entries)
    ref = SessionRef(agent="claude", session_id="S1", path=str(f), cwd=repo)
    assert [t.content for t in ClaudeSource().parse(ref, None)] == ["valid"]
