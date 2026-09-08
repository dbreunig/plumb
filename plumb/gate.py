"""Reviewed-diff signature and gate state for the review-mode pre-commit hook."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

EMPTY_SIGNATURE = "empty"


def diff_signature(repo_root: str | Path, diff: str) -> str:
    """First field of `git patch-id --stable` over *diff*; EMPTY_SIGNATURE when blank."""
    if not diff.strip():
        return EMPTY_SIGNATURE
    out = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=diff, capture_output=True, text=True, cwd=str(repo_root),
    ).stdout.strip()
    if not out:
        return EMPTY_SIGNATURE
    return out.split()[0]


def _gate_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".plumb" / "gate.json"


def read_gate_state(repo_root: str | Path) -> dict | None:
    """The stored gate state, or None when absent or unreadable."""
    try:
        return json.loads(_gate_path(repo_root).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_gate_state(repo_root: str | Path, signature: str) -> None:
    _gate_path(repo_root).write_text(json.dumps({
        "diff_signature": signature,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }))


def clear_gate_state(repo_root: str | Path) -> None:
    _gate_path(repo_root).unlink(missing_ok=True)
