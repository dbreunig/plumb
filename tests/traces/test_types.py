from plumb.traces import SessionRef, ToolCall, Turn, TraceSource


def test_toolcall_defaults():
    tc = ToolCall(name="Edit", category="Edit")
    assert tc.file_path is None
    assert tc.input_summary == ""
    assert tc.result_summary is None


def test_turn_defaults():
    t = Turn(agent="claude", session_id="s1", ordinal=0, role="user", content="hi")
    assert t.tool_calls == []
    assert t.timestamp is None


def test_sessionref_fields():
    ref = SessionRef(agent="pi", session_id="abc", path="/tmp/x.jsonl", cwd="/repo")
    assert ref.branch is None
    assert ref.parent_session_id is None


def test_tracesource_is_runtime_checkable():
    class Fake:
        name = "fake"
        def discover(self, repo_root, since): return []
        def parse(self, ref, since): return []
    assert isinstance(Fake(), TraceSource)
