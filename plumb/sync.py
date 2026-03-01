from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from plumb.config import load_config
from plumb.decision_log import (
    Decision,
    read_decisions,
    update_decision_status,
)


def _generate_requirement_id(text: str) -> str:
    """Generate stable requirement ID: req-<sha256(text.strip().lower())[:8]>"""
    h = hashlib.sha256(text.strip().lower().encode()).hexdigest()[:8]
    return f"req-{h}"


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via temp file + rename for atomicity."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=path.suffix)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def find_spec_section(spec_content: str, decision_text: str) -> tuple[str, int, int]:
    """Locate the most relevant markdown section for a decision.
    Returns (section_text, start_line, end_line).
    Falls back to returning the entire content if no section match."""
    lines = spec_content.split("\n")
    sections: list[tuple[str, int, int]] = []
    current_start = 0
    current_header = ""

    for i, line in enumerate(lines):
        if re.match(r"^#{1,4}\s", line):
            if i > current_start:
                sections.append(
                    ("\n".join(lines[current_start:i]), current_start, i)
                )
            current_start = i
            current_header = line

    # Last section
    if current_start < len(lines):
        sections.append(
            ("\n".join(lines[current_start:]), current_start, len(lines))
        )

    if not sections:
        return (spec_content, 0, len(lines))

    # Simple relevance: pick section with most word overlap with decision
    decision_words = set(decision_text.lower().split())
    best_section = sections[0]
    best_score = 0
    for sec_text, start, end in sections:
        sec_words = set(sec_text.lower().split())
        score = len(decision_words & sec_words)
        if score > best_score:
            best_score = score
            best_section = (sec_text, start, end)

    return best_section


def parse_spec_files(repo_root: str | Path) -> list[dict]:
    """Read markdown spec files, run RequirementParser, assign stable IDs,
    write requirements.json."""
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.requirement_parser import RequirementParser

    repo_root = Path(repo_root)
    config = load_config(repo_root)
    if not config:
        return []

    all_requirements: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    configure_dspy()
    parser = RequirementParser()

    for spec_path_str in config.spec_paths:
        spec_path = repo_root / spec_path_str
        if spec_path.is_dir():
            md_files = list(spec_path.rglob("*.md"))
        elif spec_path.is_file():
            md_files = [spec_path]
        else:
            continue

        for md_file in md_files:
            content = md_file.read_text()
            try:
                parsed = run_with_retries(parser, content)
            except Exception:
                continue

            for req in parsed:
                req_id = _generate_requirement_id(req.text)
                all_requirements.append(
                    {
                        "id": req_id,
                        "source_file": str(md_file.relative_to(repo_root)),
                        "source_section": "",
                        "text": req.text,
                        "ambiguous": req.ambiguous,
                        "created_at": now,
                        "last_seen_commit": None,
                    }
                )

    # Deduplicate by ID (same text = same ID)
    seen: dict[str, dict] = {}
    for r in all_requirements:
        seen[r["id"]] = r
    deduped = list(seen.values())

    # Write requirements.json
    req_path = repo_root / ".plumb" / "requirements.json"
    _atomic_write(req_path, json.dumps(deduped, indent=2) + "\n")

    return deduped


def sync_decisions(
    repo_root: str | Path, decision_ids: list[str] | None = None
) -> dict:
    """Sync approved/edited decisions to spec and tests.

    Returns summary dict with counts of spec sections updated and test stubs created.
    """
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.spec_updater import SpecUpdater
    from plumb.programs.test_generator import TestGenerator

    repo_root = Path(repo_root)
    config = load_config(repo_root)
    if not config:
        return {"spec_updated": 0, "tests_generated": 0}

    decisions = read_decisions(repo_root)
    now = datetime.now(timezone.utc).isoformat()

    # Filter to approved/edited without synced_at
    to_sync = []
    for d in decisions:
        if d.status not in ("approved", "edited"):
            continue
        if d.synced_at:
            continue
        if decision_ids and d.id not in decision_ids:
            continue
        to_sync.append(d)

    if not to_sync:
        return {"spec_updated": 0, "tests_generated": 0}

    configure_dspy()
    updater = SpecUpdater()
    spec_updated = 0

    # Update spec for each decision
    for d in to_sync:
        for spec_path_str in config.spec_paths:
            spec_path = repo_root / spec_path_str
            if not spec_path.is_file():
                continue

            content = spec_path.read_text()
            section_text, start, end = find_spec_section(
                content, d.decision or ""
            )

            try:
                updated_section = run_with_retries(
                    updater,
                    section_text,
                    d.decision or "",
                    d.question or "",
                )
            except Exception:
                continue

            # Replace section in content
            lines = content.split("\n")
            new_lines = lines[:start] + updated_section.split("\n") + lines[end:]
            _atomic_write(spec_path, "\n".join(new_lines))
            spec_updated += 1
            break  # Only update first matching spec file

    # Generate test stubs
    tests_generated = 0
    req_path = repo_root / ".plumb" / "requirements.json"
    if req_path.exists():
        try:
            requirements = json.loads(req_path.read_text())
        except Exception:
            requirements = []

        # Read existing tests
        existing_tests = ""
        for tp in config.test_paths:
            test_path = repo_root / tp
            if test_path.is_file():
                existing_tests += test_path.read_text()
            elif test_path.is_dir():
                for tf in test_path.rglob("test_*.py"):
                    existing_tests += tf.read_text()

        # Find uncovered requirements
        existing_lower = existing_tests.lower()
        uncovered = [
            r for r in requirements
            if r["id"].lower() not in existing_lower
        ]

        if uncovered:
            gen = TestGenerator()
            req_text = "\n".join(
                f"- [{r['id']}] {r['text']}" for r in uncovered
            )
            # Read some source code for context
            code_context = ""
            for item in repo_root.rglob("*.py"):
                if ".plumb" in str(item) or "test_" in item.name:
                    continue
                try:
                    code_context += item.read_text() + "\n"
                except Exception:
                    continue
                if len(code_context) > 5000:
                    break

            try:
                stubs = run_with_retries(
                    gen, req_text, existing_tests[:2000], code_context[:2000]
                )
                if stubs.strip():
                    # Append to first test path
                    test_target = repo_root / config.test_paths[0]
                    if test_target.is_dir():
                        test_target = test_target / "test_generated.py"
                    if test_target.exists():
                        existing = test_target.read_text()
                        _atomic_write(test_target, existing + "\n\n" + stubs + "\n")
                    else:
                        _atomic_write(test_target, stubs + "\n")
                    tests_generated += 1
            except Exception:
                pass

    # Re-parse spec
    try:
        parse_spec_files(repo_root)
    except Exception:
        pass

    # Mark decisions as synced
    for d in to_sync:
        update_decision_status(repo_root, d.id, synced_at=now)

    return {"spec_updated": spec_updated, "tests_generated": tests_generated}
