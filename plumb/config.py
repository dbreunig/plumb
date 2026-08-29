from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

MODES = ("review", "record")

# Fields whose invalid values in config.json fall back to defaults with a warning
# rather than disabling Plumb.
_LENIENT_FIELDS = {
    "mode": "|".join(MODES),
    "record_threshold": "0.0–1.0",
}


class PlumbConfig(BaseModel):
    spec_paths: list[str] = Field(default_factory=list)
    test_paths: list[str] = Field(default_factory=list)
    initialized_at: Optional[str] = None
    last_commit: Optional[str] = None
    last_commit_branch: Optional[str] = None
    last_extracted_at: Optional[str] = None
    program_models: dict[str, dict] = Field(default_factory=dict)
    mode: str = "review"
    record_threshold: Optional[float] = None

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {v!r}")
        return v

    @field_validator("record_threshold")
    @classmethod
    def _validate_record_threshold(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"record_threshold must be between 0.0 and 1.0, got {v!r}")
        return v


def effective_mode(cfg: PlumbConfig | None) -> tuple[str, str]:
    """Resolve the active mode and where it came from.

    Returns (mode, source) where source is "env" (PLUMB_MODE), "config",
    or "default" (no config). An invalid or empty PLUMB_MODE is ignored.
    """
    env = os.environ.get("PLUMB_MODE", "").strip().lower()
    if env in MODES:
        return env, "env"
    if cfg is None:
        return "review", "default"
    return cfg.mode, "config"


def find_repo_root(start: str | Path | None = None) -> Path | None:
    """Walk up from start (default cwd) looking for a .git directory."""
    p = Path(start) if start else Path.cwd()
    for parent in [p] + list(p.parents):
        if (parent / ".git").exists():
            return parent
    return None


# Runtime files written by the record-mode worker; never meant to be committed.
_PLUMB_GITIGNORE = "record.log\nrecord.lock\n"


def ensure_plumb_dir(repo_root: str | Path) -> Path:
    """Create .plumb/ (and its .gitignore for runtime files) if absent. Returns the path."""
    plumb_dir = Path(repo_root) / ".plumb"
    plumb_dir.mkdir(exist_ok=True)
    gitignore = plumb_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_PLUMB_GITIGNORE)
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
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return PlumbConfig(**data)
    except ValidationError as e:
        bad = {err["loc"][0] for err in e.errors() if err["loc"]}
        if not bad or not bad <= _LENIENT_FIELDS.keys():
            return None
        details = ", ".join(
            f"{field}={data.get(field)!r} (accepted: {_LENIENT_FIELDS[field]})"
            for field in sorted(bad)
        )
        print(
            f"plumb: warning: ignoring invalid value(s) in {cp}: {details}; using defaults.",
            file=sys.stderr,
        )
        for field in bad:
            data.pop(field, None)
        try:
            return PlumbConfig(**data)
        except ValidationError:
            return None
    except Exception:
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
