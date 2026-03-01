"""Parse .plumbignore and check whether files should be excluded from analysis."""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

DEFAULT_PLUMBIGNORE = """\
README.md
LICENSE
LICENSE.*
CHANGELOG.md
CONTRIBUTING.md
docs/
.github/
.vscode/
.idea/
Makefile
Dockerfile
docker-compose*
"""


def _parse_lines(text: str) -> list[str]:
    """Parse pattern lines, skipping blanks and comments."""
    patterns: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def parse_plumbignore(repo_root: str | Path) -> list[str]:
    """Read .plumbignore and return a list of patterns.

    Skips blank lines and comments (lines starting with ``#``).
    Falls back to DEFAULT_PLUMBIGNORE when the file is absent.
    """
    path = Path(repo_root) / ".plumbignore"
    if not path.is_file():
        return _parse_lines(DEFAULT_PLUMBIGNORE)
    return _parse_lines(path.read_text())


def is_ignored(filepath: str, patterns: list[str]) -> bool:
    """Return True if *filepath* matches any pattern.

    Supports:
    - Exact match: ``README.md``
    - Glob matched against the basename: ``*.txt``
    - Directory prefix (pattern ends with ``/``): ``docs/`` matches ``docs/foo``
    """
    basename = Path(filepath).name
    for pat in patterns:
        if pat.endswith("/"):
            # Directory prefix — match if filepath starts with the prefix
            if filepath == pat.rstrip("/") or filepath.startswith(pat):
                return True
        else:
            # Exact full-path match or fnmatch against basename
            if filepath == pat or fnmatch(basename, pat):
                return True
    return False
