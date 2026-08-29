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
