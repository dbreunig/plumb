# Init QoL Improvements Design

## Overview

Four quality-of-life improvements to the `plumb init` command: file suggestions for input prompts, stricter markdown validation, graceful pytest detection, and a step-by-step status spinner.

## 1. Spec Input with File Suggestions

Before prompting, scan the current directory for `.md` files and directories containing `.md` files (respecting `.plumbignore`). Display as a numbered list:

```
Found markdown files:
  [1] plumb_spec.md
  [2] docs/  (3 .md files)

Path to spec file or directory [1]:
```

- Default is `1` (first suggestion) if suggestions exist, otherwise `.`
- If only one `.md` file exists, pre-select it as default
- User can type a number to pick a suggestion or type a custom path
- Validation happens after selection

## 2. Test Input with Suggestions & Pytest Detection

Same suggestion pattern for test paths — scan for common test directories/files:

```
Found test paths:
  [1] tests/  (12 test files)
  [2] test/

Path to test file or directory [1]:
```

After path is resolved, detect pytest availability via `importlib.util.find_spec("pytest")`:

1. **Directory exists, pytest installed:** proceed normally.
2. **Directory exists, pytest NOT installed:** print a non-blocking note: `"Note: pytest was not detected. Currently, plumb only supports pytest. Install it with: pip install pytest"`. Continue with init.
3. **Directory doesn't exist:** create it (current behavior), then check for pytest as above.

## 3. Markdown Validation

Tighten validation to hard-fail on all bad input:

- **Path doesn't exist:** hard fail with error.
- **File doesn't end in `.md`:** hard fail: `"Error: '{path}' is not a markdown file. Plumb requires markdown spec files (.md)."`
- **Directory with no `.md` files:** hard fail: `"Error: No .md files found in '{path}'."`

Test path validation stays lenient (create directory if missing).

## 4. Step-by-Step Status Spinner

Wrap all post-input init steps in `console.status()` with updating text, matching the pattern used by `sync` and `coverage`:

```python
with console.status("[bold cyan]Initializing plumb...", spinner="dots") as status:
    status.update("[bold cyan]Creating .plumb/ directory...")
    status.update("[bold cyan]Saving configuration...")
    status.update("[bold cyan]Installing git hooks...")
    status.update("[bold cyan]Installing Claude skill...")
    status.update("[bold cyan]Updating CLAUDE.md...")
    status.update("[bold cyan]Creating .plumbignore...")
    status.update("[bold cyan]Parsing spec files...")
```

Spinner wraps everything after user input is collected. Final success summary prints after the spinner context exits.

## Constraints

- Stay within click for input (no new dependencies like prompt_toolkit)
- No pytest auto-install — instructions only
- Hard fail on invalid spec input
- Match existing `console.status()` spinner pattern from sync/coverage
