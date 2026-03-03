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
    read_all_decisions,
    update_decision_status,
    find_decision_branch,
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


def extract_outline(content: str) -> list[str]:
    """Extract markdown headers from content, preserving order."""
    return [line for line in content.split("\n") if re.match(r"^#{1,6}\s", line)]


def _normalize_header(header: str) -> str:
    """Normalize a markdown header for comparison: lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", header.strip().lower())


def _parse_sections(content: str) -> list[tuple[str, str]]:
    """Parse markdown into [(header_line, body_text), ...].
    Content before the first header gets header=""."""
    sections: list[tuple[str, str]] = []
    current_header = ""
    current_lines: list[str] = []

    for line in content.split("\n"):
        if re.match(r"^#{1,6}\s", line):
            sections.append((current_header, "\n".join(current_lines)))
            current_header = line
            current_lines = []
        else:
            current_lines.append(line)

    sections.append((current_header, "\n".join(current_lines)))
    return sections


def apply_section_updates(content: str, updates: list[dict]) -> str:
    """Apply section edits by matching headers. Returns updated content.
    Each update is {"header": "## X", "content": "new body text"}."""
    if not updates:
        return content

    # Build lookup: normalized_header -> new_content
    update_map: dict[str, str] = {}
    for u in updates:
        update_map[_normalize_header(u["header"])] = u["content"]

    sections = _parse_sections(content)
    result_parts: list[str] = []

    for header, body in sections:
        norm = _normalize_header(header)
        if norm in update_map:
            new_body = update_map[norm]
            # Ensure body starts with a blank line after header
            if new_body and not new_body.startswith("\n"):
                new_body = "\n" + new_body
            result_parts.append(header + new_body)
        else:
            if header:
                result_parts.append(header + body)
            else:
                result_parts.append(body)

    return "\n".join(result_parts)


def insert_new_sections(
    content: str, new_sections: list[dict], merged_outline: list[str]
) -> str:
    """Insert new sections into content at positions determined by merged_outline.
    Each new_section is {"header": "## X", "content": "body text"}.
    merged_outline is the full desired header order including existing + new."""
    if not new_sections:
        return content

    new_headers = {_normalize_header(s["header"]) for s in new_sections}
    new_by_header = {_normalize_header(s["header"]): s for s in new_sections}

    sections = _parse_sections(content)

    # Build ordered list of (header, body) from merged outline
    existing_by_norm = {}
    for header, body in sections:
        if header:
            existing_by_norm[_normalize_header(header)] = (header, body)

    result_parts: list[str] = []

    # Add any preamble (content before first header)
    for header, body in sections:
        if not header:
            result_parts.append(body)
            break

    for outline_header in merged_outline:
        norm = _normalize_header(outline_header)
        if norm in new_headers:
            s = new_by_header[norm]
            section_content = s["content"]
            if section_content and not section_content.startswith("\n"):
                section_content = "\n" + section_content
            result_parts.append(s["header"] + section_content)
        elif norm in existing_by_norm:
            header, body = existing_by_norm[norm]
            result_parts.append(header + body)

    return "\n".join(result_parts)


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

    # Load existing requirements to preserve history
    req_path = repo_root / ".plumb" / "requirements.json"
    existing_by_id: dict[str, dict] = {}
    if req_path.exists():
        try:
            for r in json.loads(req_path.read_text()):
                existing_by_id[r["id"]] = r
        except Exception:
            pass

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
                existing = existing_by_id.get(req_id)
                all_requirements.append(
                    {
                        "id": req_id,
                        "source_file": str(md_file.relative_to(repo_root)),
                        "source_section": "",
                        "text": req.text,
                        "ambiguous": req.ambiguous,
                        "created_at": existing["created_at"] if existing else now,
                        "last_seen_commit": existing["last_seen_commit"] if existing else None,
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
    repo_root: str | Path,
    decision_ids: list[str] | None = None,
    on_progress: callable | None = None,
) -> dict:
    """Sync approved/edited decisions to spec and tests.

    Returns summary dict with counts of spec sections updated and tests created.
    """
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.test_generator import TestGenerator

    repo_root = Path(repo_root)
    config = load_config(repo_root)
    if not config:
        return {"spec_updated": 0, "tests_generated": 0}

    decisions = read_all_decisions(repo_root)
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
        if on_progress:
            on_progress("No unsynced decisions found.")
        return {"spec_updated": 0, "tests_generated": 0}

    if on_progress:
        on_progress(f"Syncing {len(to_sync)} decision(s)...")

    configure_dspy()
    from plumb.programs.spec_updater import WholeFileSpecUpdater, OutlineMerger
    updater = WholeFileSpecUpdater()
    merger = OutlineMerger()
    spec_updated = 0

    # Format all decisions once
    decision_lines = []
    for i, d in enumerate(to_sync, 1):
        decision_lines.append(
            f"{i}. Question: {d.question or 'N/A'}\n   Decision: {d.decision or 'N/A'}"
        )
    decisions_text = "\n".join(decision_lines)

    for spec_path_str in config.spec_paths:
        if on_progress:
            on_progress(f"Updating spec: {spec_path_str}...")
        spec_path = repo_root / spec_path_str
        if not spec_path.is_file():
            continue

        content = spec_path.read_text()

        # Single LLM call for the whole file
        try:
            section_updates, new_sections = run_with_retries(
                updater, content, decisions_text
            )
        except Exception:
            continue

        # Apply updates to existing sections
        if section_updates:
            content = apply_section_updates(content, section_updates)
            spec_updated += len(section_updates)

        # Handle new sections via outline merge
        if new_sections:
            current_outline = extract_outline(content)
            new_headers = "\n".join(s["header"] for s in new_sections)
            try:
                merged_outline = run_with_retries(
                    merger, "\n".join(current_outline), new_headers
                )
            except Exception:
                # Fallback: append new sections at end
                for s in new_sections:
                    body = s["content"]
                    if body and not body.startswith("\n"):
                        body = "\n" + body
                    content = content.rstrip("\n") + "\n\n" + s["header"] + body
                spec_updated += len(new_sections)
                _atomic_write(spec_path, content)
                continue

            content = insert_new_sections(content, new_sections, merged_outline)
            spec_updated += len(new_sections)

        _atomic_write(spec_path, content)

    if on_progress:
        on_progress(f"Spec updated ({spec_updated} section(s)). Generating tests...")

    # Generate tests
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

        # Find uncovered requirements using marker/function-name scanning
        from plumb.coverage_reporter import _extract_test_req_ids
        covered_ids = _extract_test_req_ids(existing_tests)
        uncovered = [
            r for r in requirements
            if r["id"] not in covered_ids
        ]

        if uncovered:
            gen = TestGenerator()
            req_text = "\n".join(
                f"- [{r['id']}] {r['text']}" for r in uncovered
            )
            # Read source code referenced by the synced decisions
            code_context = ""
            seen_files: set[str] = set()
            for d in to_sync:
                for ref in d.file_refs:
                    if ref.file in seen_files:
                        continue
                    seen_files.add(ref.file)
                    fpath = repo_root / ref.file
                    if not fpath.is_file():
                        continue
                    try:
                        code_context += fpath.read_text() + "\n"
                    except Exception:
                        continue

            try:
                test_code = run_with_retries(
                    gen, req_text, existing_tests[:8000], code_context[:16000]
                )
                if test_code.strip():
                    # Append to first test path, using test_generated.py
                    test_target = repo_root / config.test_paths[0]
                    if test_target.is_dir():
                        test_target = test_target / "test_generated.py"
                    if test_target.exists():
                        existing = test_target.read_text()
                        _atomic_write(test_target, existing + "\n\n" + test_code + "\n")
                    else:
                        _atomic_write(
                            test_target,
                            "import pytest\n\n\n" + test_code + "\n",
                        )
                    tests_generated += 1
            except Exception:
                pass

    # Re-parse spec
    if on_progress:
        on_progress("Re-parsing spec files...")
    try:
        parse_spec_files(repo_root)
    except Exception:
        pass

    if on_progress:
        on_progress("Marking decisions as synced...")

    # Mark decisions as synced
    for d in to_sync:
        branch = find_decision_branch(repo_root, d.id)
        update_decision_status(repo_root, d.id, branch=branch, synced_at=now)

    return {"spec_updated": spec_updated, "tests_generated": tests_generated}
