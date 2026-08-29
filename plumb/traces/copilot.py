"""Copilot CLI adapter: ~/.copilot/session-state/<uuid>/events.jsonl (or bare <uuid>.jsonl).

UNVERIFIED: this adapter was written from agentsview's Go parser
(internal/parser/copilot.go) and its test fixtures, not from real Copilot CLI
sessions. Check it against a live events.jsonl before relying on it.

Assumed event shapes (one JSON object per line: {"type", "timestamp", "data"}):
- session.start: data.sessionId, data.context.cwd, data.context.branch
- user.message: data.content, data.source; skipped when source starts with
  "skill-" or content starts with "<skill-context"
- assistant.message: data.content, data.reasoningText (ignored),
  data.toolRequests[] of {toolCallId, name, arguments}; arguments is either a
  JSON string or an object
- tool.execution_start: ignored
- tool.execution_complete: data.toolCallId, data.success (bool), data.result
  (string, or any JSON value)
- assistant.reasoning, session.model_change, session.shutdown: ignored

Layout: when both <uuid>/events.jsonl and <uuid>.jsonl exist the directory
form wins and the bare file is dropped, matching agentsview.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from plumb.traces import SessionRef, ToolCall, Turn
from plumb.traces.jsonl import iter_jsonl, sniff_head
from plumb.traces.repo import after_cutoff, same_repo
from plumb.traces.taxonomy import categorize, file_path_from_input, file_paths_from_input, input_summary

RESULT_LIMIT = 200


def _result_text(result) -> str:
    # agentsview takes `data.result` as a string, or its raw JSON otherwise.
    # Real sessions may nest the text (e.g. result.content); revisit with live data.
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    return json.dumps(result)


def _session_start(e: dict):
    return e.get("data") if e.get("type") == "session.start" else None


def _is_skill_message(data: dict, content: str) -> bool:
    source = data.get("source")
    if isinstance(source, str) and source.strip().startswith("skill-"):
        return True
    return content.startswith("<skill-context")


class CopilotSource:
    name = "copilot"

    def __init__(self, root: Optional[Path] = None):
        self.root = root or Path.home() / ".copilot" / "session-state"

    # -- discovery -----------------------------------------------------------

    def _candidates(self):
        """Yield (path, uuid). Directory form <uuid>/events.jsonl first, then bare
        <uuid>.jsonl for uuids without a directory form."""
        if not self.root.is_dir():
            return
        seen: set[str] = set()
        for entry in self.root.iterdir():
            if entry.is_dir():
                events = entry / "events.jsonl"
                if events.is_file():
                    seen.add(entry.name)
                    yield events, entry.name
        for entry in self.root.glob("*.jsonl"):
            if entry.is_file() and entry.stem not in seen:
                yield entry, entry.stem

    def discover(self, repo_root, since: Optional[datetime]) -> list[SessionRef]:
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff_ts = since.timestamp() if since else None
        found: list[tuple[float, SessionRef]] = []
        for path, uuid in self._candidates():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if cutoff_ts is not None and mtime < cutoff_ts:
                continue
            start = sniff_head(path, _session_start)
            if not isinstance(start, dict):
                continue
            ctx = start.get("context")
            if not isinstance(ctx, dict) or not same_repo(ctx.get("cwd", ""), repo_root):
                continue
            found.append((mtime, SessionRef(
                agent=self.name,
                session_id=start.get("sessionId") or uuid,
                path=str(path),
                cwd=ctx["cwd"],
                branch=ctx.get("branch") or None,
            )))
        found.sort(key=lambda pair: pair[0])
        return [ref for _, ref in found]

    # -- parsing -------------------------------------------------------------

    def parse(self, ref: SessionRef, since: Optional[datetime]) -> list[Turn]:
        turns: list[Turn] = []
        by_call: dict[str, ToolCall] = {}
        ordinal = 0
        for e in iter_jsonl(Path(ref.path)):
            kind, ts = e.get("type"), e.get("timestamp")
            data = e.get("data")
            if not isinstance(data, dict):
                continue
            if kind == "user.message":
                content = data.get("content")
                text = content.strip() if isinstance(content, str) else ""
                if not text or _is_skill_message(data, text):
                    continue
                turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                  role="user", content=text, timestamp=ts))
                ordinal += 1
            elif kind == "assistant.message":
                calls: list[ToolCall] = []
                requests = data.get("toolRequests")
                for req in requests if isinstance(requests, list) else []:
                    if not isinstance(req, dict) or not req.get("name"):
                        continue
                    name = req["name"]
                    arg = req.get("arguments")
                    tc = ToolCall(name=name, category=categorize(name),
                                  file_path=file_path_from_input(name, arg),
                                  file_paths=file_paths_from_input(name, arg),
                                  input_summary=input_summary(name, arg),
                                  tool_use_id=req.get("toolCallId"))
                    calls.append(tc)
                    if tc.tool_use_id:
                        by_call[tc.tool_use_id] = tc
                content = data.get("content")
                text = content.strip() if isinstance(content, str) else ""
                if text or calls:
                    turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                      role="assistant", content=text, timestamp=ts, tool_calls=calls))
                    ordinal += 1
            elif kind == "tool.execution_complete":
                tc = by_call.get(data.get("toolCallId") or "")
                if tc is not None:
                    text = _result_text(data.get("result")).strip()
                    if data.get("success") is False:
                        text = "ERROR: " + text
                    tc.result_summary = text[:RESULT_LIMIT] or None
        # Turns without a timestamp are deliberately kept (see claude.py).
        return [t for t in turns if after_cutoff(t.timestamp, since)]
