
# Plumb: Specification
**Version:** 0.2.1  **Purpose:** This document is the authoritative spec for the Plumb Python library. It is intended to be fed to Claude Code to guide implementation. Implement exactly what is described here — no more, no less.
## Overview
Plumb is a Python library and CLI tool that keeps three artifacts in sync throughout a software project's lifecycle:
1. **The Spec** — one or more markdown files describing intended behavior as human-readable requirements
2. **The Tests** — a comprehensive pytest-based test suite covering those requirements with unit tests, integration tests, and edge cases
3. **The Code** — the implementation

As code is written with an AI coding agent (Claude Code), decisions are made that deviate from the spec. Bugs are fixed. Features are refined. Plumb captures these decisions, surfaces them to the user, and ensures the spec, tests, and code are updated together so that on any given day, the spec and tests alone are sufficient to reconstruct the program.

Plumb supports loading environment variables from .env files to manage configuration settings. The `plumb init` command creates a .env file in the project directory to facilitate environment-based configuration management. The system supports ANTHROPIC_API_KEY configuration through both environment variables and .env files.

Tests are linked to requirements through requirement ID comments (e.g., `# plumb:req-XXXXXXXX`) to establish traceability between test functions and specific requirements. All tests are organized in the tests/ directory with specific test files for each component. Tests without requirement links are treated as sync violations that indicate either missing requirements or unnecessary tests.

The system supports file ignore patterns through a '.plumbignore' file and provides a '--all' command option for the 'plumb approve' command to approve all changes at once. Generated cache and coverage files are excluded from commits, and the system includes a 'check' command as an alias for manual decision scanning.

Plumb can handle conversations that span multiple Claude Code session files by reading and merging them chronologically to support exit-and-relaunch scenarios. The sync command provides progress indicators with status updates to give users feedback during operation. The workflow requires an explicit sync step after approving decisions, with staging of sync output before re-committing changes.

The system uses a branch-sharded structure for decision logs and provides CLI commands for merging decision logs from different branches. Users can migrate existing decision logs from the old monolithic format to the new branch-sharded layout through dedicated CLI commands. The `plumb merge-decisions <branch>` command allows users to merge decisions from feature branches back to main. A find_decision_branch() function helps locate which branch file contains a specific decision ID.

The core LLM integration is designed with a WholeFileSpecUpdater that takes full spec content and decisions as input, and outputs section_updates for existing sections and new_sections for brand new sections. This approach accepts rewriting whole sections instead of making surgical edits for markdown specs. An OutlineMerger component handles structural changes when new sections need to be added or content needs reordering. The system includes helper functions for DuckDB result type conversion, using _clean_duckdb_row() and _to_python_native() functions to convert DuckDB result types to Python native types for Pydantic compatibility. Comprehensive test coverage is implemented for migration and merge functionality, including edge cases and error conditions.
### Design Principles
- **Simple over clever.** Plumb solves a bounded problem. It should be holdable in a single programmer's head.- **DSPy for LLM workflows.** All LLM-powered functions are implemented as DSPy programs, not open-ended agents. This ensures they are controllable, auditable, and reliable.
- **Inference via Claude.** Plumb uses the Anthropic Claude SDK (via the user's existing account) as its inference provider. Claude Sonnet 4.6 serves as the default model for all programs.
- **Non-intrusive.** Plumb operates as a git hook and CLI tool. It does not change how the user writes code.
- **The commit is canonical.** The pre-commit hook ensures that every committed state has been reviewed and approved. A commit represents a fully reconciled snapshot of spec, tests, and code.
- **Conversation analysis is opportunistic.** Plumb uses Claude Code session data when available. When it is not (e.g., committing from a bare terminal), Plumb falls back to diff-only analysis. Decisions still get captured; they are just derived from code changes rather than reasoning.
- **Multi-stage deduplication.** The system prevents duplicate decisions through a multi-stage pipeline using exact matching, Jaccard similarity, and LLM semantic analysis to catch different types of duplicates.
- **Decision filtering.** Only prescriptive choices that affect system design, behavior, architecture, or data models are captured as decisions. Process observations, tooling choices, and diagnostic findings are filtered out.
- **Search-and-replace updates.** Spec updates use a search-and-replace approach with edits containing old_text/new_text pairs rather than full section replacement for more precise modifications.
- **Resilient header matching.** Section updates use exact header matching first, then fall back to normalized matching (stripped whitespace, case-insensitive) to handle minor variations in header formatting.

---
## Installation
Plumb must be installable via both `pip` and `uv`:
```
pip install plumb-dev
uv add plumb-dev
```

The package name on PyPI is `plumb-dev`. The CLI command is `plumb`.

The package includes a comprehensive project description sourced from the README.md file to provide clear information on PyPI about the library's purpose and functionality. README.md is included in the project configuration in pyproject.toml.
## Test-Requirement Linking
Tests must support two formats for linking to requirements:- Comment-based markers using `# plumb:req-XXXXXXXX` format
- Function name-based linking using `test_req_XXXXXXXX_` format

Both formats must be supported for backwards compatibility.

## Decision Extraction
The system must filter for spec-relevant content during decision extraction in both conversation analysis and diff analysis methods to ensure only pertinent decisions are processed.
## Decision Processing Requirements
The system must implement semantic similarity checking to handle duplicate or similar decisions effectively.
Classification tasks must not generate reasoning traces to maintain optimal performance.

LLM-based deduplication must utilize Claude Haiku 4.5 as the designated model.

The `deduplicate_decisions()` function must accept a `use_llm` parameter that defaults to False to ensure backward compatibility while enabling optional LLM-based deduplication.

The deduplication function must include fallback handling for truncated or failed LLM responses by returning all candidates when the LLM returns None or produces truncated output.
### Dependencies
- `dspy` — LLM workflow programs and framework for implementing LLM-based components- `anthropic` — Claude SDK for inference
- `pytest` — test runner
- `pytest-cov` — coverage reporting
- `gitpython` — git history and diff access
- `click` — CLI framework
- `rich` — terminal output formatting
- `jsonlines` — reading/writing `.jsonl` decision logs

### Integration
Plumb integrates with git hooks to automatically trigger workflows based on repository events. This enables seamless integration into the development process without requiring manual intervention.
---
## Project Structure
```plumb/
├── __init__.py
├── cli.py                  # Click CLI entrypoint
├── config.py               # Config loading/saving (.plumb/config.json)
├── git_hook.py             # Git pre-commit hook installer and runner
├── conversation.py         # Claude Code conversation history parser and chunker
├── decision_log.py         # Read/write .plumb/decisions.jsonl
├── coverage_reporter.py    # Code, spec, and test coverage analysis
├── sync.py                 # Spec and test sync logic
└── programs/               # DSPy programs using Predict pattern
    ├── __init__.py
    ├── diff_analyzer.py        # Cluster and summarize git diffs (excludes Plumb-managed files)
    ├── decision_extractor.py   # Extract decisions/questions from conversation chunks
    ├── requirement_parser.py   # Convert spec markdown into explicit requirements
    ├── spec_updater.py         # Rewrite spec to reflect approved decisions
    ├── test_generator.py       # Generate pytest tests for uncovered requirements
    └── code_modifier.py        # Modify staged code to satisfy rejected decisions
├── skill/
│   └── SKILL.md                # Claude Code skill file, installed locally on plumb init
```

DSPy programs support program-specific model configuration through a model configuration array in config.json. Pattern parsing logic is modularized into separate functions for reusability across programs, including an extract_outline function that extracts markdown headers from content.

The programs module includes token estimation, chunking, and concurrent mapper functionality to handle large datasets by breaking them into token-budget-sized chunks. The system uses chunked mapping with ThreadPoolExecutor for concurrent processing and supports different merge strategies for result combination.
### `.plumb/` Directory
Plumb stores all state in a `.plumb/` folder at the root of the user's repository. This folder should be committed to version control.
```
.plumb/
├── config.json             # Spec paths, test paths, settings
├── decisions.jsonl         # Append-only log of all decisions
└── requirements.json       # Cached parsed requirements from the spec
```

The system shall track which requirements have been modified and only send dirty (changed) requirements to the mapper for processing, rather than sending all requirements.

---
## Core Workflow
Plumb intercepts commits via a **git pre-commit hook**. This is the central design decision: the commit is the gate, and nothing is committed until decisions have been reviewed.
There are two paths through review, both of which use the same underlying CLI commands and data structures:

### Path 1: Committing inside Claude Code (Conversational Review)
This is the primary workflow. The user works inside a Claude Code session. When they run `git commit` (or Claude Code runs it on their behalf), the pre-commit hook fires as a subprocess.

1. The hook validates API access before proceeding with analysis. If API authentication fails, the hook must exit non-zero and block the commit.
2. The hook analyzes the staged diff and the Claude Code conversation log using the unified conversation reading system that auto-detects Claude Code sessions vs legacy logs and reads from Claude Code's actual session files at `~/.claude/projects/<encoded-path>/<uuid>.jsonl`.
3. It writes pending decisions to branch-specific decision log files with filesystem-safe path sanitization and sets the `last_extracted_at` timestamp.
4. It **prints a machine-readable JSON summary of pending decisions to stdout** and **exits non-zero**, aborting the commit.
5. Claude Code's skill reads that output and begins presenting decisions to the user using AskUserQuestion format, one at a time:
   > "Plumb found 3 decisions before this commit. Here's the first one:
   > **Question:** Should we cache API responses in memory or on disk?
   > **Decision made:** In-memory cache using a dict.
   > Approve, reject, or edit?"
6. The user responds in the chat. The skill calls the appropriate per-decision command (`plumb approve <id>`, `plumb reject <id>`, or `plumb edit <id> "<text>"`). The system includes explicit safeguards to never approve, reject, or edit decisions on the user's behalf. Users can approve multiple pending decisions efficiently using the `--all` flag with the approve command. Decision operations support branch-specific handling through branch parameter support.
7. For **rejected** decisions, the skill invokes `plumb modify <id>` (see below), which modifies the staged code, runs tests, and reports the result conversationally.
8. Once all decisions are resolved, the skill re-runs `git commit`. The hook fires again, finds no pending decisions, exits zero, and the commit lands. The system drafts commit messages after decision review and includes the list of approved decisions.

**The commit only lands when there are zero pending decisions.**

The pre-commit hook must validate API access before performing any LLM operations and must block commits when authentication fails. When API authentication fails, the system must raise a custom PlumbAuthError exception that alerts the user and provides clear instructions to set their API key via environment variable export or in a .env file. API key validation must be implemented as a separate validate_api_access function that is called before all LLM operations. All authentication functionality must have comprehensive test coverage, and existing tests must be updated to mock API validation calls to ensure test suites remain unaffected. The system supports environment variable management using .env files with python-dotenv dependency.

The system uses gitignore-style patterns for ignore functionality, with default patterns used when no .plumbignore file exists. After commit completion, the post-commit hook clears the `last_extracted_at` timestamp to reset the filter for future extractions.

The conversation parser handles Claude Code's type/message schema format and converts tool usage blocks to text format '[tool: Name] description' for consistency. The system includes comprehensive documentation for the Plumb skill with detailed instructions for managing spec/test/code alignment and decision review processes.

The system implements intelligent deduplication by first running Jaccard similarity filtering, then applying LLM deduplication as a second pass when 2 or more candidates remain, using DSPy's context manager with Haiku LM for deduplication while preserving Sonnet for other operations. The deduplication process prioritizes approved and synced decisions over recent ones in the comparison set and uses an expanded context window of 200 decisions instead of 50 for more comprehensive duplicate detection.

The system generates complete and runnable tests rather than stub files with TODO comments. Test generation uses increased context limits to accommodate complete test code. The test generator produces fully functional test code that can be executed immediately and only runs if tests don't already exist.

The system provides read_all_decisions() as the primary API function for accessing decisions across all branches. The review command pre-computes decision branches to optimize performance and ensure correct shard targeting for updates. Users can migrate from the monolithic decision file to the sharded layout using the plumb migrate command to convert existing decisions.jsonl to decisions/main.jsonl.

The system implements spec updates using a single LLM call per spec file with an output schema containing section_updates and new_sections, making a second call only when new_sections is non-empty. Structural reasoning for section placement is kept separate from content generation to maintain clarity. The system keeps edit operations simple without complex operation sets and accepts the risk of unintended edits in exchange for performance gains while mitigating through clear prompting.

The legacy decisions path function is maintained for migration detection purposes while the primary system operates on branch-specific decision logs.
### Path 2: Committing from the terminal (Interactive Review)
The user commits directly from a terminal, outside of Claude Code. The pre-commit hook fires the same way:

1. The hook analyzes the staged diff. It reads Claude Code's native session files directly from auto-detected paths (~/.claude/conversations.jsonl, etc.) and extracts prescriptive choices while excluding observations and diagnostics. Within-batch similarity deduplication prevents duplicate decisions in the same batch. Time awareness uses both last_commit datetime and last_extracted_at timestamp as cutoffs, taking the later of the two to determine which conversations to process.
2. It writes pending decisions to `decisions.jsonl`, passing current decisions and recent decisions from the last couple commits to the deduplication pass using a context window of 200 decisions and prioritizing approved/synced decisions over recent ones in the comparison set.
3. It prints a human-readable summary of pending decisions and exits non-zero, aborting the commit.
4. The user runs `plumb review` in their terminal, which presents decisions interactively and accepts keypresses.
5. Rejected decisions can be modified via `plumb modify <id>`, which stages the modified code and reports test results.
6. The user re-runs `git commit`. Hook fires again, finds no pending decisions, exits zero. Commit lands.

Both paths use the same hook, the same decision log, the same per-decision commands, and the same sync logic. The only difference is who drives the review loop: Claude Code's skill or the interactive `plumb review` CLI. The system uses exact deduplication and LLM-based semantic deduplication with groq/openai/gpt-oss-120b configured for both decision_deduplicator and question_synthesizer (8192 max_tokens). Source summaries are structured as per-file mappings to enable granular tracking. All documentation files including SKILL files and CLAUDE.md must be kept consistent with the same sync workflow requirements.
## CLI Commands
All commands are invoked as `plumb <command>`.
---

### `plumb init`
Initializes Plumb in the current git repository.
**Behavior:**
1. Checks that the current directory is a git repository. Exits with an error if not.
2. Creates the `.plumb/` directory if it does not exist.
3. Prompts the user (interactively) to:
   - Provide a path to a spec file or directory of spec markdown files. Validates that the path exists and contains `.md` files. Uses recursive search (rglob) to find markdown files within directories and suggests discovered spec files.
   - Provide a path to a test file or test directory. Validates that the path exists. Scans repository for test directories and files to provide suggestions.
4. Validates pytest test collection:
   - Checks that pytest is installed
   - Verifies test files exist at the specified path
   - Runs `pytest --collect-only` to ensure tests can be collected
   - Handles test_path as either directory or single file using is_dir() vs is_file() checks
   - Skips collection validation for empty test directories to avoid false positives
   - On collection failure, displays both stdout and stderr to help user debug and fails fast with SystemExit(1)
   - Treats infrastructure issues like TimeoutExpired and FileNotFoundError as warnings, not blocking errors
5. Writes `.plumb/config.json` with the provided paths.
6. Creates a `.plumbignore` file in the project root if it does not exist.
7. Installs the git pre-commit hook by writing a script to `.git/hooks/pre-commit` that calls `plumb hook`. Sets the script as executable.
8. Installs the Claude Code skill locally by copying `plumb/skill/SKILL.md` to `.claude/skills/plumb/SKILL.md` in the project root. Creates `.claude/skills/plumb/` directories if they do not exist. This is a project-local installation only — Plumb never writes to the user's global `~/.claude/` directory.
9. Appends a Plumb status block to `CLAUDE.md` at the project root (creating `CLAUDE.md` if it does not exist). See **CLAUDE.md Integration**.
10. Runs `plumb parse-spec` to do an initial parse of the spec into requirements.
11. Prints a confirmation summary to the terminal, including confirmation that the skill was installed at `.claude/skills/plumb/SKILL.md`.

**Config schema (`.plumb/config.json`):**
```json
{
  "spec_paths": ["docs/spec.md"],
  "test_paths": ["tests/"],
  "claude_log_path": null,
  "initialized_at": "<ISO timestamp>",
  "last_commit": null,
  "last_commit_branch": null
}
```
### `plumb hook`
Called automatically by the git pre-commit hook. Not intended to be called directly by users, but must work if called manually.
**Behavior:**
1. Reads `.plumb/config.json`. If not found, exits 0 silently (Plumb not initialized, do not block commit).
2. Gets the current staged diff via `git diff --cached`.
3. Gets the current branch name.
4. **Detects amends:** Compare the HEAD commit's parent SHA to `last_commit`. If equal, delete decisions in `decisions.jsonl` where `commit_sha == last_commit` before re-running analysis.
5. **Detects broken references (rebase):** Check all SHAs in `decisions.jsonl` against git history. Flag unreachable SHAs with `"ref_status": "broken"` and include a warning in output.
6. Runs the **Diff Analysis** DSPy program on the staged diff.
7. Attempts to locate and read the Claude Code conversation log (see **Conversation Log Parsing and Chunking**).
   - If found: reads and chunks turns since `last_commit` timestamp. Uses unified conversation reading interface to support multiple sessions. Merges conversation turns from all relevant session files modified after the last commit and sorts chronologically. Handles multi-line assistant responses that span multiple JSONL lines. Converts tool_use blocks to '[tool: Name] description' format in conversation turns. Runs **Decision Extraction** per chunk.
   - If not found: skips conversation analysis. Notes `"conversation_available": false` in each decision object.
8. Filters out decisions marked as non-spec-relevant during extraction before creating Decision records.
9. Merges and deduplicates decisions across chunks using DuckDB for cross-shard queries to read and deduplicate decisions across all branch JSONL files. Deduplication checks against all existing decisions (both pending and resolved) instead of only resolved ones. Implements within-batch similarity deduplication to compare new decisions against each other using Jaccard similarity check.
10. For each decision with no associated question, runs **Question Synthesizer** with program-specific model configuration.
11. Writes all new decisions with `status: "pending"` to `decisions.jsonl`. Implements decisions sharding using a TDD approach with core refactor, DuckDB integration, caller updates, new commands, and comprehensive testing coverage.
12. Runs `plumb parse-spec` to update requirements cache for any modified spec files. Batches spec updates across all decisions instead of running spec update for each individual decision. Uses search-and-replace pairs pattern for spec updates, where LLM returns list of {old_text, new_text} operations instead of rewriting entire sections. Implements a two-tier approach for spec updates: Tier 1 for existing section updates (section-header-keyed replacement) and Tier 2 for structural changes (new sections, reordering). For structural changes, uses outline-first approach where LLM returns updated header outline first, then fills in content for new sections. Creates OutlineMerger signature for conditional second call that takes current outline and new headers, returns merged outline with proper positioning.
13. **If pending decisions exist:**
    - Checks whether it is running in a TTY (interactive terminal) or as a subprocess (e.g., called by Claude Code).
    - **TTY:** Prints a human-readable summary of pending decisions with instructions to run `plumb review`.
    - **Non-TTY (subprocess):** Prints a machine-readable JSON object to stdout:
      ```json
      {
        "pending_decisions": 3,
        "decisions": [
          {
            "id": "dec-abc123",
            "question": "...",
            "decision": "...",
            "made_by": "llm",
            "confidence": 0.87
          }
        ]
      }
      ```
    - **Exits non-zero** in both cases, aborting the commit.
14. **If no pending decisions exist:** Updates `last_commit` and `last_commit_branch` in `config.json`, and **exits 0**, allowing the commit to proceed.

**The hook must never exit non-zero due to an internal Plumb error.** If Plumb itself fails, it prints a warning to stderr and exits 0 so the commit is not blocked.
### `plumb hook --dry-run`
Runs the full hook analysis on staged changes but does not write to `decisions.jsonl` and always exits 0. Equivalent to `plumb diff`. Intended for testing and preview.
---

### `plumb diff`
Previews what Plumb will capture from currently staged changes. Read-only.
**Behavior:**
1. Reads staged changes via `git diff --cached`.
2. Runs **Diff Analysis** on the staged diff.
3. Reads and chunks the conversation log (turns since `last_commit`) if available.
4. Runs **Decision Extraction** per chunk.
5. Prints a preview to the terminal:
   - Summary of staged code changes
   - Estimated number of decisions that would be captured
   - Brief description of each estimated decision
6. Makes no writes to `.plumb/`.

---

### `plumb review`
Interactive review of pending decisions. Intended for terminal (TTY) use.
**Behavior:**
1. Reads `.plumb/decisions.jsonl`, filters for `status == "pending"`.
2. Accepts an optional `--branch <name>` flag to filter by branch.
3. When multiple session files exist, uses the most recent session file by modification time or matches the current session ID for session selection.
4. If no pending decisions exist, prints "No pending decisions." and exits 0.
5. For each pending decision, displays:
   - The framing question
   - The decision made (by user or LLM)
   - The branch it was made on
   - File and line references
   - Whether the commit SHA is reachable (`ref_status`)
   - The most related current spec text (if any)
6. Prompts the user:
   - `[a]pprove`
   - `[r]eject` (prompts for reason; optionally runs `plumb modify <id>` automatically)
   - `[e]dit` (user provides replacement decision text)
   - `[s]kip` (remains pending)
7. After all decisions are resolved, runs `plumb sync` for all approved/edited decisions.

**Decision Classification:**
- Defaults `spec_relevant` field to `True` to ensure decisions pass through for user review when LLM classification is uncertain.

---
### `plumb approve <id>`
Approves a single decision by ID. Updates its status to `approved` in `decisions.jsonl`. Then runs `plumb sync` for that decision only.
The status display should include a visual bar chart representation for coverage statistics and use Rich's Status spinner that updates in-place for progress indication.

Intended to be called by the Claude Code skill during conversational review.

---
### `plumb reject <id> [--reason "<text>"]`
Rejects a single decision by ID. Updates its status to `rejected` and records the reason. Does not modify code or spec.
Intended to be called by the Claude Code skill during conversational review. After calling this, the skill should call `plumb modify <id>` to resolve the rejected decision in the staged code.

---

### `plumb edit <id> "<new decision text>"`
Replaces the decision text for a given decision ID with the user-provided text. Updates status to `edited`. Then runs `plumb sync` for that decision only.
Intended to be called by the Claude Code skill when the user wants to modify what the decision says before approving it.

---

### `plumb modify <id>`
Modifies the staged code to satisfy a rejected decision. This is the automatic code modification path, available for v0.1.0 because rejected decisions only ever touch uncommitted, staged code.
**Behavior:**
1. Reads the decision object for `<id>` from `decisions.jsonl`. Verifies `status == "rejected"`.
2. Reads the staged diff (the code that introduced the decision).
3. Calls the Claude API (not a DSPy program — an open-ended agent call is appropriate here because code modification is inherently open-ended) with:
   - The staged diff
   - The decision that was made
   - The rejection reason
   - The current spec
   - An instruction to modify the staged code to satisfy the rejection while keeping behavior consistent with the spec
4. Applies the proposed modification to the staged files.
5. Runs `pytest` on the test suite to verify that tests still pass after modification.
   - If tests pass: prints the resulting diff to the terminal (or returns it as JSON in non-TTY mode). Stages the modified files. Updates the decision status to `"rejected_modified"`.
   - If tests fail: prints the failure output. Does **not** stage the modification. Prompts the user to resolve manually. Updates decision status to `"rejected_manual"`.
6. In non-TTY mode (Claude Code), returns a machine-readable JSON result:
   ```json
   {
     "id": "dec-abc123",
     "result": "modified",
     "tests_passed": true,
     "diff": "..."
   }
   ```

**`plumb` never commits the modification.** It only stages it. The user (or Claude Code) re-runs `git commit` after all decisions are resolved.

**Status values added for modification:** `rejected_modified` | `rejected_manual`

---
### `plumb sync`
Updates the spec and tests to reflect all approved and edited decisions. Can be run manually or is called automatically by `plumb approve` and `plumb edit`.
**Behavior:**
1. Adds early exit check to avoid expensive operations when no unsynced decisions exist.
2. Reads decisions from `decisions.jsonl` with status `approved` or `edited` that have not yet been synced (no `synced_at` timestamp).
3. For each spec file:
   - Runs **WholeFileSpecUpdater**: processes all decisions affecting the spec file together and returns section_updates for existing sections and new_sections for brand new sections
   - If new_sections is non-empty, runs **OutlineMerger** to determine proper positioning of new sections within the spec structure
   - Writes the updated spec file to disk (temp file → rename). Fixes newline formatting by ensuring proper newlines between section headers and body content.
4. Runs **Test Generator**: generates organized tests for requirements not covered by existing tests. Tests are organized by functionality with proper requirement links in the format `test_<req_id>_<description>`. The generator reads only source files referenced by `file_refs` on synced decisions for context.
5. Writes generated tests to `test_generated.py` (temp file → rename).
6. Runs `plumb parse-spec` to re-cache requirements.
7. Sets `synced_at` on each processed decision.
8. Prints a summary of spec sections updated and test files created.

**Default Configuration:**
- Uses `.plumbignore` file with default patterns for documentation, build files, and IDE configurations to exclude irrelevant files from processing.
### `plumb parse-spec`
Parses all spec markdown files into an explicit list of requirements and caches them.
**Behavior:**
1. Reads all markdown files in `spec_paths` from `config.json`.
2. For each file or paragraph block, runs **Requirement Parser** to produce explicit, testable requirement statements.
3. Assigns each requirement a stable ID based on a hash of its content.
4. Writes results to `.plumb/requirements.json`. Requirements with matching hashes are not re-processed, preserving existing metadata including `created_at` and `last_seen_commit` timestamps.
5. Uses regex patterns `PLUMB_MARKER_RE` and `FUNC_NAME_RE` to extract requirement IDs from test files for coverage mapping.
6. Processes all requirements and AST summaries of every non-test `.py` file through the CodeCoverageMapper using chunked mapping approach to avoid context overflow. Uses fan-out chunking to group source files under token budget (default 60000 tokens) and broadcasts requirements to each chunk.
7. Implements granular per-file hashing with structured v2 cache format that tracks individual requirement results for efficient cache invalidation. Preserves existing incremental caching logic by applying chunking inside the LLM call after dirty requirements are determined.
8. Extracts source file paths from evidence strings using pattern matching against known files to determine requirement-to-file mappings. Uses greedy packing to group source/test summaries into chunks and ThreadPoolExecutor for concurrent chunk processing.
9. Merges results from multiple chunks using custom logic that ORs 'implemented' flags and concatenates evidence strings per requirement_id.

**Requirements cache schema:**
```json
[
  {
    "id": "req-001",
    "source_file": "docs/spec.md",
    "source_section": "Authentication",
    "text": "The system must reject login attempts with invalid credentials.",
    "ambiguous": false,
    "created_at": "<ISO timestamp>",
    "last_seen_commit": "<SHA>"
  }
]
```
### `plumb coverage`
Reports coverage across all three dimensions.
**Behavior:**
1. **Code coverage:** Runs `pytest --cov` and parses output. Reports line coverage percentage.
2. **Spec-to-test coverage:** For each requirement in the cache, checks whether a test references or maps to it using string-matching to look for requirement IDs (like 'req-79c36afc') literally in test files. Also scans for link markers in test files to determine coverage. Reports count and percentage covered.
3. **Spec-to-code coverage:** Uses the requirements cache to check whether each requirement has a corresponding implementation by using string-matching to look for requirement IDs literally in source files. Reports gaps.
4. Prints a formatted table using `rich`.
5. Uses LLM calls to refresh the requirements cache for accurate coverage analysis.
6. Measures and reports coverage improvement achieved by marker injection.

---
### `plumb status`
Prints a human-readable summary:
- Tracked spec files and total requirements
- Number of tests
- Pending decisions, with branch breakdown if spanning multiple branches
- Decisions with broken git references
- Last sync commit
- Coverage summary (all three dimensions)

---

## Claude Code Skill
### Overview
Plumb ships with a Claude Code skill file at `plumb/skill/SKILL.md`. During `plumb init`, this file is copied to `.claude/SKILL.md` in the project root — a project-local installation only. It is never installed globally. Claude Code automatically reads files in `.claude/` at the start of each session, so no additional configuration is required after `plumb init`.
The skill file provides structured guidance for AI-assisted development workflow. It teaches Claude Code the Plumb workflow so it can guide the user naturally through the development process, and it provides the machine-readable protocol for parsing hook output and calling per-decision commands during conversational review.

The system supports reading Claude Code native session files with JSONL parsing from the `~/.claude/projects/` directory structure. Conversation logs are located in the real session JSONL files using the format `~/.claude/projects/<project>/<session-uuid>.jsonl`.
### Skill File Location
- **Source (in Plumb package):** `plumb/skill/SKILL.md`- **Installed to (per project):** `<project_root>/.claude/SKILL.md`
- **Scope:** Local to the project. Not installed globally. Not shared across projects.

The `.claude/` directory and `SKILL.md` should be committed to version control so all contributors to the project get the skill automatically.

### Skill File Content
The following is the exact content of `plumb/skill/SKILL.md`. Implement this file verbatim.
---

```markdown
# Plumb Skill
Plumb keeps the spec, tests, and code in sync. It intercepts every `git commit`via a pre-commit hook, analyzes staged changes and conversation history, and
surfaces decisions for review before the commit lands.

## Your responsibilities when Plumb is active
### Before starting work
Run `plumb status` to understand the current state of spec/test/code alignment.Note any pending decisions and any broken git references. Report a brief summaryto the user before proceeding.

### Before committing
Run `plumb diff` to preview what Plumb will capture from staged changes. Reportthe estimated decisions to the user so they are not surprised during review.
### When git commit is intercepted
When you run `git commit` and it exits non-zero with Plumb output, do thefollowing:
1. Parse the JSON from stdout. It will have this shape:
   ```json
   {
     "pending_decisions": 2,
     "decisions": [
       {
         "id": "dec-abc123",
         "question": "...",
         "decision": "...",
         "made_by": "llm",
         "confidence": 0.87
       }
     ]
   }
   ```

2. Present each decision to the user conversationally, one at a time. Use this
   format:
   ---
   Plumb found [N] decision(s) to review before this commit.

   **Decision [X of N]**
   **Question:** [question]
   **Decision made:** [decision]
   **Made by:** [made_by] (confidence: [confidence])

   How would you like to handle this?
   - **Approve** — accept it and update the spec
   - **Reject** — undo this change in the staged code
   - **Edit** — modify what the decision says before approving
   ---

3. All decisions must be explicitly reviewed and approved by the user. No decisions shall be automatically approved without explicit user instruction.

4. Based on the user's response, call the appropriate command:
   - Approve: `plumb approve <id>`
   - Reject: `plumb reject <id> --reason "<user's reason>"` (automatically triggers modification workflow)
   - Edit: `plumb edit <id> "<new decision text>"`

5. For rejections, the system automatically calls `plumb modify <id>` and parses its JSON output:
   ```json
   {
     "id": "dec-abc123",
     "result": "modified",
     "tests_passed": true,
     "diff": "..."
   }
   ```
   - If `tests_passed` is true: show the user the diff and confirm the change
     looks correct before proceeding.
   - If `tests_passed` is false: inform the user that automatic modification
     failed, show them the test output, and ask them to resolve it manually.
     The decision status will be `rejected_manual` — the user must fix the code
     themselves before committing.

6. Rejected decisions are marked with 'ignored' status rather than being deleted from the system.

7. Once all decisions are resolved, the user must run `plumb sync` to update the spec with approved decisions before re-running `git commit`. The hook will fire again. If there are no pending decisions it will exit 0 and the commit will land. If new decisions are found (rare), repeat the review process. The sync operation must identify and use the correct decision branch when marking decisions as synced.

8. All CLI operations require explicit user commands. The system shall not perform automatic synchronization or background operations without direct user instruction.
### After committing
Run `plumb coverage` and briefly report the three coverage dimensions to theuser: code coverage, spec-to-test coverage, and spec-to-code coverage. Flag anygaps that should be addressed before the next commit.

### Using coverage to guide work
When the user asks what to work on next, run `plumb coverage` to identify:- Requirements with no corresponding tests (run `plumb parse-spec` first if the  spec has changed)
- Requirements with no corresponding implementation
- Code with no test coverage

Present these gaps clearly so the user can prioritize.

## Rules
- Never edit `.plumb/decisions.jsonl` directly.- Never edit `.plumb/config.json` directly. Use `plumb init` or `plumb status`.
- Never install the Plumb skill globally (`~/.claude/`). It is project-local only.
- The spec markdown files are the source of truth for intended behavior. Plumb
  keeps them updated as decisions are approved. Do not edit spec files to resolve
  decisions — let Plumb do it via `plumb sync`.
- Do not attempt to commit if there are decisions with `status: rejected_manual`.
  The user must resolve these manually first.
- When handling decision conflicts, use opposite ordering rules: for duplicate decisions, keep the earlier one (lower index); for countermanded decisions, keep the later one (higher index).
- Use section-header-keyed full replacement instead of search-and-replace for markdown spec editing.
## Command reference
| Command | When to use ||---|---|
| `plumb status` | Start of session, before beginning work |
| `plumb diff` | Before committing, to preview decisions |
| `plumb hook` | Called automatically by pre-commit hook — do not call manually |
| `plumb approve <id>` | User approves a decision during review |
| `plumb reject <id> --reason "<text>"` | User rejects a decision |
| `plumb modify <id>` | After rejection — auto-modify staged code |
| `plumb edit <id> "<text>"` | User amends decision text before approving |
| `plumb review` | Interactive terminal review (not needed in Claude Code) |
| `plumb sync` | Called automatically by approve/edit — updates spec and tests |
| `plumb coverage` | Run coverage across all three dimensions |
| `plumb parse-spec` | Re-parse spec after manual edits |

The conversation log reader must work seamlessly across different repositories without requiring additional user setup or configuration overhead.

---
## CLAUDE.md Integration
`plumb init` appends the following block to `CLAUDE.md`. Delimited by comment markers so future Plumb commands can update it without affecting surrounding content.
```markdown
<!-- plumb:start -->
## Plumb (Spec/Test/Code Sync)
This project uses Plumb to keep the spec, tests, and code in sync.
- **Spec:** <spec_paths from config>
- **Tests:** <test_paths from config>
- **Decision log:** `.plumb/decisions.jsonl`

### When working in this project:
- Run `plumb status` before beginning work to understand current alignment.- Run `plumb diff` before committing to preview what Plumb will capture.
- When `git commit` is intercepted by Plumb, present each pending decision to
  the user using structured decision presentation and call the appropriate command:
  - `plumb approve <id>` — user accepts the decision
  - `plumb reject <id> --reason "<text>"` — user rejects it; follow with `plumb modify <id>`
  - `plumb edit <id> "<new text>"` — user amends the decision text
- After all decisions are resolved, re-run `git commit`.
- Use `plumb coverage` to identify what needs to be implemented or tested next.
- Never edit `.plumb/decisions.jsonl` directly.
- Treat the spec markdown files as the source of truth for intended behavior.
  Plumb will keep them updated as decisions are approved.
- When parsing session files, skip assistant entries that contain 'thinking' blocks, have 'isSidechain: true', or have 'isMeta: true'.
<!-- plumb:end -->
```

---
## Conversation Log Parsing and Chunking
### Locating the Log
- Configurable in `.plumb/config.json` under `claude_log_path`.- If not set, Plumb attempts to auto-detect using common Claude Code log locations.
- If not found, Plumb skips conversation analysis, notes `"conversation_available": false` in each decision, prints a warning, and continues with diff-only analysis.
- The log is a JSONL file where each line is a turn with at minimum `role` (`user` | `assistant`), `content` (string), and `timestamp`.
- Session files are pre-filtered by modification time to avoid reading old files, then individual entries are filtered by timestamp.
- Plumb reads only turns recorded after the `last_commit` timestamp in `config.json`.
- The conversation parser handles the native session JSONL format with type/message schema used by Claude Code.
### Chunking Strategy
Chunking is performed in `conversation.py` before any DSPy program is called. No LLM is involved in this step — chunks are created deterministically.
**Primary unit: user turn.** A chunk is one user message plus all following assistant turns up to but not including the next user message.

**Chunk size cap.** If a chunk exceeds 6,000 tokens, split at tool call boundaries. If no tool call boundary exists, split at the midpoint of the largest assistant turn.

**Overlap.** Prepend the final assistant turn of the previous chunk as a header to the next chunk. One turn of overlap preserves continuity without meaningfully increasing size.

**Noise reduction.** Before chunking, replace tool result turns longer than 500 tokens whose content appears to be a raw file read (heuristic: content begins with a file path or code fence) with `[file read: <filename>]`.

**Multi-line assistant parsing.** Assistant messages that span multiple JSONL entries (one content block per line) must be properly parsed and reassembled.

**Chunk metadata:**
```json
{
  "chunk_index": 0,
  "start_timestamp": "<ISO>",
  "end_timestamp": "<ISO>",
  "truncated": false,
  "turns": [...]
}
```
### Running DecisionExtractor per Chunk
- Called once per chunk with the `diff_summary` passed identically to every call.- Results merged after all chunks: near-duplicate decisions (same question + substantively same decision) are collapsed into one, preserving the earliest `chunk_index`.

---

## Git Edge Case Handling
### Amends
The pre-commit hook fires on amends. To prevent duplicates: compare HEAD's parent SHA to `last_commit`. If equal, delete decisions where `commit_sha == last_commit` before re-running analysis.
### Rebases
On every hook run, check all stored SHAs against git history. Unreachable SHAs are flagged `"ref_status": "broken"`. Plumb does not attempt to re-map decisions to new SHAs. The user must review and re-resolve broken-reference decisions manually.
---

## Decision Log Schema
Append-only. Existing lines are never modified in place. Status updates are written as new lines with the same `id`. The latest line for a given `id` is canonical.
```json
{
  "id": "dec-<uuid4>",
  "status": "pending",
  "question": "Should authentication tokens expire after inactivity or only on logout?",
  "decision": "Tokens expire after 30 minutes of inactivity.",
  "made_by": "user",
  "commit_sha": null,
  "branch": "feature/auth",
  "ref_status": "ok",
  "conversation_available": true,
  "file_refs": [
    {"file": "src/auth.py", "lines": [42, 58]}
  ],
  "related_requirement_ids": ["req-014"],
  "confidence": 0.91,
  "chunk_index": 2,
  "conversation_truncated": false,
  "rejection_reason": null,
  "user_note": null,
  "synced_at": null,
  "reviewed_at": null,
  "created_at": "2025-02-28T14:32:00Z"
}
```

**Status values:** `pending` | `approved` | `edited` | `rejected` | `rejected_modified` | `rejected_manual`  
**ref_status values:** `ok` | `broken`

Note: `commit_sha` is null until the commit lands. It is populated by the hook on the second pass (when no pending decisions remain and the commit proceeds).

---

## DSPy Programs
### `DiffAnalyzer`
**Input:** Raw unified diff string  **Output:** List of change summaries, each with:
- `files_changed`: list of filenames
- `summary`: one-sentence description
- `change_type`: `"feature"` | `"bugfix"` | `"refactor"` | `"test"` | `"spec"` | `"config"` | `"other"`

Groups related changes into logical units. Does not invent meaning.

---

### `DecisionExtractor`
**Input:**- `chunk`: single conversation chunk
- `diff_summary`: output of `DiffAnalyzer` (identical across all chunk calls)

**Output:** List of decision objects with `question`, `decision`, `made_by`, `related_diff_summary`, `confidence`.

Extracts explicit and implicit decisions. Does not extract trivial decisions (variable naming, import ordering).

---

### `QuestionSynthesizer`
**Input:** A decision object with no associated question  **Output:** A plain-English question framing the decision for a developer

---

### `RequirementParser`
**Input:** Markdown string  **Output:** List of requirement objects with `text` and `ambiguous` fields

Rules: atomic statements, active voice, no duplicates. Vague statements flagged `ambiguous: true` and excluded unless user approves.

---

### `SpecUpdater`
**Input:** `spec_section` (markdown), `decision` (approved decision object)  **Output:** Updated markdown for that section

Rules: result of decision captured as natural requirement; no reference to the decision itself; existing formatting preserved.

---

### `TestGenerator`
**Input:** `requirements` (uncovered), `existing_tests` (file contents), `code_context` (relevant source)**Output:** Runnable pytest test code as a Python string

Rules: one function per requirement, descriptive names (`test_<req_id>_<description>`), tests contain real assertions against actual code under test, no `pytest.skip()` or `# TODO: implement`, no overwriting existing tests.

---

### `CodeModifier` (Claude API — not DSPy)
Used by `plumb modify`. This is the one place in Plumb where an open-ended agent call is used rather than a DSPy program, because code modification is inherently open-ended.
**Input:** staged diff, rejected decision, rejection reason, current spec  
**Output:** modified file contents that satisfy the rejection while remaining consistent with the spec

Called via the Anthropic API directly with a structured prompt. Plumb applies the output, runs pytest, and stages the result only if tests pass.

---

## Error Handling
- All CLI commands fail gracefully with a clear error message if `config.json` is missing or malformed.- All DSPy programs retry on LLM failure (max 2 retries) then raise `PlumbInferenceError` with a human-readable message.
- The git hook **never** exits non-zero due to an internal Plumb error. Failures print a warning to stderr and exit 0.
- File writes (spec updates, test generation) use temp file → rename to avoid partial writes.
- If conversation log is unavailable, Plumb continues with diff-only analysis. This is not an error.
- If `plumb modify` test run fails, Plumb does not stage the modification and updates decision status to `rejected_manual`.
- The `plumb status` command operates in cache-only mode to avoid making LLM calls.
- The `plumb coverage` command displays progress using an in-place updating spinner, then prints the final results table.
- Session file reading is optimized by pre-filtering based on file modification times to skip files that cannot contain relevant conversations.

---
## Testing Plumb Itself
pytest, 80% coverage minimum for v0.1.0.
- `cli.py`: all commands run without error given valid inputs; per-decision commands update `decisions.jsonl` correctly; coverage command provides progress feedback with progress bar and status text; spec and test suggestion functions cover various directory scenarios
- `decision_log.py`: read/write/filter/dedup on `.jsonl`; latest-line-wins logic for status updates
- `git_hook.py`: hook produces correct pending decisions given mock diffs and conversation logs; amend detection; TTY vs non-TTY output formats
- `conversation.py`: correct chunk boundaries, overlap, noise reduction, metadata; oversized chunks split at tool call boundaries
- `programs/`: each DSPy program produces correctly structured output given fixture inputs (schema validity, not LLM quality); comprehensive test suite for chunking functionality covering token estimation, item chunking, and chunked mapper execution scenarios
- `coverage_reporter.py`: correct calculations given mock pytest output
- `sync.py`: spec and test files updated correctly given approved decisions; no partial writes; newline formatting tests covering both apply_section_updates() and insert_new_sections() functions
## Out of Scope for v0.1.0
- Web UI or dashboard- Support for non-Python projects or non-pytest test frameworks
- Multi-user or team sync features
- Undoing decisions that are already committed (users should use their coding agent and commit; the resulting spec conflict will be surfaced during the next review)
- Integration with issue trackers (GitHub Issues, Linear, etc.)
- Automatic re-mapping of decisions to new SHAs after a rebase

---

## Glossary
| Term | Definition ||---|---|
| **Spec** | One or more markdown files describing intended program behavior |
| **Requirement** | A single, atomic, testable statement of behavior extracted from the spec |
| **Decision** | A choice made in staged code (by user or LLM) that may not yet be captured in the spec |
| **Decision Log** | The append-only `.plumb/decisions.jsonl` file |
| **Chunk** | A user turn plus all following assistant turns up to the next user turn; the unit passed to `DecisionExtractor` |
| **Sync** | Updating the spec and tests to reflect approved decisions |
| **Broken Reference** | A decision whose `commit_sha` is no longer reachable in git history |
| **Conversational Review** | The review loop driven by the Claude Code skill, using per-decision commands |
| **Interactive Review** | The review loop driven by `plumb review` in a terminal |
| **Programs Module** | A dedicated module containing LLM programs, separate from core library functionality |
| **Plumb** | This library |
| **Ignore Module** | A dedicated module (ignore.py) that handles file exclusion functionality |
| **.plumbignore** | A configuration file using gitignore-style patterns to exclude files from plumb operations and analysis |
| **Claude Session Module** | A module (plumb/claude_session.py) that reads Claude Code native session files from ~/.claude/projects/ |
| **Decision Cycling** | The phenomenon where LLMs rephrase decisions slightly differently on each run, causing them to slip past deduplication thresholds |
| **DecisionDeduplicator** | A dedicated module that performs LLM-based deduplication of decisions |