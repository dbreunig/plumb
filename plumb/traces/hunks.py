"""Line ranges from the staged diff, and file_refs = edited paths ∩ staged files."""
from __future__ import annotations

import os
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
    root = os.path.normpath(os.path.abspath(str(repo_root)))
    seen: set[str] = set()
    refs: list[FileRef] = []
    for p in edited_paths:
        if not p:
            continue
        rel = p
        if os.path.isabs(p):
            norm = os.path.normpath(p)
            if not (norm == root or norm.startswith(root + os.sep)):
                continue
            rel = os.path.relpath(norm, root)
        if rel in seen or rel not in hunks:
            continue
        seen.add(rel)
        for start, end in hunks[rel]:
            refs.append(FileRef(file=rel, lines=[start, end]))
    return refs
