"""Agent-agnostic trace ingestion.

A TraceSource finds an agent's sessions for a repo and normalizes them into
Turn/ToolCall records. Everything downstream of this package is agent-agnostic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator


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
    file_path: Optional[str] = None  # primary (first) path; see file_paths for the rest
    file_paths: list[str] = Field(default_factory=list)  # every path touched, in order; [0] == file_path
    input_summary: str = ""         # first ~120 chars of the salient argument
    result_summary: Optional[str] = None  # truncated result, when the agent records one
    tool_use_id: Optional[str] = None

    @model_validator(mode="after")
    def _sync_file_paths(self) -> "ToolCall":
        if not self.file_paths and self.file_path:
            self.file_paths = [self.file_path]
        elif self.file_path is None and self.file_paths:
            self.file_path = self.file_paths[0]
        return self


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
    from plumb.traces.codex import CodexSource
    from plumb.traces.copilot import CopilotSource
    from plumb.traces.pi import PiSource

    return [ClaudeSource(), CodexSource(), PiSource(), CopilotSource()]
