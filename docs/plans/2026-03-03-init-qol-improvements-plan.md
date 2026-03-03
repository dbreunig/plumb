# Init QoL Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Improve `plumb init` with file suggestions, markdown validation, pytest detection, and a progress spinner.

**Architecture:** Extract two helper functions (`_find_spec_suggestions` and `_find_test_suggestions`) that scan the repo for candidates, then refactor the init command to use numbered-list prompting, stricter validation, pytest detection, and `console.status()` for progress. All within existing click + Rich stack.

**Tech Stack:** Python, click, Rich (console.status), importlib.util, pathlib

---

### Task 1: Add `_find_spec_suggestions` helper

**Files:**
- Modify: `plumb/cli.py` (add function before `init` command, around line 40)
- Test: `tests/test_cli.py`

**Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
from plumb.cli import _find_spec_suggestions

class TestFindSpecSuggestions:
    def test_finds_md_files(self, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "design.md").write_text("# Design\n")
        suggestions = _find_spec_suggestions(tmp_repo)
        assert "spec.md" in suggestions
        assert "design.md" in suggestions

    def test_finds_dirs_with_md_files(self, tmp_repo):
        specs_dir = tmp_repo / "specs"
        specs_dir.mkdir()
        (specs_dir / "a.md").write_text("# A\n")
        (specs_dir / "b.md").write_text("# B\n")
        suggestions = _find_spec_suggestions(tmp_repo)
        # Should include the directory with a count
        assert any("specs/" in s for s in suggestions)

    def test_excludes_plumbignored_files(self, tmp_repo):
        (tmp_repo / "README.md").write_text("# Readme\n")
        (tmp_repo / "spec.md").write_text("# Spec\n")
        suggestions = _find_spec_suggestions(tmp_repo)
        assert not any("README.md" in s for s in suggestions)
        assert any("spec.md" in s for s in suggestions)

    def test_empty_repo_no_suggestions(self, tmp_repo):
        suggestions = _find_spec_suggestions(tmp_repo)
        assert suggestions == []
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py::TestFindSpecSuggestions -v`
Expected: FAIL with "cannot import name '_find_spec_suggestions'"

**Step 3: Write minimal implementation**

Add to `plumb/cli.py` after imports, before `@click.group()` (around line 34):

```python
def _find_spec_suggestions(repo_root: Path) -> list[str]:
    """Scan repo root for markdown files/dirs, respecting .plumbignore."""
    from plumb.ignore import parse_plumbignore, is_ignored

    patterns = parse_plumbignore(repo_root)
    suggestions: list[str] = []

    # Top-level .md files
    for f in sorted(repo_root.glob("*.md")):
        rel = f.name
        if not is_ignored(rel, patterns):
            suggestions.append(rel)

    # Directories containing .md files (one level deep)
    for d in sorted(repo_root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        rel = d.name + "/"
        if is_ignored(rel, patterns) or is_ignored(d.name, patterns):
            continue
        md_count = len(list(d.rglob("*.md")))
        if md_count > 0:
            suggestions.append(f"{d.name}/  ({md_count} .md file{'s' if md_count != 1 else ''})")

    return suggestions
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py::TestFindSpecSuggestions -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/cli.py tests/test_cli.py
git commit -m "feat(init): add _find_spec_suggestions helper"
```

---

### Task 2: Add `_find_test_suggestions` helper

**Files:**
- Modify: `plumb/cli.py` (add function after `_find_spec_suggestions`)
- Test: `tests/test_cli.py`

**Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
from plumb.cli import _find_test_suggestions

class TestFindTestSuggestions:
    def test_finds_tests_dir(self, tmp_repo):
        tests_dir = tmp_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text("def test_foo(): pass\n")
        (tests_dir / "test_bar.py").write_text("def test_bar(): pass\n")
        suggestions = _find_test_suggestions(tmp_repo)
        assert any("tests/" in s for s in suggestions)

    def test_finds_test_dir(self, tmp_repo):
        test_dir = tmp_repo / "test"
        test_dir.mkdir()
        (test_dir / "test_a.py").write_text("def test_a(): pass\n")
        suggestions = _find_test_suggestions(tmp_repo)
        assert any("test/" in s for s in suggestions)

    def test_no_test_dirs(self, tmp_repo):
        suggestions = _find_test_suggestions(tmp_repo)
        assert suggestions == []
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py::TestFindTestSuggestions -v`
Expected: FAIL with "cannot import name '_find_test_suggestions'"

**Step 3: Write minimal implementation**

Add to `plumb/cli.py` after `_find_spec_suggestions`:

```python
def _find_test_suggestions(repo_root: Path) -> list[str]:
    """Scan repo root for test directories/files."""
    suggestions: list[str] = []
    for name in ["tests", "test"]:
        d = repo_root / name
        if d.is_dir():
            test_files = list(d.rglob("test_*.py")) + list(d.rglob("*_test.py"))
            count = len(test_files)
            if count > 0:
                suggestions.append(f"{name}/  ({count} test file{'s' if count != 1 else ''})")
            else:
                suggestions.append(f"{name}/")
    return suggestions
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py::TestFindTestSuggestions -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/cli.py tests/test_cli.py
git commit -m "feat(init): add _find_test_suggestions helper"
```

---

### Task 3: Add `_prompt_with_suggestions` helper

**Files:**
- Modify: `plumb/cli.py`
- Test: `tests/test_cli.py`

**Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
from unittest.mock import patch
from plumb.cli import _prompt_with_suggestions

class TestPromptWithSuggestions:
    def test_pick_by_number(self):
        suggestions = ["spec.md", "docs/  (3 .md files)"]
        with patch("click.prompt", return_value="1"):
            result = _prompt_with_suggestions("Pick a spec", suggestions, default_no_suggestions=".")
            assert result == "spec.md"

    def test_pick_second_option(self):
        suggestions = ["spec.md", "docs/  (3 .md files)"]
        with patch("click.prompt", return_value="2"):
            result = _prompt_with_suggestions("Pick a spec", suggestions, default_no_suggestions=".")
            # For dirs, strip the count suffix
            assert result == "docs/"

    def test_custom_path(self):
        suggestions = ["spec.md"]
        with patch("click.prompt", return_value="my_spec.md"):
            result = _prompt_with_suggestions("Pick a spec", suggestions, default_no_suggestions=".")
            assert result == "my_spec.md"

    def test_no_suggestions_uses_default(self):
        with patch("click.prompt", return_value="."):
            result = _prompt_with_suggestions("Pick a spec", [], default_no_suggestions=".")
            assert result == "."

    def test_default_is_first_suggestion(self):
        suggestions = ["spec.md"]
        with patch("click.prompt", return_value="1") as mock_prompt:
            _prompt_with_suggestions("Pick a spec", suggestions, default_no_suggestions=".")
            # Check that default was "1"
            mock_prompt.assert_called_once()
            assert mock_prompt.call_args[1].get("default") == "1"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py::TestPromptWithSuggestions -v`
Expected: FAIL with "cannot import name '_prompt_with_suggestions'"

**Step 3: Write minimal implementation**

Add to `plumb/cli.py`:

```python
def _prompt_with_suggestions(prompt_text: str, suggestions: list[str], default_no_suggestions: str) -> str:
    """Show numbered suggestions, then prompt. Returns the resolved path string."""
    if suggestions:
        console.print(f"\n[bold]Found candidates:[/bold]")
        for i, s in enumerate(suggestions, 1):
            console.print(f"  [cyan][{i}][/cyan] {s}")
        console.print()
        answer = click.prompt(prompt_text, default="1")
    else:
        answer = click.prompt(prompt_text, default=default_no_suggestions)

    # If answer is a number, resolve it to the suggestion
    if answer.isdigit():
        idx = int(answer) - 1
        if 0 <= idx < len(suggestions):
            raw = suggestions[idx]
            # Strip count suffix like "  (3 .md files)" for dirs
            if "  (" in raw:
                raw = raw.split("  (")[0]
            return raw
    return answer
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py::TestPromptWithSuggestions -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/cli.py tests/test_cli.py
git commit -m "feat(init): add _prompt_with_suggestions helper"
```

---

### Task 4: Refactor init to use suggestions and stricter validation

**Files:**
- Modify: `plumb/cli.py:41-129` (the `init` function)
- Test: `tests/test_cli.py`

**Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
class TestInitValidation:
    def test_non_md_file_rejected(self, runner, tmp_repo):
        """Single file that's not .md should hard-fail."""
        (tmp_repo / "spec.txt").write_text("not markdown\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.cli._find_spec_suggestions", return_value=[]), \
             patch("plumb.cli._find_test_suggestions", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.txt\n")
            assert result.exit_code != 0
            assert "not a markdown file" in result.output.lower()

    def test_shows_spec_suggestions(self, runner, tmp_repo):
        """Init should display found .md files."""
        (tmp_repo / "my_spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="1\ntests/\n")
            assert result.exit_code == 0
            assert "my_spec.md" in result.output

    def test_shows_test_suggestions(self, runner, tmp_repo):
        """Init should display found test directories."""
        (tmp_repo / "spec.md").write_text("# Spec\n")
        tests_dir = tmp_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text("def test_foo(): pass\n")
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            result = runner.invoke(cli, ["init"], input="spec.md\n1\n")
            assert result.exit_code == 0
            assert "tests/" in result.output
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py::TestInitValidation -v`
Expected: FAIL (current init doesn't show suggestions or reject non-.md files)

**Step 3: Refactor the init command**

Replace the init function in `plumb/cli.py` (lines 41-129) with:

```python
@cli.command()
def init():
    """Initialize Plumb in the current git repository."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    # --- Collect user input (before spinner) ---

    # Spec path
    spec_suggestions = _find_spec_suggestions(repo_root)
    spec_input = _prompt_with_suggestions(
        "Path to spec file or directory of spec markdown files",
        spec_suggestions,
        default_no_suggestions=".",
    )
    spec_path = repo_root / spec_input
    if not spec_path.exists():
        console.print(f"[red]Error: Path '{spec_input}' does not exist.[/red]")
        raise SystemExit(1)
    if spec_path.is_file() and not spec_input.endswith(".md"):
        console.print(f"[red]Error: '{spec_input}' is not a markdown file. Plumb requires markdown spec files (.md).[/red]")
        raise SystemExit(1)
    if spec_path.is_dir():
        md_files = list(spec_path.rglob("*.md"))
        if not md_files:
            console.print(f"[red]Error: No .md files found in '{spec_input}'.[/red]")
            raise SystemExit(1)

    # Test path
    test_suggestions = _find_test_suggestions(repo_root)
    test_input = _prompt_with_suggestions(
        "Path to test file or test directory",
        test_suggestions,
        default_no_suggestions="tests/",
    )
    test_path = repo_root / test_input
    if not test_path.exists():
        console.print(f"[yellow]Warning: Path '{test_input}' does not exist. Creating it.[/yellow]")
        test_path.mkdir(parents=True, exist_ok=True)

    # Pytest detection
    import importlib.util
    if importlib.util.find_spec("pytest") is None:
        console.print(
            "\n[yellow]Note: pytest was not detected. Currently, plumb only supports pytest.\n"
            "Install it with: pip install pytest[/yellow]\n"
        )

    # --- Progress spinner for setup steps ---
    with console.status("[bold cyan]Initializing plumb...", spinner="dots") as status:
        # Create .plumb/
        status.update("[bold cyan]Creating .plumb/ directory...")
        ensure_plumb_dir(repo_root)

        # Save config
        status.update("[bold cyan]Saving configuration...")
        cfg = PlumbConfig(
            spec_paths=[spec_input],
            test_paths=[test_input],
            initialized_at=datetime.now(timezone.utc).isoformat(),
        )
        save_config(repo_root, cfg)

        # Install git hooks
        status.update("[bold cyan]Installing git hooks...")
        hooks_dir = repo_root / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        hook_path = hooks_dir / "pre-commit"
        hook_path.write_text("#!/bin/sh\nplumb hook\nexit $?\n")
        hook_path.chmod(0o755)
        post_commit_path = hooks_dir / "post-commit"
        post_commit_path.write_text("#!/bin/sh\nplumb post-commit\n")
        post_commit_path.chmod(0o755)

        # Create default .plumbignore
        status.update("[bold cyan]Creating .plumbignore...")
        plumbignore_path = repo_root / ".plumbignore"
        if not plumbignore_path.exists():
            plumbignore_path.write_text(DEFAULT_PLUMBIGNORE)

        # Install skill file
        status.update("[bold cyan]Installing Claude skill...")
        skill_dir = repo_root / ".claude" / "skills" / "plumb"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_src = Path(__file__).parent / "skill" / "SKILL.md"
        skill_dst = skill_dir / "SKILL.md"
        if skill_src.exists():
            shutil.copy2(str(skill_src), str(skill_dst))
        else:
            console.print("[yellow]Warning: SKILL.md source not found in package.[/yellow]")

        # CLAUDE.md integration
        status.update("[bold cyan]Updating CLAUDE.md...")
        _update_claude_md(repo_root, cfg)

        # Parse spec
        status.update("[bold cyan]Parsing spec files...")
        try:
            from plumb.sync import parse_spec_files
            parse_spec_files(repo_root)
        except Exception as e:
            console.print(f"[yellow]Warning: Could not parse spec: {e}[/yellow]")

    console.print(f"\n[green]Plumb initialized successfully![/green]")
    console.print(f"  Config: .plumb/config.json")
    console.print(f"  Hooks: .git/hooks/pre-commit, post-commit")
    console.print(f"  Ignore: .plumbignore")
    console.print(f"  Skill: .claude/skills/plumb/SKILL.md")
    console.print(f"  Spec: {spec_input}")
    console.print(f"  Tests: {test_input}")
```

**Step 4: Run all init tests to verify they pass**

Run: `pytest tests/test_cli.py::TestInit tests/test_cli.py::TestInitPlumbignore tests/test_cli.py::TestInitValidation -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plumb/cli.py tests/test_cli.py
git commit -m "feat(init): suggestions, validation, pytest detection, spinner"
```

---

### Task 5: Update existing init tests for new input format

**Files:**
- Modify: `tests/test_cli.py`

The existing `test_successful_init` and plumbignore tests send `input="spec.md\ntests/\n"` which worked with the old bare `click.prompt`. With the new `_prompt_with_suggestions`, suggestions will be shown and the input needs to match.

**Step 1: Update existing tests**

Adjust the existing tests to account for suggestions being shown. The simplest approach: mock `_find_spec_suggestions` and `_find_test_suggestions` to return empty lists so the prompts fall back to bare input (matching the old behavior). Alternatively, pass numbered input when suggestions would be shown.

For `TestInit.test_successful_init`:
```python
def test_successful_init(self, runner, tmp_repo):
    spec = tmp_repo / "spec.md"
    spec.write_text("# Spec\n")
    (tmp_repo / "tests").mkdir(exist_ok=True)

    with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
         patch("plumb.sync.parse_spec_files", return_value=[]):
        result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
        assert result.exit_code == 0
        assert "initialized" in result.output.lower()
```

Note: This should still work because `spec.md` typed as a string (not a number) is treated as a custom path. But run the tests and fix any that break.

**Step 2: Run all tests**

Run: `pytest tests/test_cli.py -v`
Expected: All PASS

**Step 3: Commit if any changes were needed**

```bash
git add tests/test_cli.py
git commit -m "test(init): update tests for new suggestion-based prompts"
```

---

### Task 6: Add pytest detection test

**Files:**
- Test: `tests/test_cli.py`

**Step 1: Write the test**

```python
class TestInitPytestDetection:
    def test_warns_when_pytest_missing(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]), \
             patch("importlib.util.find_spec", return_value=None):
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0
            assert "pytest was not detected" in result.output
            assert "pip install pytest" in result.output

    def test_no_warning_when_pytest_installed(self, runner, tmp_repo):
        (tmp_repo / "spec.md").write_text("# Spec\n")
        (tmp_repo / "tests").mkdir(exist_ok=True)
        with patch("plumb.cli.find_repo_root", return_value=tmp_repo), \
             patch("plumb.sync.parse_spec_files", return_value=[]):
            # Don't mock find_spec — pytest IS installed in test env
            result = runner.invoke(cli, ["init"], input="spec.md\ntests/\n")
            assert result.exit_code == 0
            assert "pytest was not detected" not in result.output
```

**Step 2: Run tests**

Run: `pytest tests/test_cli.py::TestInitPytestDetection -v`
Expected: PASS (implementation already exists from Task 4)

**Step 3: Commit**

```bash
git add tests/test_cli.py
git commit -m "test(init): add pytest detection tests"
```

---

### Task 7: Manual integration test

**Step 1: Run `plumb init` in the plumb repo itself to verify the full flow**

Run: `cd /tmp && mkdir test-init && cd test-init && git init && echo "# Spec" > spec.md && plumb init`

Verify:
- Spec suggestions are displayed with `[1] spec.md`
- Typing `1` selects `spec.md`
- Test path prompt shows (likely no suggestions since no tests/ dir)
- Spinner is visible during setup steps
- Success message prints
- If pytest is installed, no warning; if not, warning appears

**Step 2: Test edge cases manually**

- Run with no `.md` files and type a `.txt` file → should hard-fail
- Run with a directory containing `.md` files → should show as suggestion
- Type a custom path that doesn't exist → should hard-fail

**Step 3: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix(init): polish from integration testing"
```
