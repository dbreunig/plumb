from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from plumb.config import load_config


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

    test_content_lower = test_content.lower()
    covered = 0
    for req in requirements:
        req_id = req.get("id", "")
        if req_id and req_id.lower() in test_content_lower:
            covered += 1

    return (covered, len(requirements))


def check_spec_to_code_coverage(repo_root: str | Path) -> tuple[int, int]:
    """Check how many requirements have corresponding implementation.
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

    # Read all source files (excluding test files and .plumb)
    source_content = ""
    for item in repo_root.rglob("*.py"):
        if ".plumb" in str(item) or "test_" in item.name:
            continue
        try:
            source_content += item.read_text()
        except Exception:
            continue

    source_lower = source_content.lower()
    covered = 0
    for req in requirements:
        req_id = req.get("id", "")
        if req_id and req_id.lower() in source_lower:
            covered += 1

    return (covered, len(requirements))


def print_coverage_report(repo_root: str | Path) -> None:
    """Run and print all three coverage dimensions using Rich."""
    console = Console()
    repo_root = Path(repo_root)

    table = Table(title="Plumb Coverage Report")
    table.add_column("Dimension", style="bold")
    table.add_column("Coverage", justify="right")
    table.add_column("Details")

    # Code coverage
    cov_data = run_pytest_coverage(repo_root)
    code_pct = _get_code_coverage_pct(cov_data)
    if code_pct is not None:
        table.add_row("Code Coverage", f"{code_pct:.1f}%", "pytest --cov")
    else:
        table.add_row("Code Coverage", "N/A", "Could not run pytest --cov")

    # Spec-to-test
    test_covered, test_total = check_spec_to_test_coverage(repo_root)
    if test_total > 0:
        pct = (test_covered / test_total) * 100
        table.add_row(
            "Spec-to-Test",
            f"{pct:.1f}%",
            f"{test_covered}/{test_total} requirements covered",
        )
    else:
        table.add_row("Spec-to-Test", "N/A", "No requirements parsed")

    # Spec-to-code
    code_covered, code_total = check_spec_to_code_coverage(repo_root)
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
