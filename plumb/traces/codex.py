"""Codex adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (+ archived_sessions/)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from plumb.traces import SessionRef, ToolCall, Turn
from plumb.traces.jsonl import iter_jsonl, sniff_head
from plumb.traces.repo import after_cutoff, same_repo
from plumb.traces.taxonomy import categorize, file_path_from_input, file_paths_from_input, input_summary

RESULT_LIMIT = 200


def _output_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return " ".join(o.get("text", "") for o in output if isinstance(o, dict))
    return ""


def _session_meta(e: dict):
    return e.get("payload") if e.get("type") == "session_meta" else None


def _turn_branch(e: dict):
    if e.get("type") != "turn_context":
        return None
    return ((e.get("payload") or {}).get("git") or {}).get("branch")


class CodexSource:
    name = "codex"

    def __init__(self, root: Optional[Path] = None, archived: Optional[Path] = None):
        home = Path.home() / ".codex"
        self.roots = [root or home / "sessions", archived or home / "archived_sessions"]

    # -- discovery -----------------------------------------------------------

    def _candidates(self):
        for root in self.roots:
            if root.is_dir():
                yield from root.rglob("rollout-*.jsonl")

    def discover(self, repo_root, since: Optional[datetime]) -> list[SessionRef]:
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff_ts = since.timestamp() if since else None
        found: list[tuple[float, SessionRef]] = []
        for path in self._candidates():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if cutoff_ts is not None and mtime < cutoff_ts:
                continue
            meta = sniff_head(path, _session_meta)
            if not isinstance(meta, dict) or not same_repo(meta.get("cwd", ""), repo_root):
                continue
            branch = sniff_head(path, _turn_branch)
            found.append((mtime, SessionRef(
                agent=self.name,
                session_id=meta.get("id") or path.stem,
                path=str(path),
                cwd=meta["cwd"],
                branch=branch or None,
            )))
        found.sort(key=lambda pair: pair[0])
        return [ref for _, ref in found]

    # -- parsing -------------------------------------------------------------

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
            kind, ts = e.get("type"), e.get("timestamp")
            p = e.get("payload")
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            # response_item/message duplicates the event_msg user/agent messages; ignore it.
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
                msg = p.get("message") or ""
                if msg:
                    pending_text.append(msg)
                    pending_ts = pending_ts or ts
            elif kind == "response_item" and ptype in ("function_call", "custom_tool_call"):
                name = p.get("name") or "unknown"
                arg = p.get("arguments") if ptype == "function_call" else p.get("input")
                tc = ToolCall(name=name, category=categorize(name),
                              file_path=file_path_from_input(name, arg),
                              file_paths=file_paths_from_input(name, arg),
                              input_summary=input_summary(name, arg),
                              tool_use_id=p.get("call_id"))
                pending_calls.append(tc)
                pending_ts = pending_ts or ts
                if tc.tool_use_id:
                    by_call[tc.tool_use_id] = tc
            elif kind == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
                tc = by_call.get(p.get("call_id") or "")
                if tc is not None:
                    tc.result_summary = _output_text(p.get("output")).strip()[:RESULT_LIMIT] or None
        flush()
        # Turns without a timestamp are deliberately kept (see claude.py).
        return [t for t in turns if after_cutoff(t.timestamp, since)]
