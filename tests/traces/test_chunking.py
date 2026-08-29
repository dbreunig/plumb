from plumb.traces import ToolCall, Turn
from plumb.conversation import chunk_conversation, render_turn


def _t(role, content, agent="claude", session="S1", ordinal=0, calls=()):
    return Turn(agent=agent, session_id=session, ordinal=ordinal, role=role,
                content=content, tool_calls=list(calls))


def test_render_turn_with_tool_calls():
    t = _t("assistant", "Switching cache.", calls=[
        ToolCall(name="Edit", category="Edit", file_path="plumb/cache.py", input_summary="plumb/cache.py"),
        ToolCall(name="Bash", category="Bash", input_summary="pytest -x", result_summary="12 passed"),
    ])
    assert render_turn(t) == (
        "[assistant]: Switching cache.\n"
        "  [Edit plumb/cache.py]\n"
        "  [Bash: pytest -x] -> 12 passed"
    )
    tool_only = _t("assistant", "", calls=[ToolCall(name="Bash", category="Bash", input_summary="ls")])
    assert render_turn(tool_only).split("\n")[0] == "[assistant]"


def test_render_turn_plain():
    assert render_turn(_t("user", "hi")) == "[user]: hi"


def test_chunks_never_span_sessions():
    turns = [_t("user", "a", session="S1", ordinal=0), _t("assistant", "b", session="S1", ordinal=1),
             _t("user", "c", session="S2", ordinal=0), _t("assistant", "d", session="S2", ordinal=1)]
    chunks = chunk_conversation(turns)
    assert [(c.agent, c.session_id, c.turn_start, c.turn_end) for c in chunks] == [
        ("claude", "S1", 0, 1), ("claude", "S2", 0, 1)]
    assert [c.chunk_index for c in chunks] == [0, 1]


def test_chunk_text_has_header():
    chunks = chunk_conversation([_t("user", "a", agent="codex", session="019abc", ordinal=3)])
    assert chunks[0].text.startswith("[agent=codex session=019abc turns 3-3]\n[user]: a")


def test_overlap_stays_within_session():
    turns = [_t("user", "u1", ordinal=0), _t("assistant", "a1", ordinal=1),
             _t("user", "u2", ordinal=2), _t("assistant", "a2", ordinal=3)]
    chunks = chunk_conversation(turns)
    assert len(chunks) == 2
    assert chunks[1].turns[0].content == "a1"        # one-turn overlap
    assert chunks[1].turn_start == 2                 # range excludes the overlap


def test_all_sources_imports_and_reads_this_repo(tmp_repo, tmp_path, monkeypatch):
    """Guards the lazy import in all_sources() and the read_conversation wiring."""
    import json
    from plumb.traces import all_sources
    from plumb.conversation import read_conversation, read_conversation_with_refs
    from plumb.traces.claude import ClaudeSource

    proj = tmp_path / "projects" / "-enc"
    proj.mkdir(parents=True)
    (proj / "S9.jsonl").write_text(json.dumps({
        "type": "user", "cwd": str(tmp_repo), "gitBranch": "main", "sessionId": "S9",
        "timestamp": "2026-06-01T00:00:00Z", "isSidechain": False, "isMeta": False,
        "message": {"role": "user", "content": "hello"}}) + "\n")
    monkeypatch.setattr("plumb.traces.all_sources", lambda: [ClaudeSource(root=tmp_path / "projects")])

    names = [s.name for s in all_sources()]
    assert "claude" in names
    turns = read_conversation(tmp_repo)
    assert [(t.agent, t.session_id, t.content) for t in turns] == [("claude", "S9", "hello")]
    turns2, refs = read_conversation_with_refs(tmp_repo)
    assert refs[("claude", "S9")].path.endswith("S9.jsonl")


def test_render_turn_tool_only():
    t = _t("assistant", "", calls=[ToolCall(name="Bash", category="Bash", input_summary="ls")])
    assert render_turn(t) == "[assistant]\n  [Bash: ls]"
    assert render_turn(_t("user", "")) == "[user]"


def test_chunk_header_carries_parent():
    chunks = chunk_conversation([_t("user", "sub", session="agent-x", ordinal=0)],
                                parents={("claude", "agent-x"): "S1"})
    assert chunks[0].parent_session_id == "S1"
    assert chunks[0].text.startswith("[agent=claude session=agent-x parent=S1 turns 0-0]")


def test_parents_from_refs():
    from plumb.conversation import parents_from_refs
    from plumb.traces import SessionRef
    refs = {("claude", "S1"): SessionRef(agent="claude", session_id="S1", path="/a", cwd="/r"),
            ("claude", "agent-x"): SessionRef(agent="claude", session_id="agent-x", path="/b", cwd="/r", parent_session_id="S1")}
    assert parents_from_refs(refs) == {("claude", "agent-x"): "S1"}


def test_read_conversation_isolates_failing_session(tmp_repo, monkeypatch):
    from plumb.conversation import read_conversation_with_refs
    from plumb.traces import SessionRef, Turn
    good = SessionRef(agent="fake", session_id="ok", path="/ok", cwd=str(tmp_repo))
    bad = SessionRef(agent="fake", session_id="bad", path="/bad", cwd=str(tmp_repo))
    class Fake:
        name = "fake"
        def discover(self, repo_root, since): return [bad, good]
        def parse(self, ref, since):
            if ref.session_id == "bad": raise ValueError("boom")
            return [Turn(agent="fake", session_id="ok", ordinal=0, role="user", content="x")]
    class Broken:
        name = "broken"
        def discover(self, repo_root, since): raise RuntimeError("no disk")
        def parse(self, ref, since): return []
    monkeypatch.setattr("plumb.traces.all_sources", lambda: [Broken(), Fake()])
    turns, refs = read_conversation_with_refs(tmp_repo)
    assert [t.session_id for t in turns] == ["ok"]
    assert list(refs) == [("fake", "ok")]
