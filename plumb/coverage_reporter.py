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


def _compute_cache_key(requirements: list[dict], source_summaries: str) -> str:
    """SHA256 of requirements + source summaries for cache invalidation."""
    blob = json.dumps(requirements, sort_keys=True) + source_summaries
    return hashlib.sha256(blob.encode()).hexdigest()


def _collect_source_summaries(repo_root: Path) -> str:
    """Build concise summaries of source files for LLM mapping."""
    import ast

    summaries: list[str] = []
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
            summaries.append("\n".join(parts))
    return "\n\n".join(summaries)


def check_spec_to_code_coverage(
    repo_root: str | Path,
    use_llm: bool = False,
) -> tuple[int, int]:
    """Check how many requirements have corresponding implementation.

    When *use_llm* is False (default, used by ``plumb status``), only returns
    cached results. When True (used by ``plumb coverage``), refreshes the cache
    if the inputs have changed.

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

    cache_path = repo_root / ".plumb" / "code_coverage_map.json"
    source_summaries = _collect_source_summaries(repo_root)
    cache_key = _compute_cache_key(requirements, source_summaries)

    # Try loading cache
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
            if cache.get("cache_key") == cache_key:
                return (cache["covered"], cache["total"])
        except (json.JSONDecodeError, KeyError):
            pass

    if not use_llm:
        # Cache miss but we're not allowed to call LLM — return unknown
        return (0, len(requirements))

    # Run LLM mapping
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.code_coverage_mapper import CodeCoverageMapper

    configure_dspy()
    mapper = CodeCoverageMapper()

    req_json = json.dumps([{"id": r["id"], "text": r["text"]} for r in requirements])

    try:
        results = run_with_retries(mapper, req_json, source_summaries)
    except Exception:
        return (0, len(requirements))

    implemented_ids = {r.requirement_id for r in results if r.implemented}
    covered = sum(1 for r in requirements if r.get("id", "") in implemented_ids)

    # Write cache
    cache_data = {
        "cache_key": cache_key,
        "covered": covered,
        "total": len(requirements),
        "implemented_ids": sorted(implemented_ids),
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
