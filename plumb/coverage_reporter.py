from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from plumb.config import load_config

PLUMB_MARKER_RE = re.compile(r'#\s*plumb:(req-[a-f0-9]+)')
FUNC_NAME_RE = re.compile(r'def test_req_([a-f0-9]+)_')


def run_pytest_coverage(repo_root: str | Path) -> dict | None:
    """Run pytest --cov and parse JSON output. Returns coverage data or None."""
    config = load_config(repo_root)
    if not config:
        return None

    repo_root = Path(repo_root)
    cov_json = repo_root / ".plumb" / "coverage.json"

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                "--cov=.",
                f"--cov-report=json:{cov_json}",
                "--cov-report=",
                "-q",
                "--no-header",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if cov_json.exists():
        try:
            return json.loads(cov_json.read_text())
        except json.JSONDecodeError:
            return None
    return None


def _get_code_coverage_pct(cov_data: dict | None) -> float | None:
    if not cov_data:
        return None
    try:
        return cov_data["totals"]["percent_covered"]
    except (KeyError, TypeError):
        return None


def _extract_test_req_ids(test_content: str) -> set[str]:
    """Extract requirement IDs from test content using markers and function names.

    Supports two formats:
    - ``# plumb:req-XXXXXXXX`` comments inside test functions
    - ``def test_req_XXXXXXXX_...`` function names (fallback/compat)
    """
    found: set[str] = set()
    for match in PLUMB_MARKER_RE.finditer(test_content):
        found.add(match.group(1))
    for match in FUNC_NAME_RE.finditer(test_content):
        found.add(f"req-{match.group(1)}")
    return found


def check_spec_to_test_coverage(repo_root: str | Path) -> tuple[int, int]:
    """Check how many requirements have associated tests.
    Returns (covered_count, total_count)."""
    repo_root = Path(repo_root)
    config = load_config(repo_root)
    if not config:
        return (0, 0)

    req_path = repo_root / ".plumb" / "requirements.json"
    if not req_path.exists():
        return (0, 0)

    try:
        requirements = json.loads(req_path.read_text())
    except (json.JSONDecodeError, Exception):
        return (0, 0)

    if not requirements:
        return (0, 0)

    # Read all test files
    test_content = ""
    for tp in config.test_paths:
        test_dir = repo_root / tp
        if test_dir.is_file():
            test_content += test_dir.read_text()
        elif test_dir.is_dir():
            for tf in test_dir.rglob("test_*.py"):
                test_content += tf.read_text()

    found_ids = _extract_test_req_ids(test_content)
    covered = sum(1 for r in requirements if r.get("id", "") in found_ids)

    return (covered, len(requirements))


def _collect_source_summaries(repo_root: Path) -> dict[str, str]:
    """Build concise summaries of source files for LLM mapping.

    Returns ``{relative_path: summary_text}`` dict.
    """
    import ast

    per_file: dict[str, str] = {}
    for item in sorted(repo_root.rglob("*.py")):
        rel = str(item.relative_to(repo_root))
        if ".plumb" in rel or "test_" in item.name or rel.startswith("tests/"):
            continue
        try:
            content = item.read_text()
        except Exception:
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        parts = [f"## {rel}"]
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                doc = ast.get_docstring(node) or ""
                methods = [
                    n.name for n in ast.iter_child_nodes(node)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                parts.append(
                    f"class {node.name}: {doc[:100]}"
                    + (f"  methods: {', '.join(methods)}" if methods else "")
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node) or ""
                parts.append(f"def {node.name}: {doc[:100]}")
        if len(parts) > 1:
            per_file[rel] = "\n".join(parts)
    return per_file


def _combine_summaries(per_file: dict[str, str]) -> str:
    """Join per-file summaries into a single string for the LLM."""
    return "\n\n".join(per_file[k] for k in sorted(per_file))


def _compute_per_file_hashes(per_file: dict[str, str]) -> dict[str, str]:
    """SHA256 hash of each file's summary text."""
    return {
        path: hashlib.sha256(text.encode()).hexdigest()
        for path, text in per_file.items()
    }


def _compute_requirements_hash(requirements: list[dict]) -> str:
    """SHA256 of the sorted requirements JSON."""
    blob = json.dumps(
        [{"id": r["id"], "text": r["text"]} for r in requirements],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _extract_source_files_from_evidence(
    evidence: str, known_files: set[str],
) -> list[str]:
    """Match file paths mentioned in an evidence string against known files."""
    found: list[str] = []
    for path in known_files:
        if path in evidence:
            found.append(path)
    return sorted(found)


def check_spec_to_code_coverage(
    repo_root: str | Path,
    use_llm: bool = False,
) -> tuple[int, int]:
    """Check how many requirements have corresponding implementation.

    When *use_llm* is False (default, used by ``plumb status``), only returns
    cached results. When True (used by ``plumb coverage``), refreshes the cache
    using an incremental strategy — only dirty requirements and changed source
    files are sent to the LLM.

    Returns (covered_count, total_count).
    """
    repo_root = Path(repo_root)
    config = load_config(repo_root)
    if not config:
        return (0, 0)

    req_path = repo_root / ".plumb" / "requirements.json"
    if not req_path.exists():
        return (0, 0)

    try:
        requirements = json.loads(req_path.read_text())
    except (json.JSONDecodeError, Exception):
        return (0, 0)

    if not requirements:
        return (0, 0)

    # Collect per-file source summaries and compute hashes
    per_file_summaries = _collect_source_summaries(repo_root)
    file_hashes = _compute_per_file_hashes(per_file_summaries)
    req_hash = _compute_requirements_hash(requirements)
    known_files = set(per_file_summaries.keys())

    cache_path = repo_root / ".plumb" / "code_coverage_map.json"
    cache: dict | None = None
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, Exception):
            cache = None

    # Determine if we can do an incremental update
    full_remap = True
    if cache and cache.get("version") == 2:
        cached_file_hashes = cache.get("source_hashes", {})
        cached_req_hash = cache.get("requirements_hash", "")
        cached_results = cache.get("results", {})

        if cached_req_hash == req_hash:
            # Requirements unchanged — check which files changed
            changed_files = {
                f for f in file_hashes
                if file_hashes[f] != cached_file_hashes.get(f)
            }
            new_files = set(file_hashes.keys()) - set(cached_file_hashes.keys())
            deleted_files = set(cached_file_hashes.keys()) - set(file_hashes.keys())
            all_affected_files = changed_files | new_files | deleted_files

            if not all_affected_files:
                # No file changes at all — return cached totals
                req_ids = {r["id"] for r in requirements}
                implemented = sum(
                    1 for rid, res in cached_results.items()
                    if rid in req_ids and res.get("implemented")
                )
                return (implemented, len(requirements))

            # Determine dirty requirements
            dirty_req_ids: set[str] = set()
            req_id_set = {r["id"] for r in requirements}
            for rid, res in cached_results.items():
                if rid not in req_id_set:
                    continue  # pruned later
                src_files = set(res.get("source_files", []))
                if src_files & all_affected_files:
                    dirty_req_ids.add(rid)
                elif not res.get("implemented"):
                    # Unimplemented reqs might now be in a changed file
                    dirty_req_ids.add(rid)
            # New requirements not in cache
            for r in requirements:
                if r["id"] not in cached_results:
                    dirty_req_ids.add(r["id"])

            full_remap = False

    if not use_llm:
        # Cache miss / stale — can't call LLM
        if cache and cache.get("version") == 2 and not full_remap:
            # Partial cache is still usable for status display
            req_ids = {r["id"] for r in requirements}
            implemented = sum(
                1 for rid, res in cache.get("results", {}).items()
                if rid in req_ids and res.get("implemented")
            )
            return (implemented, len(requirements))
        return (0, len(requirements))

    # --- LLM mapping ---
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.code_coverage_mapper import CodeCoverageMapper

    configure_dspy()
    mapper = CodeCoverageMapper()

    if full_remap:
        # Full re-map: all requirements, all files
        dirty_reqs = requirements
        summaries_for_llm = _combine_summaries(per_file_summaries)
    else:
        # Incremental: only dirty requirements + changed/new file summaries
        dirty_reqs = [r for r in requirements if r["id"] in dirty_req_ids]
        affected_summaries = {
            f: per_file_summaries[f]
            for f in (changed_files | new_files)
            if f in per_file_summaries
        }
        summaries_for_llm = _combine_summaries(affected_summaries) if affected_summaries else ""

    req_json = json.dumps([{"id": r["id"], "text": r["text"]} for r in dirty_reqs])

    try:
        results = run_with_retries(mapper, req_json, summaries_for_llm)
    except Exception:
        return (0, len(requirements))

    # Build fresh results dict from LLM output
    fresh_results: dict[str, dict] = {}
    for r in results:
        fresh_results[r.requirement_id] = {
            "implemented": r.implemented,
            "evidence": r.evidence,
            "source_files": _extract_source_files_from_evidence(
                r.evidence, known_files,
            ),
        }

    # Merge: keep cached results for clean reqs, update with fresh for dirty
    merged_results: dict[str, dict] = {}
    req_id_set = {r["id"] for r in requirements}

    if full_remap:
        merged_results = fresh_results
    else:
        for rid, res in cached_results.items():
            if rid in req_id_set and rid not in dirty_req_ids:
                merged_results[rid] = res
        merged_results.update(fresh_results)

    # Prune removed requirements
    merged_results = {k: v for k, v in merged_results.items() if k in req_id_set}

    covered = sum(1 for res in merged_results.values() if res.get("implemented"))

    # Write v2 cache
    cache_data = {
        "version": 2,
        "source_hashes": file_hashes,
        "requirements_hash": req_hash,
        "results": merged_results,
    }
    try:
        cache_path.write_text(json.dumps(cache_data, indent=2) + "\n")
    except Exception:
        pass

    return (covered, len(requirements))


def print_coverage_report(repo_root: str | Path) -> None:
    """Run and print all three coverage dimensions using Rich."""
    console = Console()
    repo_root = Path(repo_root)

    with console.status("[bold cyan]Running test suite with coverage...", spinner="dots") as status:
        cov_data = run_pytest_coverage(repo_root)
        code_pct = _get_code_coverage_pct(cov_data)

        status.update("[bold cyan]Scanning test markers for spec-to-test coverage...")
        test_covered, test_total = check_spec_to_test_coverage(repo_root)

        status.update("[bold cyan]Mapping requirements to source code...")
        code_covered, code_total = check_spec_to_code_coverage(repo_root, use_llm=True)

    table = Table(title="Plumb Coverage Report")
    table.add_column("Dimension", style="bold")
    table.add_column("Coverage", justify="right")
    table.add_column("Details")

    if code_pct is not None:
        table.add_row("Code Coverage", f"{code_pct:.1f}%", "pytest --cov")
    else:
        table.add_row("Code Coverage", "N/A", "Could not run pytest --cov")

    if test_total > 0:
        pct = (test_covered / test_total) * 100
        table.add_row(
            "Spec-to-Test",
            f"{pct:.1f}%",
            f"{test_covered}/{test_total} requirements covered",
        )
    else:
        table.add_row("Spec-to-Test", "N/A", "No requirements parsed")

    if code_total > 0:
        pct = (code_covered / code_total) * 100
        table.add_row(
            "Spec-to-Code",
            f"{pct:.1f}%",
            f"{code_covered}/{code_total} requirements implemented",
        )
    else:
        table.add_row("Spec-to-Code", "N/A", "No requirements parsed")

    console.print(table)
