"""Tolerant JSONL reading shared by every adapter."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)


def iter_jsonl(path: Path) -> Iterator[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError as e:
        logger.debug("Skipping %s: %s", path, e)
        return


def sniff_head(path: Path, pick: Callable[[dict], Any], max_lines: int = 64) -> Optional[Any]:
    """Return pick(entry) for the first of the first `max_lines` entries where it is truthy."""
    for i, entry in enumerate(iter_jsonl(path)):
        if i >= max_lines:
            return None
        val = pick(entry)
        if val:
            return val
    return None
