"""Pi adapter: ~/.pi/agent/sessions/<project>/<session>.jsonl, tree-structured entries.

Entries carry id/parentId; a rewind leaves abandoned branches in the file.
The conversation is the active ancestry from the last entry to the root.
Subagent sessions live in <project>/<session-stem>/<agent>.jsonl.

`compaction` nodes stay in the chain and pre-compaction messages are kept, so
ordinals remain stable for provenance; the compaction `summary` is never emitted as a turn.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from plumb.traces import SessionRef, ToolCall, Turn
from plumb.traces.jsonl import iter_jsonl, sniff_head
from plumb.traces.repo import after_cutoff, same_repo
from plumb.traces.taxonomy import categorize, file_path_from_input, file_paths_from_input, input_summary

RESULT_LIMIT = 200


def _text(blocks) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")


def _session_header(e: dict):
    return e if e.get("type") == "session" and e.get("cwd") else None


class PiSource:
    name = "pi"

    def __init__(self, root: Optional[Path] = None):
        self.root = root or Path.home() / ".pi" / "agent" / "sessions"

    # -- discovery -----------------------------------------------------------

    def _candidates(self):
        """Yield (path, parent_stem). Top-level: <project>/<stem>.jsonl -> None.
        Subagent: <project>/<stem>/**/<agent>.jsonl -> <stem>."""
        if not self.root.is_dir():
            return
        for proj in self.root.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                yield f, None
            for sub in proj.glob("*/**/*.jsonl"):
                yield sub, sub.relative_to(proj).parts[0]

    def discover(self, repo_root, since: Optional[datetime]) -> list[SessionRef]:
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff_ts = since.timestamp() if since else None
        stem_to_id: dict[str, str] = {}
        pending: list[tuple[float, Path, dict, str, Optional[str]]] = []
        for path, parent_stem in self._candidates():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if cutoff_ts is not None and mtime < cutoff_ts:
                continue
            head = sniff_head(path, _session_header)
            if not isinstance(head, dict) or not same_repo(head.get("cwd", ""), repo_root):
                continue
            sid = head.get("id") or path.stem
            if parent_stem is None:
                stem_to_id[path.stem] = sid
            pending.append((mtime, path, head, sid, parent_stem))
        # Second pass: subagent parents resolve via the enclosing session's stem,
        # which may have been discovered after the child.
        found: list[tuple[float, SessionRef]] = []
        for mtime, path, head, sid, parent_stem in pending:
            if parent_stem is not None:
                parent_id = stem_to_id.get(parent_stem)
            else:
                parent_id = head.get("branchedFrom") or None
            found.append((mtime, SessionRef(
                agent=self.name,
                session_id=sid,
                path=str(path),
                cwd=head["cwd"],
                parent_session_id=parent_id,
            )))
        found.sort(key=lambda pair: pair[0])
        return [ref for _, ref in found]

    # -- parsing -------------------------------------------------------------

    def _active_chain(self, path: Path) -> list[dict]:
        """Entries on the path from the last id-bearing entry up to the root, root first."""
        nodes: dict[str, dict] = {}
        last_id: Optional[str] = None
        for e in iter_jsonl(path):
            if e.get("type") == "session":
                continue  # the header carries an id, but nothing points at it
            node_id = e.get("id")
            if isinstance(node_id, str) and node_id:
                nodes[node_id] = e
                last_id = node_id
        chain: list[dict] = []
        cur = last_id
        seen: set[str] = set()  # a malformed parentId cycle must not hang
        while cur is not None and cur in nodes and cur not in seen:
            seen.add(cur)
            chain.append(nodes[cur])
            cur = nodes[cur].get("parentId")
        chain.reverse()
        return chain

    def parse(self, ref: SessionRef, since: Optional[datetime]) -> list[Turn]:
        turns: list[Turn] = []
        by_call: dict[str, ToolCall] = {}
        ordinal = 0
        for e in self._active_chain(Path(ref.path)):
            if e.get("type") != "message":
                continue
            m = e.get("message")
            if not isinstance(m, dict):
                continue
            role, content, ts = m.get("role"), m.get("content"), e.get("timestamp")
            if role == "user":
                text = _text(content).strip()
                if text:
                    turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                      role="user", content=text, timestamp=ts))
                    ordinal += 1
            elif role == "assistant":
                calls: list[ToolCall] = []
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "toolCall":
                        name = b.get("name") or "unknown"
                        arg = b.get("arguments")
                        tc = ToolCall(name=name, category=categorize(name),
                                      file_path=file_path_from_input(name, arg),
                                      file_paths=file_paths_from_input(name, arg),
                                      input_summary=input_summary(name, arg),
                                      # Pi ids like `write:28` recur within a session; results always
                                      # immediately follow their call.
                                      tool_use_id=b.get("id"))
                        calls.append(tc)
                        if tc.tool_use_id:
                            by_call[tc.tool_use_id] = tc
                text = _text(content).strip()
                if text or calls:
                    turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                      role="assistant", content=text, timestamp=ts, tool_calls=calls))
                    ordinal += 1
            elif role == "toolResult":
                tc = by_call.get(m.get("toolCallId") or "")
                if tc is not None:
                    text = _text(content).strip()
                    if m.get("isError"):
                        text = "ERROR: " + text
                    tc.result_summary = text[:RESULT_LIMIT] or None
        # Turns without a timestamp are deliberately kept (see claude.py).
        return [t for t in turns if after_cutoff(t.timestamp, since)]
