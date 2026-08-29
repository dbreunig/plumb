import json
import subprocess
from datetime import datetime, timezone

from plumb.traces import SessionRef
from plumb.traces.copilot import CopilotSource


def _ev(kind, data, ts="2026-06-01T00:00:00Z"):
    return {"type": kind, "timestamp": ts, "data": data}


def _events(cwd, sid="abc-123"):
    start = {"context": {"cwd": cwd, "branch": "main"}}
    if sid is not None:
        start["sessionId"] = sid
    return [
        _ev("session.start", start),
        _ev("user.message", {"content": "add x", "source": "user"}),
        _ev("assistant.reasoning", {"content": "thinking..."}),
        _ev("assistant.message", {
            "content": "Editing.",
            "reasoningText": "ignored",
            "toolRequests": [
                {"toolCallId": "tc-1", "name": "edit_file",
                 "arguments": {"path": "src/x.py", "old_str": "a", "new_str": "b"}},
                {"toolCallId": "tc-2", "name": "shell",
                 "arguments": json.dumps({"command": "pytest -x"})},
            ],
        }),
        _ev("tool.execution_start", {"toolCallId": "tc-1"}),
        _ev("tool.execution_complete", {"toolCallId": "tc-1", "success": True, "result": "edited"}),
        _ev("tool.execution_start", {"toolCallId": "tc-2"}),
        _ev("tool.execution_complete", {"toolCallId": "tc-2", "success": True, "result": "3 passed"}),
        _ev("session.model_change", {"newModel": "gpt-5"}),
        _ev("session.shutdown", {"modelMetrics": {}}),
    ]


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return path


def _dir_session(root, cwd, uuid="abc-123", sid="abc-123"):
    return _write(root / "session-state" / uuid / "events.jsonl", _events(cwd, sid))


def _bare_session(root, cwd, uuid="abc-123", sid="abc-123"):
    return _write(root / "session-state" / f"{uuid}.jsonl", _events(cwd, sid))


def test_discover_by_session_start_cwd(tmp_repo, tmp_path):
    root = tmp_path / "copilot"
    f = _dir_session(root, str(tmp_repo))
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    _dir_session(root, str(other), uuid="zzz", sid="zzz")
    refs = CopilotSource(root=root / "session-state").discover(tmp_repo, None)
    assert [(r.agent, r.session_id, r.path, r.branch, r.cwd) for r in refs] == \
        [("copilot", "abc-123", str(f), "main", str(tmp_repo))]


def test_discover_prefers_directory_form_over_bare(tmp_repo, tmp_path):
    root = tmp_path / "copilot"
    f = _dir_session(root, str(tmp_repo))
    _bare_session(root, str(tmp_repo))
    refs = CopilotSource(root=root / "session-state").discover(tmp_repo, None)
    assert [r.path for r in refs] == [str(f)]


def test_discover_bare_form(tmp_repo, tmp_path):
    root = tmp_path / "copilot"
    f = _bare_session(root, str(tmp_repo), uuid="bare-1", sid=None)
    refs = CopilotSource(root=root / "session-state").discover(tmp_repo, None)
    assert [(r.session_id, r.path) for r in refs] == [("bare-1", str(f))]


def test_discover_directory_form_session_id_falls_back_to_dir_name(tmp_repo, tmp_path):
    root = tmp_path / "copilot"
    _dir_session(root, str(tmp_repo), uuid="dir-1", sid=None)
    refs = CopilotSource(root=root / "session-state").discover(tmp_repo, None)
    assert [r.session_id for r in refs] == ["dir-1"]


def test_discover_filters_by_mtime(tmp_repo, tmp_path):
    import os, time
    root = tmp_path / "copilot"
    f = _dir_session(root, str(tmp_repo))
    old = time.time() - 3600
    os.utime(f, (old, old))
    assert CopilotSource(root=root / "session-state").discover(tmp_repo, datetime.now(timezone.utc)) == []


def test_parse_turns_and_tool_results(tmp_repo, tmp_path):
    f = _dir_session(tmp_path / "copilot", str(tmp_repo))
    ref = SessionRef(agent="copilot", session_id="abc-123", path=str(f), cwd=str(tmp_repo))
    turns = CopilotSource().parse(ref, None)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert [t.ordinal for t in turns] == [0, 1]
    assert turns[0].content == "add x"
    assert turns[1].content == "Editing."
    edit, sh = turns[1].tool_calls
    assert (edit.name, edit.category, edit.file_path, edit.file_paths, edit.result_summary, edit.tool_use_id) == \
        ("edit_file", "Edit", "src/x.py", ["src/x.py"], "edited", "tc-1")
    assert (sh.name, sh.category, sh.input_summary, sh.result_summary, sh.file_path) == \
        ("shell", "Bash", "pytest -x", "3 passed", None)


def test_parse_non_string_result_and_failure(tmp_repo, tmp_path):
    lines = [
        _ev("session.start", {"sessionId": "s", "context": {"cwd": str(tmp_repo)}}),
        _ev("assistant.message", {"content": "", "toolRequests": [
            {"toolCallId": "a", "name": "view", "arguments": "{\"path\": \"a.py\"}"},
            {"toolCallId": "b", "name": "shell", "arguments": "{\"command\": \"false\"}"},
        ]}),
        _ev("tool.execution_complete", {"toolCallId": "a", "success": True, "result": {"files": ["a.py"]}}),
        _ev("tool.execution_complete", {"toolCallId": "b", "success": False, "result": "exit 1"}),
    ]
    f = _write(tmp_path / "copilot" / "session-state" / "s.jsonl", lines)
    ref = SessionRef(agent="copilot", session_id="s", path=str(f), cwd=str(tmp_repo))
    turns = CopilotSource().parse(ref, None)
    assert [t.role for t in turns] == ["assistant"]
    a, b = turns[0].tool_calls
    assert a.result_summary == '{"files": ["a.py"]}'
    assert b.result_summary == "ERROR: exit 1"


def test_parse_skips_skill_messages(tmp_repo, tmp_path):
    lines = [
        _ev("session.start", {"sessionId": "s", "context": {"cwd": str(tmp_repo)}}),
        _ev("user.message", {"content": "<skill-context name=\"gh\">\nbody\n</skill-context>",
                             "source": "skill-gh"}),
        _ev("user.message", {"content": "skill payload without wrapper", "source": "skill-prd"}),
        _ev("user.message", {"content": "<skill-context name=\"x\">\nbody\n</skill-context>"}),
        _ev("user.message", {"content": "  real question  ", "source": "user"}),
    ]
    f = _write(tmp_path / "copilot" / "session-state" / "s.jsonl", lines)
    ref = SessionRef(agent="copilot", session_id="s", path=str(f), cwd=str(tmp_repo))
    turns = CopilotSource().parse(ref, None)
    assert [(t.role, t.content) for t in turns] == [("user", "real question")]


def test_parse_since_keeps_ordinals(tmp_repo, tmp_path):
    lines = [
        _ev("session.start", {"sessionId": "s", "context": {"cwd": str(tmp_repo)}}),
        _ev("user.message", {"content": "old"}, ts="2026-01-01T00:00:00Z"),
        _ev("user.message", {"content": "new"}, ts="2026-06-01T00:00:00Z"),
    ]
    f = _write(tmp_path / "copilot" / "session-state" / "s.jsonl", lines)
    ref = SessionRef(agent="copilot", session_id="s", path=str(f), cwd=str(tmp_repo))
    turns = CopilotSource().parse(ref, datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert [(t.content, t.ordinal) for t in turns] == [("new", 1)]


def test_all_sources_includes_copilot():
    from plumb.traces import all_sources
    assert [s.name for s in all_sources()] == ["claude", "codex", "pi", "copilot"]
