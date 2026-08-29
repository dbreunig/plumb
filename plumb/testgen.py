"""Sanitize LLM-generated pytest code before it is appended to the test suite.

`TestGenerator` output is free text. Three failure modes have bitten the
generated file in practice: a block that does not parse (which breaks
collection of the *entire* file), imports of names that do not exist in the
codebase (which fail at import time), and tests that assert behavior the code
never had (which fail forever). All three are cheap to detect mechanically,
so the generator never gets to write them:

    sanitize_generated_tests  -> parse check + plumb import resolution
    prune_failing_tests       -> run the survivors once, drop the failures
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_DEF_LINE = re.compile(r"^(?:async\s+def|def|class)\s+\w+")
_TEST_NAME = re.compile(r"^(?:async\s+)?def\s+(test_\w+)")


@dataclass
class SanitizeResult:
    code: str
    dropped: list[str] = field(default_factory=list)  # human-readable reasons

    @property
    def kept_tests(self) -> int:
        return len(re.findall(r"^(?:async )?def test_", self.code, re.MULTILINE))


# --------------------------------------------------------------------------- #
# Splitting
# --------------------------------------------------------------------------- #

def _excise_block_around(lines: list[str], lineno: int) -> tuple[list[str], str]:
    """Remove the top-level def/class block containing 1-based `lineno`.

    Falls back to removing just that line when no enclosing def/class exists.
    Returns (new_lines, label_of_removed_block).
    """
    i = lineno - 1
    start = i
    while start > 0 and not _DEF_LINE.match(lines[start]):
        if lines[start][:1] not in (" ", "\t", "\n", "") and not lines[start].startswith(("@", "#")):
            break  # hit an unrelated column-0 statement
        start -= 1
    if not _DEF_LINE.match(lines[start]):
        # no enclosing def: drop the single offending line
        label = lines[i].strip()[:80]
        return lines[:i] + lines[i + 1:], label
    # include decorators / comments directly above the def
    while start > 0 and lines[start - 1].lstrip().startswith(("@", "#")):
        start -= 1
    end = i + 1
    while end < len(lines) and (lines[end][:1] in (" ", "\t", "\n", "") or not _DEF_LINE.match(lines[end])
                                and not re.match(r"^[A-Za-z_@#]", lines[end])):
        end += 1
    # `end` now points at the next column-0 statement/def/decorator/comment
    label = next((l.strip()[:80] for l in lines[start:end] if _DEF_LINE.match(l)), lines[start].strip()[:80])
    return lines[:start] + lines[end:], label


def _parse_dropping_bad_blocks(source: str, max_rounds: int = 200) -> tuple[ast.Module, list[str], list[str]]:
    """Parse `source`, excising whole top-level blocks around each SyntaxError."""
    lines = source.splitlines(keepends=True)
    dropped: list[str] = []
    for _ in range(max_rounds):
        text = "".join(lines)
        try:
            return ast.parse(text), lines, dropped
        except SyntaxError as e:
            lineno = e.lineno or 1
            lines, label = _excise_block_around(lines, min(lineno, len(lines)))
            dropped.append(f"syntax error ({e.msg}) in: {label}")
            if not lines:
                break
    return ast.parse(""), [], dropped


def split_top_level_blocks(source: str) -> tuple[list[str], list[str]]:
    """Split into top-level statement blocks using the AST.

    Each block is the exact source of one top-level statement, plus any
    decorators and comment lines (e.g. ``# plumb:req-…`` markers) directly
    above it. Blocks that cannot be parsed are excised and reported.
    Returns (blocks, dropped_reasons).
    """
    tree, lines, dropped = _parse_dropping_bad_blocks(source)
    blocks: list[str] = []
    prev_end = 0  # 0-based index of the line after the previous node
    for node in tree.body:
        start = node.lineno - 1
        decos = getattr(node, "decorator_list", [])
        if decos:
            start = min(start, decos[0].lineno - 1)
        # attach comment lines immediately above
        while start > prev_end and lines[start - 1].lstrip().startswith("#"):
            start -= 1
        end = node.end_lineno  # 1-based inclusive -> 0-based exclusive
        blocks.append("".join(lines[start:end]))
        prev_end = end
    return blocks, dropped


# --------------------------------------------------------------------------- #
# Import resolution
# --------------------------------------------------------------------------- #

def _import_problem(node: ast.AST) -> str | None:
    """Reason string if a plumb import in `node` cannot resolve, else None."""
    if isinstance(node, ast.ImportFrom):
        if not node.module or node.module.split(".")[0] != "plumb":
            return None
        try:
            mod = importlib.import_module(node.module)
        except Exception as e:
            return f"cannot import {node.module}: {e.__class__.__name__}"
        for alias in node.names:
            if alias.name == "*" or hasattr(mod, alias.name):
                continue
            try:
                importlib.import_module(f"{node.module}.{alias.name}")
            except Exception:
                return f"{node.module} has no attribute {alias.name}"
        return None
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.split(".")[0] != "plumb":
                continue
            try:
                importlib.import_module(alias.name)
            except Exception as e:
                return f"cannot import {alias.name}: {e.__class__.__name__}"
    return None


def _partial_import(node: ast.ImportFrom) -> tuple[str, list[str]]:
    """For a module-level `from plumb.x import a, b, c`, keep the names that
    resolve and report the ones that don't. Returns (source or "", bad_names)."""
    try:
        mod = importlib.import_module(node.module)
    except Exception:
        return "", [a.name for a in node.names]
    good, bad = [], []
    for alias in node.names:
        ok = alias.name == "*" or hasattr(mod, alias.name)
        if not ok:
            try:
                importlib.import_module(f"{node.module}.{alias.name}")
                ok = True
            except Exception:
                ok = False
        (good if ok else bad).append(alias)
    if not good:
        return "", [a.name for a in bad]
    return ast.unparse(ast.ImportFrom(module=node.module, names=good, level=node.level)), [a.name for a in bad]


def _label(block: str) -> str:
    for line in block.splitlines():
        s = line.strip()
        if s and not s.startswith(("#", "@")):
            return s[:80]
    return block.strip()[:80] or "<empty>"


def sanitize_generated_tests(source: str) -> SanitizeResult:
    """Keep only blocks that parse and whose plumb imports resolve.

    A block that fails to parse is dropped. A block containing an import of a
    nonexistent plumb module/name is dropped — for a def that means the whole
    test, for a module-level import it means that import statement.
    """
    blocks, dropped = split_top_level_blocks(source)
    kept: list[str] = []
    for block in blocks:
        tree = ast.parse(block)
        node = tree.body[0] if len(tree.body) == 1 else None
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "plumb":
            rewritten, bad = _partial_import(node)
            if bad:
                dropped.append(f"{node.module} has no attribute {', '.join(bad)} in: {_label(block)}")
            if rewritten:
                kept.append(rewritten + "\n")
            continue
        reasons = sorted({r for n in ast.walk(tree) if (r := _import_problem(n))})
        if reasons:
            dropped.append(f"{'; '.join(reasons)} in: {_label(block)}")
            continue
        kept.append(block.rstrip("\n") + "\n")
    code = "\n\n".join(kept).strip("\n")
    return SanitizeResult(code=(code + "\n") if code else "", dropped=dropped)


# --------------------------------------------------------------------------- #
# Runtime pruning
# --------------------------------------------------------------------------- #

def _failing_test_names(test_file: Path, repo_root: Path) -> set[str] | None:
    """Run pytest on one file; return failing/erroring test names, or None if
    the run itself could not be interpreted (collection error, pytest missing)."""
    cmd = [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider",
           "--tb=no", "-rfE", "--no-header"]
    try:
        out = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout + out.stderr
    if "error during collection" in text or "ImportError while importing" in text:
        return None
    names: set[str] = set()
    for m in re.finditer(r"^(?:FAILED|ERROR) [^:\n]+::((?:[A-Za-z_]\w*::)?test_\w+)", text, re.MULTILINE):
        names.add(m.group(1))            # "Class::test_x" or "test_x"
        names.add(m.group(1).split("::")[-1])
    return names


def _remove_failing_from_class(block: str, failing: set[str]) -> tuple[str, list[str]]:
    """Drop failing test methods from a class block; return ("" , names) if
    no test methods remain."""
    tree = ast.parse(block)
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef)), None)
    if cls is None:
        return block, []
    lines = block.splitlines(keepends=True)
    offset = tree.body[0].lineno - 1  # block may start with comments/decorators
    removed: list[str] = []
    ranges: list[tuple[int, int]] = []
    tests_left = 0
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"):
            key = f"{cls.name}::{n.name}"
            if key in failing or n.name in failing:
                start = (n.decorator_list[0].lineno if n.decorator_list else n.lineno) - 1
                ranges.append((start, n.end_lineno))
                removed.append(key)
            else:
                tests_left += 1
    if not removed:
        return block, []
    if tests_left == 0:
        return "", removed
    for start, end in sorted(ranges, reverse=True):
        del lines[start:end]
    return "".join(lines), removed


def prune_failing_tests(code: str, repo_root: str | Path, test_dir: str | Path,
                        max_rounds: int = 3) -> SanitizeResult:
    """Run `code` as a temporary test module inside `test_dir` (so conftest
    fixtures apply) and drop every test function or method that fails or
    errors. Repeats until a run is clean (bounded), since removing one test
    can expose order-dependent failures in another."""
    repo_root = Path(repo_root)
    test_dir = Path(test_dir)
    dropped: list[str] = []
    if not code.strip() or not test_dir.is_dir():
        return SanitizeResult(code=code)
    for _ in range(max_rounds):
        failing = _run_once(code, repo_root, test_dir)
        if failing is None:
            dropped.append("could not run generated tests to prune failures")
            break
        if not failing:
            break
        blocks, _ = split_top_level_blocks(code)
        kept: list[str] = []
        for block in blocks:
            head = _strip_prefix(block)
            m = _TEST_NAME.search(head)
            if m and m.group(1) in failing:
                dropped.append(f"fails at runtime: {m.group(1)}")
                continue
            if head.startswith("class "):
                block, removed = _remove_failing_from_class(block, failing)
                dropped.extend(f"fails at runtime: {r}" for r in removed)
                if not block:
                    continue
            kept.append(block.rstrip("\n") + "\n")
        code = "\n\n".join(kept).strip("\n")
        code = (code + "\n") if code else ""
        if not code:
            break
    return SanitizeResult(code=code, dropped=dropped)


def _run_once(code: str, repo_root: Path, test_dir: Path) -> set[str] | None:
    fd, tmp_name = tempfile.mkstemp(prefix="_plumb_gencheck_", suffix=".py", dir=str(test_dir))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)
        return _failing_test_names(tmp, repo_root)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _strip_prefix(block: str) -> str:
    """Drop leading comment/decorator lines so the def line is first."""
    lines = block.splitlines()
    while lines and lines[0].lstrip().startswith(("#", "@")):
        lines.pop(0)
    return "\n".join(lines)


def clean_generated_tests(code: str, repo_root: str | Path, test_dir: str | Path) -> SanitizeResult:
    """sanitize_generated_tests followed by prune_failing_tests, merged."""
    s = sanitize_generated_tests(code)
    if not s.code:
        return s
    p = prune_failing_tests(s.code, repo_root, test_dir)
    return SanitizeResult(code=p.code, dropped=s.dropped + p.dropped)
