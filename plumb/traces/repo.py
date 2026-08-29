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
