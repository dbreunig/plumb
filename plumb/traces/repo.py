"""Attribute a recorded cwd to a git repository (worktree-aware)."""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
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
def _git_common_dir_cached(path: str) -> Optional[Path]:
    """Uncached body of git_common_dir; `path` must already be normalized.

    Uses --git-common-dir (not --show-toplevel) so linked worktrees resolve
    to the same repo as the main checkout. Misses (including transient git
    failures) are cached for the process lifetime.
    """
    start = _existing_ancestor(path)
    if start is None:
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(start), capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    common = Path(out.stdout.strip())
    if not common.is_absolute():
        common = start / common
    return common.resolve()


def git_common_dir(path: str | Path) -> Optional[Path]:
    """Resolve `path` to its repo's common .git dir; None if not in a repo."""
    return _git_common_dir_cached(os.path.normpath(os.path.abspath(str(path))))


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


def parse_ts(value) -> Optional[datetime]:
    """ISO-8601 string (with Z) or epoch ms/s -> aware datetime; None if unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def after_cutoff(ts, cutoff: Optional[datetime]) -> bool:
    if cutoff is None:
        return True
    dt = parse_ts(ts)
    if dt is None:
        return True
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return dt > cutoff


def resolve_cutoff(repo_root, since_commit: Optional[str], since_datetime: Optional[str]) -> Optional[datetime]:
    """since_datetime (last_extracted_at) wins; else the commit's datetime; else None."""
    dt = parse_ts(since_datetime) if since_datetime else None
    if dt is None and since_commit:
        dt = commit_datetime(repo_root, since_commit)
    return dt
