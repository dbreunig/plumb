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
    # Codex built-ins
    "run": "Tool", "update_plan": "Tool", "wait": "Tool", "view_image": "Read", "imagegen": "Tool",
    "load_workspace_dependencies": "Tool", "search_openai_docs": "Tool", "fetch_openai_doc": "Tool",
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
_SUMMARY_KEYS = ("command", "cmd", "url", "skill", "message", "pattern", "prompt", "description", "query")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)
# Codex `exec` input is a JS snippet: const r = await tools.exec_command({cmd:"...", workdir:"..."})
_EXEC_CMD_RE = re.compile(r'\b(?:cmd|command)\s*:\s*"((?:[^"\\]|\\.)*)"')
_EXEC_WRAP_RE = re.compile(r"^\s*const \w+ = await tools\.")


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


def _patch_text(tool_input: Any) -> str:
    text = tool_input if isinstance(tool_input, str) else _as_dict(tool_input).get("input")
    return text if isinstance(text, str) else ""


def file_path_from_input(name: str, tool_input: Any) -> Optional[str]:
    if name == "apply_patch":
        m = _PATCH_FILE_RE.search(_patch_text(tool_input))
        return m.group(1).strip() if m else None
    d = _as_dict(tool_input)
    for key in _PATH_KEYS:
        val = d.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def file_paths_from_input(name: str, tool_input: Any) -> list[str]:
    """Every file a call touches, in order. apply_patch may name several."""
    if name == "apply_patch":
        return [m.strip() for m in _PATCH_FILE_RE.findall(_patch_text(tool_input))]
    return [p] if (p := file_path_from_input(name, tool_input)) else []


def input_summary(name: str, tool_input: Any, limit: int = 120) -> str:
    if name == "apply_patch":
        return (file_path_from_input(name, tool_input) or "")[:limit]
    if name == "AskUserQuestion":
        questions = _as_dict(tool_input).get("questions")
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            q = questions[0].get("question")
            if isinstance(q, str) and q:
                return q.replace("\n", " ")[:limit]
    if name == "exec" and isinstance(tool_input, str):
        m = _EXEC_CMD_RE.search(tool_input)
        text = m.group(1) if m else _EXEC_WRAP_RE.sub("", tool_input)
        return text.replace("\n", " ")[:limit]
    if isinstance(tool_input, str) and not tool_input.lstrip().startswith("{"):
        return tool_input[:limit]
    d = _as_dict(tool_input)
    for key in _SUMMARY_KEYS + _PATH_KEYS:
        val = d.get(key)
        if isinstance(val, str) and val:
            return val.replace("\n", " ")[:limit]
    return ""
