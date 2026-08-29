"""Line ranges from the staged diff, and file_refs = edited paths ∩ staged files."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

from git import Repo

from plumb.decision_log import FileRef

_DIFF_RE = re.compile(r"^diff --git ")
_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_unified_hunks(diff: str) -> dict[str, list[list[int]]]:
    """{path: [[new_start, new_end], ...]} for every added/modified range.

    Pure deletions (+N,0) contribute no range. Deleted files (+++ /dev/null) are skipped.

    File paths are only read from the header block that follows a ``diff --git``
    line, so content lines that happen to look like ``+++ b/x.py`` (an added
    line ``++ b/x.py``) cannot hijack attribution.
    """
    out: dict[str, list[list[int]]] = {}
    current: str | None = None
    in_header = False
    for line in diff.splitlines():
        if _DIFF_RE.match(line):
            in_header, current = True, None
            continue
        if in_header:
            if line.startswith("+++ /dev/null"):
                current = None
            elif (m := _FILE_RE.match(line)):
                # git appends a tab after paths containing spaces
                current = m.group(1).rstrip("\t")
            elif line.startswith("@@"):
                in_header = False
            else:
                continue
        h = _HUNK_RE.match(line)
        if h and current:
            start = int(h.group(1))
            count = int(h.group(2)) if h.group(2) is not None else 1
            if count > 0:
                out.setdefault(current, []).append([start, start + count - 1])
    return out


def staged_hunks(repo: Repo) -> dict[str, list[list[int]]]:
    # quotePath=false so non-ASCII paths are emitted verbatim, not octal-escaped.
    diff = repo.git.execute(["git", "-c", "core.quotePath=false", "diff", "--cached", "-U0"])
    return parse_unified_hunks(diff)


def file_refs_for(edited_paths: Iterable[str], hunks: dict[str, list[list[int]]],
                  repo_root: str | Path, cwd: str | Path | None = None) -> list[FileRef]:
    """FileRefs for every edited path that has staged hunks.

    Absolute paths are made relative to *repo_root* (paths outside the repo are
    dropped). Relative paths are joined onto *cwd* (the session's working
    directory) when given, otherwise normalized as repo-relative.
    """
    root = os.path.normpath(os.path.abspath(str(repo_root)))
    seen: set[str] = set()
    refs: list[FileRef] = []
    for p in edited_paths:
        if not p:
            continue
        if not os.path.isabs(p) and cwd is not None:
            p = os.path.join(str(cwd), p)
        if os.path.isabs(p):
            norm = os.path.normpath(p)
            if not (norm == root or norm.startswith(root + os.sep)):
                continue
            rel = os.path.relpath(norm, root)
        else:
            rel = os.path.normpath(p)
        if rel in seen or rel not in hunks:
            continue
        seen.add(rel)
        for start, end in hunks[rel]:
            refs.append(FileRef(file=rel, lines=[start, end]))
    return refs
