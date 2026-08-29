# Multi-Agent Traces Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace Plumb's Claude-only transcript reader with an agent-agnostic `TraceSource` layer (Claude, Codex, Pi, Copilot) that preserves tool calls, segments per session, and stamps every extracted decision with verifiable provenance.

**Architecture:** Stage 1 (`plumb/traces/`) discovers sessions by the `cwd` recorded inside each transcript and normalizes them into `Turn`/`ToolCall` records. Stage 2 (`conversation.py`, `git_hook.py`) chunks per `(agent, session_id)`, renders tool calls with substance for the extractor, and enriches each `Decision` with `agent`, `session_id`, `turn_range`, `evidence_digest`, and deterministic `file_refs` (edited paths ∩ staged hunks). The DSPy `DecisionExtractor` call is unchanged. No trace data is stored; the transcript on disk is the evidence store. Design: `docs/plans/2026-08-29-multi-agent-traces-design.md`.

**Tech Stack:** Python 3.10+, pydantic v2, GitPython, click/rich, pytest. Run everything with `uv run`.

**Conventions for the executor:**
- @superpowers:test-driven-development — write the failing test first, every task.
- The pre-commit Plumb hook is disabled in this checkout (`.git/hooks/pre-commit.disabled`). Commit normally; do not run `plumb diff`/`plumb sync`.
- `tests/test_generated.py` has a pre-existing `SyntaxError` and is excluded: always run `uv run pytest tests/ -q --ignore=tests/test_generated.py`. 12 other failures are pre-existing (`test_sync.py`, `test_integration.py`, `test_git_hook_extended.py::test_with_conversation_log`); the baseline is **378 passed, 12 failed**. Do not make it worse.
- Default LLM is Haiku (`plumb/programs/__init__.py`). Do not change it.
- Commit after every task with the message given. Messages end with the trailer block used in this repo (`Co-Authored-By` / `Claude-Session`) if you are Claude; otherwise a plain message is fine.

---

## Task 1: Trace types and the `TraceSource` protocol

**Files:**
- Create: `plumb/traces/__init__.py`
- Test: `tests/traces/__init__.py` (empty), `tests/traces/test_types.py`

**Step 1: Write the failing test**

```python
# tests/traces/test_types.py
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
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/traces/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'plumb.traces'`

**Step 3: Write minimal implementation**

```python
# plumb/traces/__init__.py
"""Agent-agnostic trace ingestion.

A TraceSource finds an agent's sessions for a repo and normalizes them into
Turn/ToolCall records. Everything downstream of this package is agent-agnostic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class SessionRef(BaseModel):
    agent: str
    session_id: str
    path: str
    cwd: str
    branch: Optional[str] = None
    parent_session_id: Optional[str] = None


class ToolCall(BaseModel):
    name: str                       # raw tool name, e.g. "apply_patch"
    category: str                   # Read|Edit|Write|Bash|Grep|Glob|Task|Tool|Other
    file_path: Optional[str] = None
    input_summary: str = ""         # first ~120 chars of the salient argument
    result_summary: Optional[str] = None  # truncated result, when the agent records one
    tool_use_id: Optional[str] = None


class Turn(BaseModel):
    agent: str
    session_id: str
    ordinal: int                    # position within the session, stable across runs
    role: str                       # "user" | "assistant"
    content: str = ""
    timestamp: Optional[str] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


@runtime_checkable
class TraceSource(Protocol):
    name: str

    def discover(self, repo_root, since: Optional[datetime]) -> list[SessionRef]:
        """Sessions that touched repo_root and were active after `since`."""
        ...

    def parse(self, ref: SessionRef, since: Optional[datetime]) -> list[Turn]:
        """Normalized turns for one session, in order, after `since`."""
        ...


def all_sources() -> list[TraceSource]:
    """Registered adapters. Add to this list; no registry until it hurts."""
    from plumb.traces.claude import ClaudeSource

    return [ClaudeSource()]
```

`all_sources()` will fail to import until Task 5; that is fine — nothing calls it yet.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/traces/test_types.py -v`
Expected: 4 PASS

**Step 5: Commit**

```bash
git add plumb/traces/__init__.py tests/traces/__init__.py tests/traces/test_types.py
git commit -m "feat(traces): add Turn/ToolCall/SessionRef types and TraceSource protocol"
```

---

## Task 2: Tool taxonomy

**Files:**
- Create: `plumb/traces/taxonomy.py`
- Test: `tests/traces/test_taxonomy.py`

**Step 1: Write the failing test**

```python
# tests/traces/test_taxonomy.py
import pytest
from plumb.traces.taxonomy import categorize, file_path_from_input, input_summary


@pytest.mark.parametrize("name,expected", [
    ("Read", "Read"), ("Edit", "Edit"), ("Write", "Write"), ("NotebookEdit", "Write"),
    ("Bash", "Bash"), ("Grep", "Grep"), ("Glob", "Glob"), ("Task", "Task"), ("Agent", "Task"),
    ("Skill", "Tool"),
    ("shell_command", "Bash"), ("exec_command", "Bash"), ("shell", "Bash"), ("exec", "Bash"),
    ("apply_patch", "Edit"), ("list_files", "Read"), ("spawn_agent", "Task"),
    ("read_file", "Read"), ("read", "Read"), ("find", "Read"), ("view", "Read"),
    ("str_replace", "Edit"), ("edit", "Edit"), ("edit_file", "Edit"),
    ("create_file", "Write"), ("write", "Write"), ("write_file", "Write"),
    ("run_command", "Bash"), ("bash", "Bash"),
    ("grep", "Grep"), ("glob", "Glob"), ("report_intent", "Tool"),
    ("mcp__pencil__batch_get", "Tool"),
    ("something_new", "Other"),
])
def test_categorize(name, expected):
    assert categorize(name) == expected


def test_file_path_from_common_keys():
    assert file_path_from_input("Edit", {"file_path": "a.py"}) == "a.py"
    assert file_path_from_input("read", {"path": "b.py"}) == "b.py"
    assert file_path_from_input("Bash", {"command": "ls"}) is None


def test_file_path_from_apply_patch():
    patch = "*** Begin Patch\n*** Update File: src/x.py\n@@\n-a\n+b\n*** End Patch"
    assert file_path_from_input("apply_patch", {"input": patch}) == "src/x.py"
    assert file_path_from_input("apply_patch", patch) == "src/x.py"


def test_input_summary_prefers_command_then_path():
    assert input_summary("Bash", {"command": "pytest -x"}) == "pytest -x"
    assert input_summary("Edit", {"file_path": "a.py", "old_string": "x"}) == "a.py"
    assert len(input_summary("Bash", {"command": "x" * 500})) == 120
    assert input_summary("weird", "raw string arg") == "raw string arg"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/traces/test_taxonomy.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# plumb/traces/taxonomy.py
"""Nine-category tool taxonomy, ported from agentsview's taxonomy.go.

Every agent's tool vocabulary collapses into Read/Edit/Write/Bash/Grep/Glob/
Task/Tool/Other so downstream code never sees agent-specific names.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_CATEGORIES: dict[str, str] = {
    # Claude Code
    "Read": "Read", "Edit": "Edit", "Write": "Write", "NotebookEdit": "Write",
    "Bash": "Bash", "Grep": "Grep", "Glob": "Glob", "Task": "Task", "Agent": "Task",
    "Skill": "Tool", "WebFetch": "Tool", "WebSearch": "Tool",
    # Codex
    "shell_command": "Bash", "exec_command": "Bash", "write_stdin": "Bash", "shell": "Bash",
    "exec": "Bash", "list_files": "Read", "apply_patch": "Edit", "spawn_agent": "Task",
    # Pi
    "read_file": "Read", "read": "Read", "find": "Read", "ls": "Read",
    "str_replace": "Edit", "edit": "Edit", "create_file": "Write", "write": "Write",
    "run_command": "Bash", "bash": "Bash", "grep": "Grep", "glob": "Glob",
    # Copilot
    "view": "Read", "edit_file": "Edit", "write_file": "Write", "report_intent": "Tool",
    # Gemini / OpenCode (cheap to include)
    "list_directory": "Read", "replace": "Edit", "run_shell_command": "Bash",
    "search_files": "Grep", "grep_search": "Grep", "task": "Task",
}

_PATH_KEYS = ("file_path", "path", "filePath", "notebook_path", "target_file")
_SUMMARY_KEYS = ("command", "cmd", "pattern", "prompt", "description", "query")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)


def categorize(name: str) -> str:
    if name in _CATEGORIES:
        return _CATEGORIES[name]
    if name.startswith("mcp__"):
        return "Tool"
    return "Other"


def _as_dict(tool_input: Any) -> dict:
    if isinstance(tool_input, dict):
        return tool_input
    if isinstance(tool_input, str):
        try:
            parsed = json.loads(tool_input)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def file_path_from_input(name: str, tool_input: Any) -> Optional[str]:
    if name == "apply_patch":
        text = tool_input if isinstance(tool_input, str) else _as_dict(tool_input).get("input", "")
        m = _PATCH_FILE_RE.search(text or "")
        return m.group(1).strip() if m else None
    d = _as_dict(tool_input)
    for key in _PATH_KEYS:
        val = d.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def input_summary(name: str, tool_input: Any, limit: int = 120) -> str:
    if isinstance(tool_input, str) and not tool_input.lstrip().startswith("{"):
        return tool_input[:limit]
    d = _as_dict(tool_input)
    for key in _SUMMARY_KEYS + _PATH_KEYS:
        val = d.get(key)
        if isinstance(val, str) and val:
            return val.replace("\n", " ")[:limit]
    return ""
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/traces/test_taxonomy.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add plumb/traces/taxonomy.py tests/traces/test_taxonomy.py
git commit -m "feat(traces): add 9-category tool taxonomy with path/summary extraction"
```

---

## Task 3: Repo matching by `cwd` and JSONL helpers

**Files:**
- Create: `plumb/traces/repo.py`, `plumb/traces/jsonl.py`
- Test: `tests/traces/test_repo.py`, `tests/traces/test_jsonl.py`

**Step 1: Write the failing tests**

```python
# tests/traces/test_repo.py
import subprocess
from plumb.traces.repo import git_common_dir, same_repo, commit_datetime


def test_git_common_dir_resolves_subdir(tmp_repo):
    sub = tmp_repo / "pkg"
    sub.mkdir()
    assert git_common_dir(str(sub)) == (tmp_repo / ".git").resolve()


def test_git_common_dir_none_outside_repo(tmp_path):
    assert git_common_dir(str(tmp_path)) is None


def test_same_repo_true_for_subdir(tmp_repo):
    assert same_repo(str(tmp_repo / "pkg"), tmp_repo)


def test_same_repo_true_for_linked_worktree(tmp_repo, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(tmp_repo), "worktree", "add", "--detach", str(wt)],
                   check=True, capture_output=True)
    assert same_repo(str(wt), tmp_repo)


def test_same_repo_false_for_other_repo(tmp_repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    assert not same_repo(str(other), tmp_repo)


def test_same_repo_false_for_missing_path(tmp_repo):
    assert not same_repo("/definitely/not/here", tmp_repo)


def test_commit_datetime(tmp_repo):
    from git import Repo
    sha = Repo(tmp_repo).head.commit.hexsha
    assert commit_datetime(tmp_repo, sha) is not None
    assert commit_datetime(tmp_repo, "0" * 40) is None
```

```python
# tests/traces/test_jsonl.py
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/traces/test_repo.py tests/traces/test_jsonl.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# plumb/traces/repo.py
"""Attribute a recorded cwd to a git repository (worktree-aware)."""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _existing_ancestor(path: str) -> Optional[Path]:
    p = Path(path)
    for cand in [p] + list(p.parents):
        if cand.is_dir():
            return cand
    return None


@lru_cache(maxsize=512)
def git_common_dir(path: str) -> Optional[Path]:
    """Resolve `path` to its repo's common .git dir; None if not in a repo.

    Uses --git-common-dir (not --show-toplevel) so linked worktrees resolve
    to the same repo as the main checkout.
    """
    start = _existing_ancestor(path)
    if start is None:
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(start), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    common = Path(out.stdout.strip())
    if not common.is_absolute():
        common = start / common
    return common.resolve()


def same_repo(cwd: str, repo_root: str | Path) -> bool:
    if not cwd:
        return False
    target = git_common_dir(str(repo_root))
    return target is not None and git_common_dir(cwd) == target


def commit_datetime(repo_root: str | Path, sha: str) -> Optional[datetime]:
    try:
        from git import Repo
        return Repo(repo_root).commit(sha).committed_datetime
    except Exception:
        logger.debug("Could not resolve commit %s", sha)
        return None
```

```python
# plumb/traces/jsonl.py
"""Tolerant JSONL reading shared by every adapter."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


def iter_jsonl(path: Path) -> Iterator[dict]:
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def sniff_head(path: Path, pick: Callable[[dict], Any], max_lines: int = 64) -> Optional[Any]:
    """Return pick(entry) for the first of the first `max_lines` entries where it is truthy."""
    for i, entry in enumerate(iter_jsonl(path)):
        if i >= max_lines:
            return None
        val = pick(entry)
        if val:
            return val
    return None
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/traces/test_repo.py tests/traces/test_jsonl.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add plumb/traces/repo.py plumb/traces/jsonl.py tests/traces/test_repo.py tests/traces/test_jsonl.py
git commit -m "feat(traces): cwd-based repo matching via git-common-dir; tolerant JSONL helpers"
```

---

## Task 4: Time-cutoff helper

Both `discover` (file mtime) and `parse` (entry timestamp) need the same cutoff logic that `read_claude_sessions` has today.

**Files:**
- Modify: `plumb/traces/repo.py` (append)
- Test: `tests/traces/test_repo.py` (append)

**Step 1: Write the failing test**

```python
# append to tests/traces/test_repo.py
from datetime import datetime, timezone
from plumb.traces.repo import resolve_cutoff, parse_ts, after_cutoff


def test_resolve_cutoff_prefers_datetime(tmp_repo):
    from git import Repo
    sha = Repo(tmp_repo).head.commit.hexsha
    dt = resolve_cutoff(tmp_repo, since_commit=sha, since_datetime="2030-01-01T00:00:00Z")
    assert dt.year == 2030


def test_resolve_cutoff_falls_back_to_commit(tmp_repo):
    from git import Repo
    sha = Repo(tmp_repo).head.commit.hexsha
    assert resolve_cutoff(tmp_repo, since_commit=sha, since_datetime=None) is not None


def test_resolve_cutoff_none(tmp_repo):
    assert resolve_cutoff(tmp_repo, None, None) is None


def test_parse_ts_handles_z_and_epoch_ms():
    assert parse_ts("2026-01-01T00:00:00Z").tzinfo is not None
    assert parse_ts(1786916035750).year == 2026
    assert parse_ts("garbage") is None
    assert parse_ts(None) is None


def test_after_cutoff():
    cut = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert after_cutoff("2026-01-02T00:00:00Z", cut)
    assert not after_cutoff("2025-12-31T00:00:00Z", cut)
    assert after_cutoff(None, cut)          # unknown timestamps are kept
    assert after_cutoff("2025-01-01T00:00:00Z", None)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/traces/test_repo.py -v -k "cutoff or parse_ts"`
Expected: FAIL — `ImportError`

**Step 3: Write minimal implementation** (append to `plumb/traces/repo.py`)

```python
from datetime import timezone


def parse_ts(value) -> Optional[datetime]:
    """ISO-8601 string (with Z) or epoch ms/s -> aware datetime; None if unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def after_cutoff(ts, cutoff: Optional[datetime]) -> bool:
    if cutoff is None:
        return True
    dt = parse_ts(ts)
    if dt is None:
        return True
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return dt > cutoff


def resolve_cutoff(repo_root, since_commit: Optional[str], since_datetime: Optional[str]) -> Optional[datetime]:
    """since_datetime (last_extracted_at) wins; else the commit's datetime; else None."""
    dt = parse_ts(since_datetime) if since_datetime else None
    if dt is None and since_commit:
        dt = commit_datetime(repo_root, since_commit)
    return dt
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/traces/test_repo.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add plumb/traces/repo.py tests/traces/test_repo.py
git commit -m "feat(traces): shared cutoff/timestamp helpers"
```

---

## Task 5: Claude adapter

Replaces `plumb/claude_session.py`. Discovery scans every project dir under `~/.claude/projects/` (plus each session's `subagents/` dir), keeps files whose recorded `cwd` is in this repo. Parsing keeps every content block, attaches tool results to their `tool_use_id`, and accepts sidechain entries (they only appear in subagent files, which carry `parent_session_id`).

Real entry shapes (from a live transcript):
- user text: `{"type":"user","cwd":"/repo","gitBranch":"main","sessionId":"S","timestamp":"...","message":{"role":"user","content":"hi"}}`
- assistant: `{"type":"assistant",...,"message":{"content":[{"type":"text","text":"..."},{"type":"tool_use","id":"toolu_1","name":"Edit","input":{...}}]}}` — one block per line in practice, but treat as a list.
- tool result: `{"type":"user",...,"message":{"content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"...","is_error":false}]},"toolUseResult":{...,"agentId":"a239..."}}`
- subagent file: `<project>/<session_id>/subagents/agent-<id>.jsonl`, entries have `isSidechain: true`, `agentId`, same `sessionId` as parent.

**Files:**
- Create: `plumb/traces/claude.py`
- Test: `tests/traces/test_claude.py`

**Step 1: Write the failing test**

```python
# tests/traces/test_claude.py
import json
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
    _project(tmp_path, tmp_path / "elsewhere")
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
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/traces/test_claude.py -v`
Expected: FAIL — `ModuleNotFoundError: plumb.traces.claude`

**Step 3: Write minimal implementation**

```python
# plumb/traces/claude.py
"""Claude Code adapter: ~/.claude/projects/<enc>/<session>.jsonl (+ subagents/)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from plumb.traces import SessionRef, ToolCall, Turn
from plumb.traces.jsonl import iter_jsonl, sniff_head
from plumb.traces.repo import after_cutoff, same_repo
from plumb.traces.taxonomy import categorize, file_path_from_input, input_summary

RESULT_LIMIT = 200


def _default_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


class ClaudeSource:
    name = "claude"

    def __init__(self, root: Optional[Path] = None):
        self.root = root or _default_root()

    # -- discovery -----------------------------------------------------------

    def _candidates(self):
        if not self.root.is_dir():
            return
        for proj in self.root.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                yield f, None
            for sub in proj.glob("*/subagents/agent-*.jsonl"):
                yield sub, sub.parent.parent.name  # <session_id>/subagents/agent-x.jsonl

    def discover(self, repo_root, since: Optional[datetime]) -> list[SessionRef]:
        refs: list[SessionRef] = []
        cutoff_ts = since.timestamp() if since else None
        for path, parent in self._candidates():
            try:
                if cutoff_ts is not None and path.stat().st_mtime < cutoff_ts:
                    continue
            except OSError:
                continue
            head = sniff_head(path, lambda e: e if e.get("cwd") else None)
            if not head or not same_repo(head["cwd"], repo_root):
                continue
            refs.append(SessionRef(
                agent=self.name,
                session_id=path.stem if parent else (head.get("sessionId") or path.stem),
                path=str(path),
                cwd=head["cwd"],
                branch=head.get("gitBranch") or None,
                parent_session_id=parent,
            ))
        refs.sort(key=lambda r: Path(r.path).stat().st_mtime)
        return refs

    # -- parsing -------------------------------------------------------------

    def parse(self, ref: SessionRef, since: Optional[datetime]) -> list[Turn]:
        turns: list[Turn] = []
        by_tool_id: dict[str, ToolCall] = {}
        ordinal = 0
        for e in iter_jsonl(Path(ref.path)):
            if e.get("type") not in ("user", "assistant") or e.get("isMeta"):
                continue
            content = (e.get("message") or {}).get("content", "")
            ts = e.get("timestamp")

            if e["type"] == "user":
                if isinstance(content, str):
                    if content.strip():
                        turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                          role="user", content=content, timestamp=ts))
                        ordinal += 1
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            tc = by_tool_id.get(b.get("tool_use_id", ""))
                            if tc is not None:
                                text = _result_text(b.get("content"))
                                if b.get("is_error"):
                                    text = "ERROR: " + text
                                tc.result_summary = text.strip()[:RESULT_LIMIT] or None
                continue

            # assistant
            if not isinstance(content, list):
                continue
            texts, calls = [], []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    texts.append(b["text"])
                elif b.get("type") == "tool_use":
                    name = b.get("name", "unknown")
                    tc = ToolCall(name=name, category=categorize(name),
                                  file_path=file_path_from_input(name, b.get("input")),
                                  input_summary=input_summary(name, b.get("input")),
                                  tool_use_id=b.get("id"))
                    calls.append(tc)
                    if tc.tool_use_id:
                        by_tool_id[tc.tool_use_id] = tc
            if not texts and not calls:
                continue
            turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                              role="assistant", content="\n".join(texts), timestamp=ts, tool_calls=calls))
            ordinal += 1

        return [t for t in turns if after_cutoff(t.timestamp, since)]
```

Note the ordering subtlety: tool results arrive *after* the assistant turn that made the call, so `result_summary` is mutated in place on an already-appended `Turn` — that's intended and why `ToolCall` is a mutable model.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/traces/test_claude.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add plumb/traces/claude.py tests/traces/test_claude.py
git commit -m "feat(traces): Claude Code adapter with cwd discovery, full tool calls, subagent files"
```

---

## Task 6: Turn rendering and per-session chunking

`ConversationTurn` becomes a thin alias of `Turn` (same fields), chunk text renders tool calls with substance, and chunks never span sessions.

**Files:**
- Modify: `plumb/conversation.py` (whole file — see below)
- Modify: `tests/test_conversation.py` (drop `TestLocateConversationLog`, `TestReadConversationLog`; add rendering tests)
- Test: `tests/traces/test_chunking.py` (new)

**Step 1: Write the failing tests**

```python
# tests/traces/test_chunking.py
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
```

Also, in `tests/test_conversation.py`, delete `TestLocateConversationLog` and `TestReadConversationLog` (their functions are being removed) and change every `ConversationTurn(role=..., content=...)` construction to include `agent="claude", session_id="S", ordinal=<n>` — or use the `_t` helper above. `TestReduceNoise` and `TestChunkConversation` stay.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/traces/test_chunking.py tests/test_conversation.py -v`
Expected: FAIL — `ImportError: render_turn`

**Step 3: Write the implementation** — replace `plumb/conversation.py` with:

```python
"""Turn rendering and per-session chunking for the decision extractor."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from plumb.traces import ToolCall, Turn

# Backwards-compatible name; a ConversationTurn *is* a Turn.
ConversationTurn = Turn


class Chunk(BaseModel):
    chunk_index: int
    agent: str
    session_id: str
    turn_start: int
    turn_end: int
    start_timestamp: Optional[str] = None
    end_timestamp: Optional[str] = None
    truncated: bool = False
    turns: list[Turn] = Field(default_factory=list)

    @property
    def header(self) -> str:
        return f"[agent={self.agent} session={self.session_id} turns {self.turn_start}-{self.turn_end}]"

    @property
    def text(self) -> str:
        return "\n".join([self.header] + [render_turn(t) for t in self.turns])


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def render_tool_call(tc: ToolCall) -> str:
    if tc.category in ("Edit", "Write", "Read") and tc.file_path:
        head = f"[{tc.category} {tc.file_path}]"
    elif tc.input_summary:
        head = f"[{tc.category}: {tc.input_summary}]"
    else:
        head = f"[{tc.category}: {tc.name}]"
    if tc.result_summary:
        head += f" -> {tc.result_summary}"
    return "  " + head


def render_turn(t: Turn) -> str:
    lines = [f"[{t.role}]: {t.content}"]
    lines += [render_tool_call(tc) for tc in t.tool_calls]
    return "\n".join(lines)


def _looks_like_file_read(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    if stripped.startswith("```"):
        return True
    return bool(re.match(r"^[/.]", stripped) or re.match(r"^\w+[/\\]", stripped))


def reduce_noise(turns: list[Turn]) -> list[Turn]:
    """Replace >500-token turns that look like file reads with a placeholder."""
    result = []
    for turn in turns:
        if estimate_tokens(turn.content) > 500 and _looks_like_file_read(turn.content):
            first_line = turn.content.strip().split("\n")[0].strip("`").strip()[:100]
            result.append(turn.model_copy(update={"content": f"[file read: {first_line}]"}))
        else:
            result.append(turn)
    return result


def _split_at_tool_boundary(turns: list[Turn], max_tokens: int) -> list[list[Turn]]:
    chunks, current, current_tokens = [], [], 0
    for turn in turns:
        n = estimate_tokens(render_turn(turn))
        if current and current_tokens + n > max_tokens:
            chunks.append(current)
            current, current_tokens = [turn], n
        else:
            current.append(turn)
            current_tokens += n
    if current:
        chunks.append(current)
    return chunks


def _chunk_session(turns: list[Turn], max_tokens: int) -> list[list[Turn]]:
    groups: list[list[Turn]] = []
    current: list[Turn] = []
    for turn in turns:
        if turn.role == "user" and current:
            groups.append(current)
            current = [turn]
        else:
            current.append(turn)
    if current:
        groups.append(current)
    final: list[list[Turn]] = []
    for g in groups:
        if sum(estimate_tokens(render_turn(t)) for t in g) <= max_tokens:
            final.append(g)
        else:
            final.extend(_split_at_tool_boundary(g, max_tokens))
    return final


def chunk_conversation(turns: list[Turn], max_tokens: int = 6000) -> list[Chunk]:
    """Group by (agent, session_id) in first-seen order, then by user turn.

    One-turn overlap between consecutive chunks of the *same* session; the
    turn range excludes the overlap so provenance points at new content only.
    """
    if not turns:
        return []
    sessions: dict[tuple[str, str], list[Turn]] = {}
    for t in turns:
        sessions.setdefault((t.agent, t.session_id), []).append(t)

    chunks: list[Chunk] = []
    for (agent, session_id), sturns in sessions.items():
        sturns = sorted(sturns, key=lambda t: t.ordinal)
        groups = _chunk_session(sturns, max_tokens)
        for i, group in enumerate(groups):
            overlap = [groups[i - 1][-1]] if i > 0 else []
            all_turns = overlap + group
            stamps = [t.timestamp for t in all_turns if t.timestamp]
            chunks.append(Chunk(
                chunk_index=len(chunks), agent=agent, session_id=session_id,
                turn_start=group[0].ordinal, turn_end=group[-1].ordinal,
                start_timestamp=stamps[0] if stamps else None,
                end_timestamp=stamps[-1] if stamps else None,
                turns=all_turns,
            ))
    return chunks


def read_conversation(
    repo_root: Path,
    config_path: str | None = None,   # accepted and ignored; legacy config field
    since_commit: str | None = None,
    since_datetime: str | None = None,
) -> list[Turn]:
    """Stage 1: every registered TraceSource, every session in this repo since the cutoff."""
    from plumb.traces import all_sources
    from plumb.traces.repo import resolve_cutoff

    cutoff = resolve_cutoff(repo_root, since_commit, since_datetime)
    turns: list[Turn] = []
    for source in all_sources():
        for ref in source.discover(repo_root, cutoff):
            turns.extend(source.parse(ref, cutoff))
    return turns
```

Then **delete** `plumb/claude_session.py` and `tests/test_claude_session.py` — the adapter replaces both. Search for other importers first: `grep -rn "claude_session\|locate_conversation_log\|read_conversation_log" plumb tests` must return nothing except `plumb_spec.md` prose.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/traces tests/test_conversation.py -v`
Expected: all PASS. Then the full suite: `uv run pytest tests/ -q --ignore=tests/test_generated.py` — expect the baseline 12 failures, no new ones. (`test_git_hook_extended.py::test_with_conversation_log` was already failing; it tested the removed legacy path — delete that test.)

**Step 5: Commit**

```bash
git add plumb/conversation.py tests/traces/test_chunking.py tests/test_conversation.py tests/test_git_hook_extended.py
git rm plumb/claude_session.py tests/test_claude_session.py
git commit -m "feat(traces): per-session chunking with rendered tool calls; remove claude_session and legacy log path"
```

---

## Task 7: Provenance fields on `Decision`

**Files:**
- Modify: `plumb/decision_log.py:20-40`
- Test: `tests/test_decision_log.py` (append)

**Step 1: Write the failing test**

```python
# append to tests/test_decision_log.py
def test_decision_provenance_fields_roundtrip(initialized_repo):
    from plumb.decision_log import Decision, append_decisions, read_decisions
    d = Decision(id="dec-prov1", status="pending", decision="x", branch="main",
                 agent="codex", session_id="019a", parent_session_id=None,
                 source_path="/tmp/r.jsonl", turn_range=[12, 19], evidence_digest="ab" * 32)
    append_decisions(initialized_repo, [d], branch="main")
    back = {x.id: x for x in read_decisions(initialized_repo, branch="main")}["dec-prov1"]
    assert (back.agent, back.session_id, back.turn_range) == ("codex", "019a", [12, 19])
    assert back.evidence_digest == "ab" * 32


def test_decision_provenance_via_duckdb(initialized_repo):
    from plumb.decision_log import Decision, append_decisions, read_all_decisions
    append_decisions(initialized_repo, [Decision(id="dec-prov2", decision="y", branch="main",
                                                 agent="pi", turn_range=[0, 3])], branch="main")
    back = {x.id: x for x in read_all_decisions(initialized_repo)}["dec-prov2"]
    assert back.agent == "pi" and back.turn_range == [0, 3]
```

Check the exact signatures of `append_decisions`/`read_decisions` in `plumb/decision_log.py` before running; adjust the calls if the `branch` kwarg differs.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_decision_log.py -k provenance -v`
Expected: FAIL — pydantic ignores unknown fields, so `back.agent` raises `AttributeError`.

**Step 3: Write minimal implementation** — in `plumb/decision_log.py`, add to `Decision` after `related_requirement_ids`:

```python
    # Provenance (stage 2). turn_range is [start, end] ordinals within session.
    agent: Optional[str] = None
    session_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    source_path: Optional[str] = None
    turn_range: Optional[list[int]] = None
    evidence_digest: Optional[str] = None
```

Keep `chunk_index` (deprecated, no longer populated) so old rows still deserialize cleanly. In `_clean_duckdb_row`, `turn_range` comes back as a DuckDB list — `_to_python_native` already converts lists; verify with the DuckDB test.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_decision_log.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add plumb/decision_log.py tests/test_decision_log.py
git commit -m "feat(decisions): provenance fields (agent, session, source, turn_range, evidence_digest)"
```

---

## Task 8: Staged hunks and deterministic `file_refs`

**Files:**
- Create: `plumb/traces/hunks.py`
- Test: `tests/traces/test_hunks.py`

**Step 1: Write the failing test**

```python
# tests/traces/test_hunks.py
from git import Repo
from plumb.traces.hunks import parse_unified_hunks, staged_hunks, file_refs_for

DIFF = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -10,0 +11,3 @@ def f():
+x
+y
+z
@@ -40,2 +44 @@
-old
-old2
+new
diff --git a/src/gone.py b/src/gone.py
--- a/src/gone.py
+++ /dev/null
@@ -1,5 +0,0 @@
-bye
"""


def test_parse_unified_hunks():
    assert parse_unified_hunks(DIFF) == {"src/a.py": [[11, 13], [44, 44]]}


def test_staged_hunks_real_repo(tmp_repo):
    (tmp_repo / "README.md").write_text("# Test Repo\nline2\nline3\n")
    repo = Repo(tmp_repo)
    repo.index.add(["README.md"])
    assert staged_hunks(repo) == {"README.md": [[2, 3]]}


def test_file_refs_for_intersects_edited_paths():
    hunks = {"src/a.py": [[11, 13]], "src/b.py": [[1, 1]]}
    refs = file_refs_for(["src/a.py", "src/untouched.py", "/abs/src/b.py"], hunks, repo_root="/abs")
    assert [(r.file, r.lines) for r in refs] == [("src/a.py", [11, 13]), ("src/b.py", [1, 1])]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/traces/test_hunks.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# plumb/traces/hunks.py
"""Line ranges from the staged diff, and file_refs = edited paths ∩ staged files."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from git import Repo

from plumb.decision_log import FileRef

_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_unified_hunks(diff: str) -> dict[str, list[list[int]]]:
    """{path: [[new_start, new_end], ...]} for every added/modified range.

    Pure deletions (+N,0) contribute no range. Deleted files (+++ /dev/null) are skipped.
    """
    out: dict[str, list[list[int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        m = _FILE_RE.match(line)
        if m:
            current = m.group(1)
            continue
        if line.startswith("+++ /dev/null"):
            current = None
            continue
        h = _HUNK_RE.match(line)
        if h and current:
            start = int(h.group(1))
            count = int(h.group(2)) if h.group(2) is not None else 1
            if count > 0:
                out.setdefault(current, []).append([start, start + count - 1])
    return out


def staged_hunks(repo: Repo) -> dict[str, list[list[int]]]:
    return parse_unified_hunks(repo.git.diff("--cached", "-U0"))


def file_refs_for(edited_paths: Iterable[str], hunks: dict[str, list[list[int]]],
                  repo_root: str | Path) -> list[FileRef]:
    root = Path(repo_root).resolve()
    seen: set[str] = set()
    refs: list[FileRef] = []
    for p in edited_paths:
        if not p:
            continue
        rel = p
        if Path(p).is_absolute():
            try:
                rel = str(Path(p).resolve().relative_to(root))
            except ValueError:
                continue
        if rel in seen or rel not in hunks:
            continue
        seen.add(rel)
        for start, end in hunks[rel]:
            refs.append(FileRef(file=rel, lines=[start, end]))
    return refs
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/traces/test_hunks.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add plumb/traces/hunks.py tests/traces/test_hunks.py
git commit -m "feat(traces): staged hunk parsing and deterministic file_refs"
```

---

## Task 9: Stamp provenance and file_refs in the hook

**Files:**
- Modify: `plumb/git_hook.py:106-155` (`_extract_decisions_from_conversation`)
- Test: `tests/test_git_hook.py` (append)

**Step 1: Write the failing test**

```python
# append to tests/test_git_hook.py
from unittest.mock import patch
from plumb.traces import ToolCall, Turn


def test_extraction_stamps_provenance_and_file_refs(initialized_repo):
    from git import Repo
    from plumb.git_hook import _extract_decisions_from_conversation
    from plumb.config import load_config
    from plumb.programs.decision_extractor import ExtractedDecision

    # stage an edit to src/a.py so hunks exist
    (initialized_repo / "src").mkdir()
    (initialized_repo / "src" / "a.py").write_text("x = 1\n")
    Repo(initialized_repo).index.add(["src/a.py"])

    turns = [
        Turn(agent="codex", session_id="019a", ordinal=4, role="user", content="make x 1"),
        Turn(agent="codex", session_id="019a", ordinal=5, role="assistant", content="done",
             tool_calls=[ToolCall(name="apply_patch", category="Edit", file_path="src/a.py")]),
    ]
    extracted = [ExtractedDecision(question="x?", decision="x is 1", made_by="user", confidence=0.9)]
    with patch("plumb.git_hook.read_conversation", return_value=turns), \
         patch("plumb.programs.configure_dspy"), \
         patch("plumb.git_hook.run_with_retries", return_value=extracted, create=True), \
         patch("plumb.programs.run_with_retries", return_value=extracted):
        decisions = _extract_decisions_from_conversation(initialized_repo, load_config(initialized_repo), "summary")

    d = decisions[0]
    assert (d.agent, d.session_id, d.turn_range) == ("codex", "019a", [4, 5])
    assert d.source_path is None or isinstance(d.source_path, str)
    assert len(d.evidence_digest) == 64
    assert [(r.file, r.lines) for r in d.file_refs] == [("src/a.py", [1, 1])]
    assert d.chunk_index is None
```

If `_extract_decisions_from_conversation` imports `run_with_retries` inside the function (it does today), the `plumb.programs.run_with_retries` patch is the one that matters; keep both, the `create=True` one is harmless.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_git_hook.py -k provenance -v`
Expected: FAIL — `d.agent` is None / file_refs empty

**Step 3: Write the implementation** — replace the loop body in `_extract_decisions_from_conversation` (`plumb/git_hook.py`):

```python
    import hashlib
    from plumb.traces.hunks import staged_hunks, file_refs_for

    repo = Repo(repo_root)
    branch = _get_branch_name(repo)
    hunks = staged_hunks(repo)
    source_paths = {(r.agent, r.session_id): r.path for r in _session_refs}  # see note below

    all_decisions: list[Decision] = []
    for chunk in chunks:
        try:
            extracted = run_with_retries(extractor, chunk.text, diff_summary)
        except Exception:
            continue
        digest = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        edited = [tc.file_path for t in chunk.turns for tc in t.tool_calls
                  if tc.category in ("Edit", "Write") and tc.file_path]
        refs = file_refs_for(edited, hunks, repo_root)
        parent = next((t for t in chunk.turns), None)
        for ed in extracted:
            if not ed.spec_relevant:
                continue
            all_decisions.append(Decision(
                id=generate_decision_id(), status="pending",
                question=ed.question, decision=ed.decision, made_by=ed.made_by,
                branch=branch, confidence=ed.confidence,
                conversation_available=True, created_at=now,
                agent=chunk.agent, session_id=chunk.session_id,
                source_path=source_paths.get((chunk.agent, chunk.session_id)),
                turn_range=[chunk.turn_start, chunk.turn_end],
                evidence_digest=digest, file_refs=refs,
            ))
    return all_decisions
```

`source_path` and `parent_session_id` need the `SessionRef`s, which `read_conversation` currently discards. Simplest: have `read_conversation` return turns but also expose refs — change its signature to `read_conversation(...) -> tuple[list[Turn], dict[tuple[str,str], SessionRef]]`? That ripples. Instead, add a module-level helper in `plumb/conversation.py`:

```python
def read_conversation_with_refs(repo_root, since_commit=None, since_datetime=None):
    from plumb.traces import all_sources
    from plumb.traces.repo import resolve_cutoff
    cutoff = resolve_cutoff(repo_root, since_commit, since_datetime)
    turns, refs = [], {}
    for source in all_sources():
        for ref in source.discover(repo_root, cutoff):
            refs[(ref.agent, ref.session_id)] = ref
            turns.extend(source.parse(ref, cutoff))
    return turns, refs


def read_conversation(repo_root, config_path=None, since_commit=None, since_datetime=None):
    return read_conversation_with_refs(repo_root, since_commit, since_datetime)[0]
```

and in the hook call `turns, refs = read_conversation_with_refs(...)`, then `source_paths = {k: r.path for k, r in refs.items()}` and stamp `parent_session_id=refs[(chunk.agent, chunk.session_id)].parent_session_id` when present. Update the test's patch target to `plumb.git_hook.read_conversation_with_refs` returning `(turns, {("codex","019a"): SessionRef(agent="codex", session_id="019a", path="/tmp/r.jsonl", cwd=str(initialized_repo))})` and assert `d.source_path == "/tmp/r.jsonl"`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_git_hook.py -v` then the full suite.
Expected: new test PASS; baseline unchanged.

**Step 5: Commit**

```bash
git add plumb/git_hook.py plumb/conversation.py tests/test_git_hook.py
git commit -m "feat(hook): stamp agent/session/turn_range/evidence_digest and deterministic file_refs"
```

---

## Task 10: Codex adapter

Real rollout shape (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, one JSON per line, all with top-level `timestamp` and `type`):
- `{"type":"session_meta","payload":{"id":"<uuid>","cwd":"/repo","originator":"codex_cli_rs",...}}`
- `{"type":"turn_context","payload":{"cwd":"/repo","git":{"branch":"main"},...}}` (git may be absent)
- `{"type":"event_msg","payload":{"type":"user_message","message":"..."}}`
- `{"type":"event_msg","payload":{"type":"agent_message","message":"..."}}`
- `{"type":"response_item","payload":{"type":"function_call","name":"shell_command","call_id":"call_1","arguments":"{\"command\":\"ls\"}"}}`
- `{"type":"response_item","payload":{"type":"custom_tool_call","name":"apply_patch","call_id":"call_2","input":"*** Begin Patch\n*** Update File: a.py..."}}`
- `{"type":"response_item","payload":{"type":"function_call_output","call_id":"call_1","output":"..."}}`
- `{"type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"call_2","output":[{"type":"input_text","text":"..."}]}}`
- also `response_item/message` with `role: "developer"|"user"|"assistant"` — ignore `developer`; prefer the `event_msg` user/agent messages and ignore `response_item/message` to avoid duplicates.

Consecutive `function_call`s with no assistant text between them fold into one assistant turn.

**Files:**
- Create: `plumb/traces/codex.py`
- Modify: `plumb/traces/__init__.py` (`all_sources`)
- Test: `tests/traces/test_codex.py`

**Step 1: Write the failing test**

```python
# tests/traces/test_codex.py
import json
from datetime import datetime, timezone
from plumb.traces import SessionRef
from plumb.traces.codex import CodexSource


def _line(kind, payload, ts="2026-06-01T00:00:00Z"):
    return {"timestamp": ts, "type": kind, "payload": payload}


def _rollout(tmp_path, cwd, sid="01a0"):
    d = tmp_path / "sessions" / "2026" / "06" / "01"
    d.mkdir(parents=True)
    f = d / f"rollout-2026-06-01T00-00-00-{sid}.jsonl"
    lines = [
        _line("session_meta", {"id": sid, "cwd": cwd}),
        _line("turn_context", {"cwd": cwd, "git": {"branch": "feat"}}),
        _line("event_msg", {"type": "user_message", "message": "add x"}),
        _line("response_item", {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "ctx"}]}),
        _line("response_item", {"type": "function_call", "name": "shell_command", "call_id": "c1",
                                "arguments": json.dumps({"command": "pytest -x"})}),
        _line("response_item", {"type": "function_call_output", "call_id": "c1", "output": "3 passed"}),
        _line("event_msg", {"type": "agent_message", "message": "Patching."}),
        _line("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2",
                                "input": "*** Begin Patch\n*** Update File: src/x.py\n@@\n+1\n*** End Patch"}),
        _line("response_item", {"type": "custom_tool_call_output", "call_id": "c2",
                                "output": [{"type": "input_text", "text": "Done"}]}),
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return f


def test_discover_by_session_meta_cwd(tmp_repo, tmp_path):
    f = _rollout(tmp_path, str(tmp_repo))
    _rollout(tmp_path, str(tmp_path / "other"), sid="zz")
    refs = CodexSource(root=tmp_path / "sessions").discover(tmp_repo, None)
    assert [(r.session_id, r.path, r.branch) for r in refs] == [("01a0", str(f), "feat")]


def test_parse_codex_turns(tmp_repo, tmp_path):
    f = _rollout(tmp_path, str(tmp_repo))
    ref = SessionRef(agent="codex", session_id="01a0", path=str(f), cwd=str(tmp_repo))
    turns = CodexSource().parse(ref, None)
    assert [t.role for t in turns] == ["user", "assistant", "assistant"]
    assert turns[0].content == "add x"
    first = turns[1].tool_calls[0]
    assert (first.name, first.category, first.input_summary, first.result_summary) == \
        ("shell_command", "Bash", "pytest -x", "3 passed")
    assert turns[2].content == "Patching."
    patch = turns[2].tool_calls[0]
    assert (patch.category, patch.file_path, patch.result_summary) == ("Edit", "src/x.py", "Done")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/traces/test_codex.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# plumb/traces/codex.py
"""Codex adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (+ archived_sessions/)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from plumb.traces import SessionRef, ToolCall, Turn
from plumb.traces.jsonl import iter_jsonl, sniff_head
from plumb.traces.repo import after_cutoff, same_repo
from plumb.traces.taxonomy import categorize, file_path_from_input, input_summary

RESULT_LIMIT = 200


def _output_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return " ".join(o.get("text", "") for o in output if isinstance(o, dict))
    return ""


class CodexSource:
    name = "codex"

    def __init__(self, root: Optional[Path] = None, archived: Optional[Path] = None):
        home = Path.home() / ".codex"
        self.roots = [root or home / "sessions", archived or home / "archived_sessions"]

    def _candidates(self):
        for root in self.roots:
            if root.is_dir():
                yield from root.rglob("rollout-*.jsonl")

    def discover(self, repo_root, since: Optional[datetime]) -> list[SessionRef]:
        refs = []
        cutoff_ts = since.timestamp() if since else None
        for path in self._candidates():
            try:
                if cutoff_ts is not None and path.stat().st_mtime < cutoff_ts:
                    continue
            except OSError:
                continue
            meta = sniff_head(path, lambda e: e.get("payload") if e.get("type") == "session_meta" else None)
            if not meta or not same_repo(meta.get("cwd", ""), repo_root):
                continue
            branch = sniff_head(path, lambda e: (e.get("payload") or {}).get("git", {}).get("branch")
                                if e.get("type") == "turn_context" else None)
            refs.append(SessionRef(agent=self.name, session_id=meta.get("id") or path.stem,
                                   path=str(path), cwd=meta["cwd"], branch=branch))
        refs.sort(key=lambda r: Path(r.path).stat().st_mtime)
        return refs

    def parse(self, ref: SessionRef, since: Optional[datetime]) -> list[Turn]:
        turns: list[Turn] = []
        by_call: dict[str, ToolCall] = {}
        ordinal = 0
        pending_text: list[str] = []
        pending_calls: list[ToolCall] = []
        pending_ts: Optional[str] = None

        def flush():
            nonlocal ordinal, pending_text, pending_calls, pending_ts
            if pending_text or pending_calls:
                turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                  role="assistant", content="\n".join(pending_text),
                                  timestamp=pending_ts, tool_calls=pending_calls))
                ordinal += 1
            pending_text, pending_calls, pending_ts = [], [], None

        for e in iter_jsonl(Path(ref.path)):
            kind, p, ts = e.get("type"), e.get("payload") or {}, e.get("timestamp")
            ptype = p.get("type")
            if kind == "event_msg" and ptype == "user_message":
                flush()
                msg = (p.get("message") or "").strip()
                if msg:
                    turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                      role="user", content=msg, timestamp=ts))
                    ordinal += 1
            elif kind == "event_msg" and ptype == "agent_message":
                if pending_calls:
                    flush()
                pending_text.append(p.get("message") or "")
                pending_ts = pending_ts or ts
            elif kind == "response_item" and ptype in ("function_call", "custom_tool_call"):
                name = p.get("name", "unknown")
                arg = p.get("arguments") if ptype == "function_call" else p.get("input")
                tc = ToolCall(name=name, category=categorize(name),
                              file_path=file_path_from_input(name, arg),
                              input_summary=input_summary(name, arg), tool_use_id=p.get("call_id"))
                pending_calls.append(tc)
                pending_ts = pending_ts or ts
                if tc.tool_use_id:
                    by_call[tc.tool_use_id] = tc
            elif kind == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
                tc = by_call.get(p.get("call_id", ""))
                if tc is not None:
                    tc.result_summary = _output_text(p.get("output")).strip()[:RESULT_LIMIT] or None
        flush()
        return [t for t in turns if after_cutoff(t.timestamp, since)]
```

Register it: in `plumb/traces/__init__.py`, `all_sources()` returns `[ClaudeSource(), CodexSource()]`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/traces/test_codex.py -v`
Expected: all PASS. Manual check against a real file: `uv run python -c "from plumb.traces.codex import CodexSource; from pathlib import Path; print(len(CodexSource().discover(Path('.'), None)))"` — should not raise.

**Step 5: Commit**

```bash
git add plumb/traces/codex.py plumb/traces/__init__.py tests/traces/test_codex.py
git commit -m "feat(traces): Codex rollout adapter"
```

---

## Task 11: Pi adapter (active-ancestry walk)

Real shape (`~/.pi/agent/sessions/<encoded-project>/<timestamp>_<uuid>.jsonl`):
- line 1: `{"type":"session","version":3,"id":"<uuid>","timestamp":"...","cwd":"/repo"}` (may carry `branchedFrom`)
- entries: `{"type":"message","id":"49813f1b","parentId":"05c65627","timestamp":"...","message":{"role":"user","content":[{"type":"text","text":"..."}]}}`
- assistant content blocks: `text`, `thinking`, `toolCall {id,name,arguments}`
- tool results: `{"type":"message","message":{"role":"toolResult","toolCallId":"...","toolName":"read","content":[{"type":"text","text":"..."}],"isError":false}}`
- other types (`model_change`, `thinking_level_change`, `compaction`) are chain nodes too — they carry `id`/`parentId` and must be walked through, just not emitted.
- Subagents: `<project>/<session>/<agent>.jsonl` next to `<project>/<session>.jsonl`.

The conversation is the **active ancestry**: start at the last entry that has an `id`, follow `parentId` to the root, reverse.

**Files:**
- Create: `plumb/traces/pi.py`
- Modify: `plumb/traces/__init__.py`
- Test: `tests/traces/test_pi.py`

**Step 1: Write the failing test**

```python
# tests/traces/test_pi.py
import json
from plumb.traces import SessionRef
from plumb.traces.pi import PiSource


def _msg(id, parent, role, content, ts="2026-06-01T00:00:00Z", **extra):
    m = {"role": role, "content": content}
    m.update(extra)
    return {"type": "message", "id": id, "parentId": parent, "timestamp": ts, "message": m}


def _session(tmp_path, cwd, sid="abc", extra_lines=None):
    d = tmp_path / "sessions" / "--enc--"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"2026-06-01T00-00-00-000Z_{sid}.jsonl"
    lines = [{"type": "session", "version": 3, "id": sid, "timestamp": "2026-06-01T00:00:00Z", "cwd": cwd},
             {"type": "model_change", "id": "m0", "parentId": None, "timestamp": "2026-06-01T00:00:00Z"},
             _msg("u1", "m0", "user", [{"type": "text", "text": "hello"}]),
             _msg("a1", "u1", "assistant", [{"type": "text", "text": "hi"},
                                             {"type": "toolCall", "id": "t1", "name": "read", "arguments": {"path": "a.py"}}]),
             _msg("r1", "a1", "toolResult", [{"type": "text", "text": "file body"}], toolCallId="t1", toolName="read"),
             # abandoned branch (rewound): child of u1 that nothing continues from
             _msg("dead", "u1", "assistant", [{"type": "text", "text": "WRONG BRANCH"}]),
             _msg("u2", "r1", "user", [{"type": "text", "text": "thanks"}]),
             ] + (extra_lines or [])
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return f


def test_discover_by_header_cwd(tmp_repo, tmp_path):
    f = _session(tmp_path, str(tmp_repo))
    _session(tmp_path, "/elsewhere", sid="zzz")
    refs = PiSource(root=tmp_path / "sessions").discover(tmp_repo, None)
    assert [(r.session_id, r.path) for r in refs] == [("abc", str(f))]


def test_discover_subagent_gets_parent(tmp_repo, tmp_path):
    _session(tmp_path, str(tmp_repo))
    sub = tmp_path / "sessions" / "--enc--" / "2026-06-01T00-00-00-000Z_abc" / "worker.jsonl"
    sub.parent.mkdir()
    sub.write_text(json.dumps({"type": "session", "version": 3, "id": "w1", "cwd": str(tmp_repo)}) + "\n")
    refs = {r.session_id: r for r in PiSource(root=tmp_path / "sessions").discover(tmp_repo, None)}
    assert refs["w1"].parent_session_id == "abc"


def test_parse_walks_active_ancestry_only(tmp_repo, tmp_path):
    f = _session(tmp_path, str(tmp_repo))
    ref = SessionRef(agent="pi", session_id="abc", path=str(f), cwd=str(tmp_repo))
    turns = PiSource().parse(ref, None)
    assert [t.content for t in turns] == ["hello", "hi", "thanks"]
    assert "WRONG BRANCH" not in [t.content for t in turns]
    tc = turns[1].tool_calls[0]
    assert (tc.name, tc.category, tc.file_path, tc.result_summary) == ("read", "Read", "a.py", "file body")
    assert [t.ordinal for t in turns] == [0, 1, 2]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/traces/test_pi.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# plumb/traces/pi.py
"""Pi adapter: ~/.pi/agent/sessions/<project>/<session>.jsonl, tree-structured entries."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from plumb.traces import SessionRef, ToolCall, Turn
from plumb.traces.jsonl import iter_jsonl
from plumb.traces.repo import after_cutoff, same_repo
from plumb.traces.taxonomy import categorize, file_path_from_input, input_summary

RESULT_LIMIT = 200


def _text(blocks) -> str:
    if isinstance(blocks, str):
        return blocks
    return "\n".join(b.get("text", "") for b in blocks or [] if isinstance(b, dict) and b.get("type") == "text")


class PiSource:
    name = "pi"

    def __init__(self, root: Optional[Path] = None):
        self.root = root or Path.home() / ".pi" / "agent" / "sessions"

    def _candidates(self):
        if not self.root.is_dir():
            return
        for proj in self.root.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                yield f, None
            for sub in proj.glob("*/**/*.jsonl"):
                # <project>/<session-stem>/<agent>.jsonl -> parent is the session file's stem
                parent_stem = sub.relative_to(proj).parts[0]
                yield sub, parent_stem

    def _header(self, path: Path) -> Optional[dict]:
        for e in iter_jsonl(path):
            return e if e.get("type") == "session" else None
        return None

    def discover(self, repo_root, since: Optional[datetime]) -> list[SessionRef]:
        refs = []
        cutoff_ts = since.timestamp() if since else None
        stem_to_id: dict[str, str] = {}
        pending = []
        for path, parent_stem in self._candidates():
            try:
                if cutoff_ts is not None and path.stat().st_mtime < cutoff_ts:
                    continue
            except OSError:
                continue
            head = self._header(path)
            if not head or not same_repo(head.get("cwd", ""), repo_root):
                continue
            sid = head.get("id") or path.stem
            if parent_stem is None:
                stem_to_id[path.stem] = sid
            pending.append((path, head, sid, parent_stem))
        for path, head, sid, parent_stem in pending:
            parent_id = stem_to_id.get(parent_stem) if parent_stem else head.get("branchedFrom")
            refs.append(SessionRef(agent=self.name, session_id=sid, path=str(path),
                                   cwd=head["cwd"], parent_session_id=parent_id))
        refs.sort(key=lambda r: Path(r.path).stat().st_mtime)
        return refs

    def parse(self, ref: SessionRef, since: Optional[datetime]) -> list[Turn]:
        nodes: dict[str, dict] = {}
        last_id = None
        for e in iter_jsonl(Path(ref.path)):
            if e.get("id"):
                nodes[e["id"]] = e
                last_id = e["id"]
        # active ancestry: last entry -> root
        chain = []
        cur = last_id
        while cur is not None and cur in nodes:
            chain.append(nodes[cur])
            cur = nodes[cur].get("parentId")
        chain.reverse()

        turns: list[Turn] = []
        by_call: dict[str, ToolCall] = {}
        ordinal = 0
        for e in chain:
            if e.get("type") != "message":
                continue
            m = e.get("message") or {}
            role, content, ts = m.get("role"), m.get("content"), e.get("timestamp")
            if role == "user":
                text = _text(content).strip()
                if text:
                    turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                      role="user", content=text, timestamp=ts))
                    ordinal += 1
            elif role == "assistant":
                calls = []
                for b in content or []:
                    if isinstance(b, dict) and b.get("type") == "toolCall":
                        name = b.get("name", "unknown")
                        tc = ToolCall(name=name, category=categorize(name),
                                      file_path=file_path_from_input(name, b.get("arguments")),
                                      input_summary=input_summary(name, b.get("arguments")),
                                      tool_use_id=b.get("id"))
                        calls.append(tc)
                        if tc.tool_use_id:
                            by_call[tc.tool_use_id] = tc
                text = _text(content)
                if text or calls:
                    turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                      role="assistant", content=text, timestamp=ts, tool_calls=calls))
                    ordinal += 1
            elif role == "toolResult":
                tc = by_call.get(m.get("toolCallId", ""))
                if tc is not None:
                    text = _text(content).strip()
                    if m.get("isError"):
                        text = "ERROR: " + text
                    tc.result_summary = text[:RESULT_LIMIT] or None
        return [t for t in turns if after_cutoff(t.timestamp, since)]
```

Register `PiSource()` in `all_sources()`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/traces/test_pi.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add plumb/traces/pi.py plumb/traces/__init__.py tests/traces/test_pi.py
git commit -m "feat(traces): Pi adapter walking the active ancestry"
```

---

## Task 12: Copilot CLI adapter

**No Copilot sessions exist on the development machine**, so this adapter is written from agentsview's parser (`internal/parser/copilot.go`) and must be verified against a real `~/.copilot/session-state/<uuid>/events.jsonl` before it is trusted. Event shape per agentsview:
- `{"type":"session.start","timestamp":"...","data":{"sessionId":"...","context":{"cwd":"/repo","branch":"main"}}}`
- `{"type":"user.message","data":{"content":"...","source":"user"}}` — skip when `source` starts with `skill-` or content starts with `<skill-context`
- `{"type":"assistant.message","data":{"content":"...","toolRequests":[{"toolCallId":"x","name":"edit_file","arguments":{...}}]}}`
- `{"type":"tool.execution_complete","data":{"toolCallId":"x","result":{"content":"..."}}}` — result field name is **unverified**; read both `data.result.content` and `data.output` and take the first string.
- Layout: `<root>/session-state/<uuid>/events.jsonl` or bare `<uuid>.jsonl`.

**Files:**
- Create: `plumb/traces/copilot.py`
- Modify: `plumb/traces/__init__.py`
- Test: `tests/traces/test_copilot.py`

**Step 1: Write the failing test** — same pattern as Codex: a `_events()` helper writing `session.start`, one `user.message`, one `assistant.message` with a `toolRequests` `edit_file` on `src/x.py`, and a `tool.execution_complete` with `data.result.content = "ok"`. Assert discover matches by `data.context.cwd`, `branch == "main"`, and parse yields `["user","assistant"]` with the `Edit` call carrying `file_path="src/x.py"` and `result_summary="ok"`. Add a test that a `user.message` with `source: "skill-foo"` is skipped.

**Step 2: Run test to verify it fails** — `ModuleNotFoundError`.

**Step 3: Write minimal implementation** — mirror `codex.py`: `root = ~/.copilot/session-state`; candidates are `root/*/events.jsonl` and `root/*.jsonl`; `session_id` from `data.sessionId` or the directory/file stem; discovery sniffs `session.start`; parsing dispatches on `type`, folds `toolRequests` into the assistant turn, and attaches results by `toolCallId`. Register `CopilotSource()` in `all_sources()`.

**Step 4: Run tests** — `uv run pytest tests/traces/test_copilot.py -v` → PASS.

**Step 5: Commit**

```bash
git add plumb/traces/copilot.py plumb/traces/__init__.py tests/traces/test_copilot.py
git commit -m "feat(traces): Copilot CLI adapter (unverified against live sessions)"
```

Add a one-line note to `docs/plans/2026-08-29-multi-agent-traces-design.md` under the agents table: "Copilot adapter written from agentsview's parser; verify against a real events.jsonl before relying on it."

---

## Task 13: `plumb log` (grouped by commit, then agent) with `--verify`

**Files:**
- Create: `plumb/log_view.py`
- Modify: `plumb/cli.py` (new command, next to `status`)
- Test: `tests/test_log_view.py`

**Step 1: Write the failing test**

```python
# tests/test_log_view.py
import hashlib, json
from plumb.decision_log import Decision
from plumb.log_view import group_decisions, verify_evidence


def _d(id, sha, agent, **kw):
    return Decision(id=id, status="approved", decision=f"d{id}", commit_sha=sha, agent=agent, **kw)


def test_group_by_commit_then_agent():
    ds = [_d("1", "aaa", "claude"), _d("2", "aaa", "codex"), _d("3", None, "pi"), _d("4", "aaa", "claude")]
    grouped = group_decisions(ds)
    assert list(grouped.keys()) == ["uncommitted", "aaa"]          # uncommitted first
    assert [d.id for d in grouped["aaa"]["claude"]] == ["1", "4"]
    assert list(grouped["aaa"].keys()) == ["claude", "codex"]


def test_verify_evidence_ok_and_stale(tmp_path, monkeypatch):
    from plumb.traces import Turn
    turns = [Turn(agent="claude", session_id="S", ordinal=i, role="user", content=f"t{i}") for i in range(3)]
    from plumb.conversation import chunk_conversation
    chunk = chunk_conversation(turns[:2])[0]           # turns 0-1 in one chunk
    digest = hashlib.sha256(chunk.text.encode()).hexdigest()
    good = _d("1", "aaa", "claude", session_id="S", source_path="/x.jsonl", turn_range=[0, 1], evidence_digest=digest)
    bad = _d("2", "aaa", "claude", session_id="S", source_path="/x.jsonl", turn_range=[0, 1], evidence_digest="0" * 64)

    class FakeSource:
        name = "claude"
        def parse(self, ref, since): return turns
    monkeypatch.setattr("plumb.log_view._source_for", lambda agent: FakeSource())

    assert verify_evidence(good) == "ok"
    assert verify_evidence(bad) == "stale"
    assert verify_evidence(_d("3", "aaa", "claude")) == "unverifiable"
```

**Step 2: Run test to verify it fails** — `ModuleNotFoundError`.

**Step 3: Write minimal implementation**

```python
# plumb/log_view.py
"""Grouping and evidence verification behind `plumb log`."""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Optional

from plumb.decision_log import Decision


def group_decisions(decisions: list[Decision]) -> "OrderedDict[str, OrderedDict[str, list[Decision]]]":
    """{commit_sha | 'uncommitted': {agent | 'unknown': [decisions]}}, uncommitted first, newest commit first."""
    by_commit: dict[str, list[Decision]] = {}
    for d in decisions:
        by_commit.setdefault(d.commit_sha or "uncommitted", []).append(d)
    ordered = OrderedDict()
    keys = sorted(by_commit, key=lambda k: (k != "uncommitted", -max((x.created_at or "") for x in by_commit[k]) and 0))
    # simple, stable: uncommitted first, then by latest created_at desc
    rest = [k for k in by_commit if k != "uncommitted"]
    rest.sort(key=lambda k: max((x.created_at or "") for x in by_commit[k]), reverse=True)
    for k in (["uncommitted"] if "uncommitted" in by_commit else []) + rest:
        agents: "OrderedDict[str, list[Decision]]" = OrderedDict()
        for d in by_commit[k]:
            agents.setdefault(d.agent or "unknown", []).append(d)
        ordered[k] = OrderedDict(sorted(agents.items()))
    return ordered


def _source_for(agent: str):
    from plumb.traces import all_sources
    return next((s for s in all_sources() if s.name == agent), None)


def verify_evidence(d: Decision) -> str:
    """'ok' | 'stale' | 'unverifiable' — re-renders the decision's turn range and compares digests."""
    if not (d.agent and d.session_id and d.source_path and d.turn_range and d.evidence_digest):
        return "unverifiable"
    source = _source_for(d.agent)
    if source is None:
        return "unverifiable"
    from plumb.conversation import chunk_conversation
    from plumb.traces import SessionRef
    ref = SessionRef(agent=d.agent, session_id=d.session_id, path=d.source_path, cwd="")
    try:
        turns = source.parse(ref, None)
    except Exception:
        return "unverifiable"
    start, end = d.turn_range
    window = [t for t in turns if start <= t.ordinal <= end]
    if not window:
        return "stale"
    for chunk in chunk_conversation(window):
        if hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() == d.evidence_digest:
            return "ok"
    return "stale"
```

Clean up `group_decisions` — the `keys = sorted(...)` line is dead; delete it before committing (the test will still pass; the linter won't). Then the CLI command in `plumb/cli.py`:

```python
@cli.command(name="log")
@click.option("--since", "since_ref", default=None, help="Only commits reachable from <ref>..HEAD")
@click.option("--verify", is_flag=True, help="Re-check evidence digests against transcripts")
def log_cmd(since_ref, verify):
    """Decisions grouped by commit, then by agent."""
    from git import Repo
    from plumb.log_view import group_decisions, verify_evidence
    from plumb.decision_log import read_all_decisions, update_decision_status, find_decision_branch

    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]"); raise SystemExit(1)
    decisions = [d for d in read_all_decisions(repo_root) if d.status != "ignored"]
    if since_ref:
        shas = {c.hexsha for c in Repo(repo_root).iter_commits(f"{since_ref}..HEAD")}
        decisions = [d for d in decisions if d.commit_sha is None or d.commit_sha in shas]
    for commit, agents in group_decisions(decisions).items():
        console.print(f"[bold]{commit[:12] if commit != 'uncommitted' else 'uncommitted'}[/bold]")
        for agent, ds in agents.items():
            console.print(f"  [cyan]{agent}[/cyan] ({len(ds)})")
            for d in ds:
                rng = f"turns {d.turn_range[0]}-{d.turn_range[1]}" if d.turn_range else ""
                sess = f"session {d.session_id[:8]}" if d.session_id else ""
                flag = ""
                if verify:
                    result = verify_evidence(d)
                    flag = f" [{ 'green' if result == 'ok' else 'yellow'}]{result}[/]"
                    if result == "stale" and d.ref_status != "stale":
                        update_decision_status(repo_root, d.id, branch=find_decision_branch(repo_root, d.id), ref_status="stale")
                console.print(f"    {d.id}  {d.status:<9} {d.made_by or '-':<6} {d.confidence or '-'}  {sess} {rng}{flag}")
                console.print(f"      {d.decision}")
```

Check `update_decision_status`'s accepted kwargs in `decision_log.py`; if it doesn't accept `ref_status`, add it (it uses `model_copy(update=...)` on the latest line and appends).

**Step 4: Run tests** — `uv run pytest tests/test_log_view.py -v` → PASS; `uv run plumb log | head` prints groups.

**Step 5: Commit**

```bash
git add plumb/log_view.py plumb/cli.py tests/test_log_view.py
git commit -m "feat(cli): plumb log grouped by commit and agent, with --since and --verify"
```

---

## Task 14: `AGENTS.md` gets the same instruction block

**Files:**
- Modify: `plumb/cli.py:305-345` (`_update_claude_md`)
- Test: `tests/test_cli.py` (append)

**Step 1: Write the failing test**

```python
# append to tests/test_cli.py
def test_update_claude_md_writes_agents_md_too(tmp_repo):
    from plumb.cli import _update_claude_md
    from plumb.config import PlumbConfig
    (tmp_repo / "AGENTS.md").write_text("# Existing\n")
    _update_claude_md(tmp_repo, PlumbConfig(spec_paths=["spec.md"], test_paths=["tests/"]))
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (tmp_repo / name).read_text()
        assert "<!-- plumb:start -->" in text and "<!-- plumb:end -->" in text
    assert (tmp_repo / "AGENTS.md").read_text().startswith("# Existing\n")
    # idempotent
    _update_claude_md(tmp_repo, PlumbConfig(spec_paths=["spec.md"], test_paths=["tests/"]))
    assert (tmp_repo / "AGENTS.md").read_text().count("plumb:start") == 1
```

**Step 2: Run test to verify it fails** — AGENTS.md lacks the block.

**Step 3: Write minimal implementation** — refactor `_update_claude_md` so the block-building and the read/replace/append logic live in `_write_block(path: Path, block: str)`, then call it for both `repo_root / "CLAUDE.md"` and `repo_root / "AGENTS.md"`. Keep the block text identical; the record-mode design will change its wording later.

**Step 4: Run tests** — `uv run pytest tests/test_cli.py -v` → PASS.

**Step 5: Commit**

```bash
git add plumb/cli.py tests/test_cli.py
git commit -m "feat(init): write the Plumb instruction block to AGENTS.md as well as CLAUDE.md"
```

---

## Task 15: Spec and docs sync

**Files:**
- Modify: `plumb_spec.md` — the "Conversation Capture"/session-reading section (search for `encode_project_path`, `~/.claude/projects`, `[tool:`) to describe TraceSource discovery by `cwd`, the four agents, the taxonomy, per-session chunking, and the provenance fields on the Decision schema JSON (add `agent`, `session_id`, `parent_session_id`, `source_path`, `turn_range`, `evidence_digest`; mark `chunk_index` deprecated).
- Modify: `README.md` — the "How It Works" section: mention Codex/Pi/Copilot and `plumb log`.
- Modify: `docs/plans/2026-08-29-multi-agent-traces-design.md` — set **Status:** Implemented (stages 1–2).

**Step 1–4:** No code. Run the full suite once more: `uv run pytest tests/ -q --ignore=tests/test_generated.py` → 12 pre-existing failures at most.

**Step 5: Commit**

```bash
git add plumb_spec.md README.md docs/plans/2026-08-29-multi-agent-traces-design.md
git commit -m "docs: spec and README for multi-agent traces"
```

---

## Out of scope (tracked, not in this plan)

- Record mode (stage 3) — `docs/plans/2026-08-29-record-mode-design.md`.
- Codex `spawn_agent` child rollouts as subagent sessions.
- `git blame` line resolution and `git patch-id` re-mapping (design doc, "Line attribution").
- Re-enabling the pre-commit hook (`.git/hooks/pre-commit.disabled`) — the user's call once record mode lands.
- Repair the two pre-existing `tests/test_integration.py` amend/gate failures (now meaningful since `commit_sha` is stamped).
- Regenerate `tests/test_generated.py` (SyntaxError at line 4901; 14 references to removed APIs).
- Constraining `made_by` to `Literal["user", "agent"]` in `ExtractedDecision` (Haiku emitted `"assistant"` once in testing).
