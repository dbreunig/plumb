"""Claude Code adapter: ~/.claude/projects/<enc>/<session>.jsonl (+ subagents/)."""
from __future__ import annotations

from datetime import datetime, timezone
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
            # <session_id>/subagents/agent-x.jsonl, or nested:
            # <session_id>/subagents/workflows/wf_<id>/agent-x.jsonl
            for sub in proj.glob("*/subagents/**/agent-*.jsonl"):
                yield sub, sub.relative_to(proj).parts[0]

    def discover(self, repo_root, since: Optional[datetime]) -> list[SessionRef]:
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff_ts = since.timestamp() if since else None
        found: list[tuple[float, SessionRef]] = []
        for path, parent in self._candidates():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if cutoff_ts is not None and mtime < cutoff_ts:
                continue
            head = sniff_head(path, lambda e: e if e.get("cwd") else None)
            if not head or not same_repo(head["cwd"], repo_root):
                continue
            found.append((mtime, SessionRef(
                agent=self.name,
                session_id=path.stem if parent else (head.get("sessionId") or path.stem),
                path=str(path),
                cwd=head["cwd"],
                branch=head.get("gitBranch") or None,
                parent_session_id=parent,
            )))
        found.sort(key=lambda pair: pair[0])
        return [ref for _, ref in found]

    # -- parsing -------------------------------------------------------------

    def parse(self, ref: SessionRef, since: Optional[datetime]) -> list[Turn]:
        turns: list[Turn] = []
        by_tool_id: dict[str, ToolCall] = {}
        ordinal = 0
        for e in iter_jsonl(Path(ref.path)):
            if e.get("type") not in ("user", "assistant") or e.get("isMeta"):
                continue
            msg = e.get("message")
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            ts = e.get("timestamp")

            if e["type"] == "user":
                if isinstance(content, str):
                    if content.strip():
                        turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                          role="user", content=content, timestamp=ts))
                        ordinal += 1
                elif isinstance(content, list):
                    texts, saw_result = [], False
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_result":
                            saw_result = True
                            tc = by_tool_id.get(b.get("tool_use_id", ""))
                            if tc is not None:
                                text = _result_text(b.get("content"))
                                if b.get("is_error"):
                                    text = "ERROR: " + text
                                tc.result_summary = text.strip()[:RESULT_LIMIT] or None
                        elif b.get("type") == "text" and b.get("text"):
                            texts.append(b["text"])
                    # A list-form prompt (e.g. text + image) is a real user turn;
                    # text riding alongside tool_results is not.
                    if texts and not saw_result:
                        turns.append(Turn(agent=ref.agent, session_id=ref.session_id, ordinal=ordinal,
                                          role="user", content="\n".join(texts), timestamp=ts))
                        ordinal += 1
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

        # Turns without a timestamp are deliberately kept: losing a turn is worse
        # than re-seeing one, and dedup happens downstream.
        return [t for t in turns if after_cutoff(t.timestamp, since)]
