"""Record mode: extract decisions for a landed commit, outside the commit path.

``run_post_commit`` spawns ``plumb record-extract <sha>`` as a detached worker;
the worker takes ``.plumb/record.lock`` and calls :func:`record_extract`, which
runs the shared extraction pipeline against the commit's own diff (never the
index) and appends each decision as ``recorded`` (auto-approved) or ``pending``
depending on ``record_threshold``.
"""
from __future__ import annotations

import contextlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from git import Repo

from plumb.config import load_config
from plumb.decision_log import Decision, append_decisions, delete_decisions_by_commit
from plumb.traces.hunks import parse_unified_hunks
from plumb.traces.repo import commit_datetime

# Transcript cutoff for a root commit: there is no previous commit, so read everything.
_EPOCH = "1970-01-01T00:00:00+00:00"

_GIT = ["git", "-c", "core.quotePath=false"]


def commit_diff(repo: Repo, config, sha: str) -> tuple[str, dict[str, list[list[int]]]]:
    """(diff text, hunks) for commit ``sha``, restricted to files that survive the
    managed-path + ``.plumbignore`` filter the pre-commit path applies.

    Uses ``git show`` so a root commit needs no special case. Returns
    ``("", {})`` when no file survives (or the commit has no patch, e.g. a merge).
    """
    from plumb import git_hook

    names = repo.git.execute([*_GIT, "show", "--format=", "--name-only", sha])
    files = git_hook._filter_paths(repo, config, [f for f in names.splitlines() if f])
    if not files:
        return "", {}
    diff = repo.git.execute([*_GIT, "show", "--format=", "--patch", sha, "--", *files])
    hunks = parse_unified_hunks(
        repo.git.execute([*_GIT, "show", "--format=", "-U0", sha, "--", *files])
    )
    return diff, hunks


def _on_any_ref(repo: Repo, sha: str) -> bool:
    """True if some local or remote branch (or HEAD) contains ``sha``."""
    try:
        return repo.git.branch("--all", "--contains", sha).strip() != ""
    except Exception:
        return False


def _same_parent(repo: Repo, old_sha: str, new_sha: str) -> bool:
    try:
        o, n = repo.commit(old_sha), repo.commit(new_sha)
        return bool(o.parents) and bool(n.parents) and o.parents[0].hexsha == n.parents[0].hexsha
    except Exception:
        return False


def delete_replaced_commit_decisions(
    repo_root: Path, repo: Repo, last_commit: str | None, sha: str, branch: str
) -> int:
    """If ``sha`` amended ``last_commit`` (same parent, different commit, and the
    old commit is no longer on any ref), drop the replaced commit's decisions —
    they describe a commit that no longer exists. A same-parent sibling whose
    predecessor still lives on some branch is not an amend. Returns the number
    of lines removed."""
    if not last_commit or last_commit == sha or not _same_parent(repo, last_commit, sha):
        return 0
    if _on_any_ref(repo, last_commit):
        return 0
    return delete_decisions_by_commit(repo_root, last_commit, branch=branch)


def record_extract(repo_root, sha: str, branch: str | None = None) -> list[Decision]:
    """Extract decisions for commit ``sha`` and append them as recorded/pending
    to the ``branch`` shard (default: the currently checked-out branch; the
    worker passes the branch the commit landed on, since the user may have
    switched branches by the time it runs).

    Returns the decisions written. Writes nothing (and skips the LLM) when the
    commit touches only ignored/managed files, or is no longer on any ref (it
    was amended or reset away while the worker waited for the lock).
    """
    from plumb import git_hook  # late import: git_hook imports this module

    repo_root = Path(repo_root)
    repo = Repo(repo_root)
    cfg = load_config(repo_root)
    if cfg is None:
        return []
    c = repo.commit(sha)
    sha = c.hexsha
    if branch is None:
        branch = git_hook._get_branch_name(repo)

    if not _on_any_ref(repo, sha):
        return []

    prev = c.parents[0].hexsha if c.parents else None
    delete_replaced_commit_decisions(repo_root, repo, cfg.last_commit, sha, branch)

    diff, hunks = commit_diff(repo, cfg, sha)
    if not diff.strip():
        return []

    # Explicit cutoff: transcripts since the parent commit. Passing
    # since_commit=None alone would fall back to config.last_commit, which
    # post-commit has already advanced to this very commit.
    prev_dt = commit_datetime(repo_root, prev) if prev else None
    since_datetime = prev_dt.isoformat() if prev_dt is not None else _EPOCH
    decisions = git_hook.extract_decisions(
        repo_root, cfg, diff, branch,
        since_commit=None, since_datetime=since_datetime, hunks=hunks,
    )
    now = datetime.now(timezone.utc).isoformat()
    out: list[Decision] = []
    for d in decisions:
        auto = cfg.record_threshold is None or (
            d.confidence is not None and d.confidence >= cfg.record_threshold
        )
        out.append(d.model_copy(update={
            "status": "recorded" if auto else "pending",
            "approved_by": "auto" if auto else None,
            "commit_sha": sha,
            "branch": branch,
            "created_at": d.created_at or now,
        }))
    if out:
        append_decisions(repo_root, out, branch=branch)
    return out


@contextlib.contextmanager
def record_lock(repo_root, wait: bool = True):
    """Advisory lock on ``.plumb/record.lock``; yields True when held, False when
    ``wait=False`` and another worker holds it."""
    import fcntl

    path = Path(repo_root) / ".plumb" / "record.lock"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def spawn_worker(repo_root, sha: str, branch: str) -> None:
    """Launch ``plumb record-extract <sha> --branch <branch>`` detached; the
    commit returns immediately. Worker output is appended to ``.plumb/record.log``."""
    plumb_dir = Path(repo_root) / ".plumb"
    plumb_dir.mkdir(exist_ok=True)
    with open(plumb_dir / "record.log", "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "plumb.cli", "record-extract", sha, "--branch", branch],
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
