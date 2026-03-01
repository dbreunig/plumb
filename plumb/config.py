from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class PlumbConfig(BaseModel):
    spec_paths: list[str] = Field(default_factory=list)
    test_paths: list[str] = Field(default_factory=list)
    claude_log_path: Optional[str] = None
    initialized_at: Optional[str] = None
    last_commit: Optional[str] = None
    last_commit_branch: Optional[str] = None


def find_repo_root(start: str | Path | None = None) -> Path | None:
    """Walk up from start (default cwd) looking for a .git directory."""
    p = Path(start) if start else Path.cwd()
    for parent in [p] + list(p.parents):
        if (parent / ".git").exists():
            return parent
    return None


def ensure_plumb_dir(repo_root: str | Path) -> Path:
    """Create .plumb/ directory if it doesn't exist. Returns the path."""
    plumb_dir = Path(repo_root) / ".plumb"
    plumb_dir.mkdir(exist_ok=True)
    return plumb_dir


def config_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".plumb" / "config.json"


def load_config(repo_root: str | Path) -> PlumbConfig | None:
    """Load config from .plumb/config.json. Returns None if not found or malformed."""
    cp = config_path(repo_root)
    if not cp.exists():
        return None
    try:
        data = json.loads(cp.read_text())
        return PlumbConfig(**data)
    except (json.JSONDecodeError, Exception):
        return None


def save_config(repo_root: str | Path, cfg: PlumbConfig) -> None:
    """Write config to .plumb/config.json atomically via temp file + rename."""
    plumb_dir = ensure_plumb_dir(repo_root)
    cp = plumb_dir / "config.json"
    fd, tmp = tempfile.mkstemp(dir=str(plumb_dir), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg.model_dump(), f, indent=2)
            f.write("\n")
        os.replace(tmp, str(cp))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
