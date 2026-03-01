# Plumb: Specification

**Version:** 0.4.0-draft  
**Purpose:** This document is the authoritative spec for the Plumb Python library. It is intended to be fed to Claude Code to guide implementation. Implement exactly what is described here — no more, no less.

---

## Overview

Plumb is a Python library and CLI tool that keeps three artifacts in sync throughout a software project's lifecycle:

1. **The Spec** — one or more markdown files describing intended behavior as human-readable requirements
2. **The Tests** — a pytest-based test suite covering those requirements
3. **The Code** — the implementation

As code is written with an AI coding agent (Claude Code), decisions are made that deviate from the spec. Bugs are fixed. Features are refined. Plumb captures these decisions, surfaces them to the user, and ensures the spec, tests, and code are updated together so that on any given day, the spec and tests alone are sufficient to reconstruct the program.

### Design Principles

- **Simple over clever.** Plumb solves a bounded problem. It should be holdable in a single programmer's head.
- **DSPy for LLM workflows.** All LLM-powered functions are implemented as DSPy programs, not open-ended agents. This ensures they are controllable, auditable, and reliable.
- **Inference via Claude.** Plumb uses the Anthropic Claude SDK (via the user's existing account) as its inference provider.
- **Non-intrusive.** Plumb operates as a git hook and CLI tool. It does not change how the user writes code.
- **The commit is canonical.** The pre-commit hook ensures that every committed state has been reviewed and approved. A commit represents a fully reconciled snapshot of spec, tests, and code.
- **Conversation analysis is opportunistic.** Plumb uses the Claude Code conversation log when available. When it is not (e.g., committing from a bare terminal), Plumb falls back to diff-only analysis. Decisions still get captured; they are just derived from code changes rather than reasoning.

---

## Installation

Plumb must be installable via both `pip` and `uv`:

```
pip install plumb-dev
uv add plumb-dev
```

The package name on PyPI is `plumb-dev`. The CLI command is `plumb`.

### Dependencies

- `dspy` — LLM workflow programs
- `anthropic` — Claude SDK for inference
- `pytest` — test runner
- `pytest-cov` — coverage reporting
- `gitpython` — git history and diff access
- `click` — CLI framework
- `rich` — terminal output formatting
- `jsonlines` — reading/writing `.jsonl` decision logs

---

## Project Structure

```
plumb/
├── __init__.py
├── cli.py                  # Click CLI entrypoint
├── config.py               # Config loading/saving (.plumb/config.json)
├── git_hook.py             # Git pre-commit hook installer and runner
├── conversation.py         # Claude Code conversation history parser and chunker
├── decision_log.py         # Read/write .plumb/decisions.jsonl
├── coverage_reporter.py    # Code, spec, and test coverage analysis
├── sync.py                 # Spec and test sync logic
└── programs/               # DSPy programs
    ├── __init__.py
    ├── diff_analyzer.py        # Cluster and summarize git diffs
    ├── decision_extractor.py   # Extract decisions/questions from conversation chunks
    ├── requirement_parser.py   # Convert spec markdown into explicit requirements
    ├── spec_updater.py         # Rewrite spec to reflect approved decisions
    ├── test_generator.py       # Generate pytest stubs for uncovered requirements
    └── code_modifier.py        # Modify staged code to satisfy rejected decisions
├── skill/
│   └── SKILL.md                # Claude Code skill file, installed locally on plumb init
```

### `.plumb/` Directory

Plumb stores all state in a `.plumb/` folder at the root of the user's repository. This folder should be committed to version control.

```
.plumb/
├── config.json             # Spec paths, test paths, settings
├── decisions.jsonl         # Append-only log of all decisions
└── requirements.json       # Cached parsed requirements from the spec
```

---

## Core Workflow

Plumb intercepts commits via a **git pre-commit hook**. This is the central design decision: the commit is the gate, and nothing is committed until decisions have been reviewed.

There are two paths through review, both of which use the same underlying CLI commands and data structures:

### Path 1: Committing inside Claude Code (Conversational Review)

This is the primary workflow. The user works inside a Claude Code session. When they run `git commit` (or Claude Code runs it on their behalf), the pre-commit hook fires as a subprocess.

1. The hook analyzes the staged diff and the Claude Code conversation log.
2. It writes pending decisions to `decisions.jsonl`.
3. It **prints a machine-readable JSON summary of pending decisions to stdout** and **exits non-zero**, aborting the commit.
4. Claude Code's skill reads that output and begins presenting decisions to the user conversationally, one at a time:
   > "Plumb found 3 decisions before this commit. Here's the first one:
   > **Question:** Should we cache API responses in memory or on disk?
   > **Decision made:** In-memory cache using a dict.
   > Approve, reject, or edit?"
5. The user responds in the chat. The skill calls the appropriate per-decision command (`plumb approve <id>`, `plumb reject <id>`, or `plumb edit <id> "<text>"`).
6. For **rejected** decisions, the skill invokes `plumb modify <id>` (see below), which modifies the staged code, runs tests, and reports the result conversationally.
7. Once all decisions are resolved, the skill re-runs `git commit`. The hook fires again, finds no pending decisions, exits zero, and the commit lands.

**The commit only lands when there are zero pending decisions.**

### Path 2: Committing from the terminal (Interactive Review)

The user commits directly from a terminal, outside of Claude Code. The pre-commit hook fires the same way:

1. The hook analyzes the staged diff. Conversation analysis is skipped if no log is found (noted in output).
2. It writes pending decisions to `decisions.jsonl`.
3. It prints a human-readable summary of pending decisions and exits non-zero, aborting the commit.
4. The user runs `plumb review` in their terminal, which presents decisions interactively and accepts keypresses.
5. Rejected decisions can be modified via `plumb modify <id>`, which stages the modified code and reports test results.
6. The user re-runs `git commit`. Hook fires again, finds no pending decisions, exits zero. Commit lands.

Both paths use the same hook, the same decision log, the same per-decision commands, and the same sync logic. The only difference is who drives the review loop: Claude Code's skill or the interactive `plumb review` CLI.

---

## CLI Commands

All commands are invoked as `plumb <command>`.

---

### `plumb init`

Initializes Plumb in the current git repository.

**Behavior:**
1. Checks that the current directory is a git repository. Exits with an error if not.
2. Creates the `.plumb/` directory if it does not exist.
3. Prompts the user (interactively) to:
   - Provide a path to a spec file or directory of spec markdown files. Validates that the path exists and contains `.md` files.
   - Provide a path to a test file or test directory. Validates that the path exists.
4. Writes `.plumb/config.json` with the provided paths.
5. Installs the git pre-commit hook by writing a script to `.git/hooks/pre-commit` that calls `plumb hook`. Sets the script as executable.
6. Installs the Claude Code skill locally by copying `plumb/skill/SKILL.md` to `.claude/SKILL.md` in the project root. Creates `.claude/` if it does not exist. This is a project-local installation only — Plumb never writes to the user's global `~/.claude/` directory.
7. Appends a Plumb status block to `CLAUDE.md` at the project root (creating `CLAUDE.md` if it does not exist). See **CLAUDE.md Integration**.
8. Runs `plumb parse-spec` to do an initial parse of the spec into requirements.
9. Prints a confirmation summary to the terminal, including confirmation that the skill was installed at `.claude/SKILL.md`.

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

---

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
   - If found: reads and chunks turns since `last_commit` timestamp. Runs **Decision Extraction** per chunk.
   - If not found: skips conversation analysis. Notes `"conversation_available": false` in each decision object.
8. Merges and deduplicates decisions across chunks.
9. For each decision with no associated question, runs **Question Synthesizer**.
10. Writes all new decisions with `status: "pending"` to `decisions.jsonl`.
11. Runs `plumb parse-spec` to update requirements cache for any modified spec files.
12. **If pending decisions exist:**
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
13. **If no pending decisions exist:** Runs `plumb coverage`, prints a brief summary, updates `last_commit` and `last_commit_branch` in `config.json`, and **exits 0**, allowing the commit to proceed.

**The hook must never exit non-zero due to an internal Plumb error.** If Plumb itself fails, it prints a warning to stderr and exits 0 so the commit is not blocked.

---

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
3. If none, prints "No pending decisions." and exits 0.
4. For each pending decision, displays:
   - The framing question
   - The decision made (by user or LLM)
   - The branch it was made on
   - File and line references
   - Whether the commit SHA is reachable (`ref_status`)
   - The most related current spec text (if any)
5. Prompts the user:
   - `[a]pprove`
   - `[r]eject` (prompts for reason; optionally runs `plumb modify <id>` automatically)
   - `[e]dit` (user provides replacement decision text)
   - `[s]kip` (remains pending)
6. After all decisions are resolved, runs `plumb sync` for all approved/edited decisions.

---

### `plumb approve <id>`

Approves a single decision by ID. Updates its status to `approved` in `decisions.jsonl`. Then runs `plumb sync` for that decision only.

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
5. Runs `pytest` on the test suite.
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

**Plumb never commits the modification.** It only stages it. The user (or Claude Code) re-runs `git commit` after all decisions are resolved.

**Status values added for modification:** `rejected_modified` | `rejected_manual`

---

### `plumb sync`

Updates the spec and tests to reflect all approved and edited decisions. Can be run manually or is called automatically by `plumb approve` and `plumb edit`.

**Behavior:**
1. Reads decisions from `decisions.jsonl` with status `approved` or `edited` that have not yet been synced (no `synced_at` timestamp).
2. For each decision:
   - Runs **Spec Updater**: rewrites the relevant spec section so the result of the decision is captured as a natural requirement. The decision itself is not mentioned.
   - Writes the updated spec file to disk (temp file → rename).
3. Runs **Test Generator**: generates pytest stubs for requirements not covered by existing tests.
4. Writes generated stubs to the appropriate test file (temp file → rename).
5. Runs `plumb parse-spec` to re-cache requirements.
6. Sets `synced_at` on each processed decision.
7. Prints a summary of spec sections updated and test stubs created.

---

### `plumb parse-spec`

Parses all spec markdown files into an explicit list of requirements and caches them.

**Behavior:**
1. Reads all markdown files in `spec_paths` from `config.json`.
2. For each file or paragraph block, runs **Requirement Parser** to produce explicit, testable requirement statements.
3. Assigns each requirement a stable ID based on a hash of its content.
4. Writes results to `.plumb/requirements.json`. Requirements with matching hashes are not re-processed.

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

---

### `plumb coverage`

Reports coverage across all three dimensions.

**Behavior:**
1. **Code coverage:** Runs `pytest --cov` and parses output. Reports line coverage percentage.
2. **Spec-to-test coverage:** For each requirement in the cache, checks whether a test references or maps to it. Reports count and percentage covered.
3. **Spec-to-code coverage:** Uses the requirements cache to check whether each requirement has a corresponding implementation. Reports gaps.
4. Prints a formatted table using `rich`.

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

The skill file serves two purposes: it teaches Claude Code the Plumb workflow so it can guide the user naturally, and it provides the machine-readable protocol for parsing hook output and calling per-decision commands during conversational review.

### Skill File Location

- **Source (in Plumb package):** `plumb/skill/SKILL.md`
- **Installed to (per project):** `<project_root>/.claude/SKILL.md`
- **Scope:** Local to the project. Not installed globally. Not shared across projects.

The `.claude/` directory and `SKILL.md` should be committed to version control so all contributors to the project get the skill automatically.

### Skill File Content

The following is the exact content of `plumb/skill/SKILL.md`. Implement this file verbatim.

---

```markdown
# Plumb Skill

Plumb keeps the spec, tests, and code in sync. It intercepts every `git commit`
via a pre-commit hook, analyzes staged changes and conversation history, and
surfaces decisions for review before the commit lands.

## Your responsibilities when Plumb is active

### Before starting work
Run `plumb status` to understand the current state of spec/test/code alignment.
Note any pending decisions and any broken git references. Report a brief summary
to the user before proceeding.

### Before committing
Run `plumb diff` to preview what Plumb will capture from staged changes. Report
the estimated decisions to the user so they are not surprised during review.

### When git commit is intercepted
When you run `git commit` and it exits non-zero with Plumb output, do the
following:

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

3. Based on the user's response, call the appropriate command:
   - Approve: `plumb approve <id>`
   - Reject: `plumb reject <id> --reason "<user's reason>"` then immediately
     call `plumb modify <id>`
   - Edit: `plumb edit <id> "<new decision text>"`

4. For rejections, after calling `plumb modify <id>`, parse its JSON output:
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

5. Once all decisions are resolved, re-run `git commit`. The hook will fire
   again. If there are no pending decisions it will exit 0 and the commit will
   land. If new decisions are found (rare), repeat the review process.

### After committing
Run `plumb coverage` and briefly report the three coverage dimensions to the
user: code coverage, spec-to-test coverage, and spec-to-code coverage. Flag any
gaps that should be addressed before the next commit.

### Using coverage to guide work
When the user asks what to work on next, run `plumb coverage` to identify:
- Requirements with no corresponding tests (run `plumb parse-spec` first if the
  spec has changed)
- Requirements with no corresponding implementation
- Code with no test coverage

Present these gaps clearly so the user can prioritize.

## Rules

- Never edit `.plumb/decisions.jsonl` directly.
- Never edit `.plumb/config.json` directly. Use `plumb init` or `plumb status`.
- Never install the Plumb skill globally (`~/.claude/`). It is project-local only.
- The spec markdown files are the source of truth for intended behavior. Plumb
  keeps them updated as decisions are approved. Do not edit spec files to resolve
  decisions — let Plumb do it via `plumb sync`.
- Do not attempt to commit if there are decisions with `status: rejected_manual`.
  The user must resolve these manually first.

## Command reference

| Command | When to use |
|---|---|
| `plumb status` | Start of session, before beginning work |
| `plumb diff` | Before committing, to preview decisions |
| `plumb hook` | Called automatically by pre-commit hook — do not call manually |
| `plumb approve <id>` | User approves a decision during review |
| `plumb reject <id> --reason "<text>"` | User rejects a decision |
| `plumb modify <id>` | After rejection — auto-modify staged code |
| `plumb edit <id> "<text>"` | User amends decision text before approving |
| `plumb review` | Interactive terminal review (not needed in Claude Code) |
| `plumb sync` | Called automatically by approve/edit — updates spec and tests |
| `plumb coverage` | Report coverage across all three dimensions |
| `plumb parse-spec` | Re-parse spec after manual edits |
```

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

- Run `plumb status` before beginning work to understand current alignment.
- Run `plumb diff` before committing to preview what Plumb will capture.
- When `git commit` is intercepted by Plumb, present each pending decision to
  the user conversationally and call the appropriate command:
  - `plumb approve <id>` — user accepts the decision
  - `plumb reject <id> --reason "<text>"` — user rejects it; follow with `plumb modify <id>`
  - `plumb edit <id> "<new text>"` — user amends the decision text
- After all decisions are resolved, re-run `git commit`.
- Use `plumb coverage` to identify what needs to be implemented or tested next.
- Never edit `.plumb/decisions.jsonl` directly.
- Treat the spec markdown files as the source of truth for intended behavior.
  Plumb will keep them updated as decisions are approved.
<!-- plumb:end -->
```

---

## Conversation Log Parsing and Chunking

### Locating the Log

- Configurable in `.plumb/config.json` under `claude_log_path`.
- If not set, Plumb attempts to auto-detect using common Claude Code log locations.
- If not found, Plumb skips conversation analysis, notes `"conversation_available": false` in each decision, prints a warning, and continues with diff-only analysis.
- The log is a JSONL file where each line is a turn with at minimum `role` (`user` | `assistant`), `content` (string), and `timestamp`.
- Plumb reads only turns recorded after the `last_commit` timestamp in `config.json`.

### Chunking Strategy

Chunking is performed in `conversation.py` before any DSPy program is called. No LLM is involved in this step — chunks are created deterministically.

**Primary unit: user turn.** A chunk is one user message plus all following assistant turns up to but not including the next user message.

**Chunk size cap.** If a chunk exceeds 6,000 tokens, split at tool call boundaries. If no tool call boundary exists, split at the midpoint of the largest assistant turn.

**Overlap.** Prepend the final assistant turn of the previous chunk as a header to the next chunk. One turn of overlap preserves continuity without meaningfully increasing size.

**Noise reduction.** Before chunking, replace tool result turns longer than 500 tokens whose content appears to be a raw file read (heuristic: content begins with a file path or code fence) with `[file read: <filename>]`.

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

- Called once per chunk with the `diff_summary` passed identically to every call.
- Results merged after all chunks: near-duplicate decisions (same question + substantively same decision) are collapsed into one, preserving the earliest `chunk_index`.

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

**Input:** Raw unified diff string  
**Output:** List of change summaries, each with:
- `files_changed`: list of filenames
- `summary`: one-sentence description
- `change_type`: `"feature"` | `"bugfix"` | `"refactor"` | `"test"` | `"spec"` | `"config"` | `"other"`

Groups related changes into logical units. Does not invent meaning.

---

### `DecisionExtractor`

**Input:**
- `chunk`: single conversation chunk
- `diff_summary`: output of `DiffAnalyzer` (identical across all chunk calls)

**Output:** List of decision objects with `question`, `decision`, `made_by`, `related_diff_summary`, `confidence`.

Extracts explicit and implicit decisions. Does not extract trivial decisions (variable naming, import ordering).

---

### `QuestionSynthesizer`

**Input:** A decision object with no associated question  
**Output:** A plain-English question framing the decision for a developer

---

### `RequirementParser`

**Input:** Markdown string  
**Output:** List of requirement objects with `text` and `ambiguous` fields

Rules: atomic statements, active voice, no duplicates. Vague statements flagged `ambiguous: true` and excluded unless user approves.

---

### `SpecUpdater`

**Input:** `spec_section` (markdown), `decision` (approved decision object)  
**Output:** Updated markdown for that section

Rules: result of decision captured as natural requirement; no reference to the decision itself; existing formatting preserved.

---

### `TestGenerator`

**Input:** `requirements` (uncovered), `existing_tests` (file contents), `code_context` (relevant source)  
**Output:** pytest test stubs as a Python string

Rules: one function per requirement, descriptive names (`test_<req_id>_<description>`), stubs include `# TODO: implement` and `pytest.skip()`, no overwriting existing tests.

---

### `CodeModifier` (Claude API — not DSPy)

Used by `plumb modify`. This is the one place in Plumb where an open-ended agent call is used rather than a DSPy program, because code modification is inherently open-ended.

**Input:** staged diff, rejected decision, rejection reason, current spec  
**Output:** modified file contents that satisfy the rejection while remaining consistent with the spec

Called via the Anthropic API directly with a structured prompt. Plumb applies the output, runs pytest, and stages the result only if tests pass.

---

## Error Handling

- All CLI commands fail gracefully with a clear error message if `config.json` is missing or malformed.
- All DSPy programs retry on LLM failure (max 2 retries) then raise `PlumbInferenceError` with a human-readable message.
- The git hook **never** exits non-zero due to an internal Plumb error. Failures print a warning to stderr and exit 0.
- File writes (spec updates, test generation) use temp file → rename to avoid partial writes.
- If conversation log is unavailable, Plumb continues with diff-only analysis. This is not an error.
- If `plumb modify` test run fails, Plumb does not stage the modification and updates decision status to `rejected_manual`.

---

## Testing Plumb Itself

pytest, 80% coverage minimum for v0.1.0.

- `cli.py`: all commands run without error given valid inputs; per-decision commands update `decisions.jsonl` correctly
- `decision_log.py`: read/write/filter/dedup on `.jsonl`; latest-line-wins logic for status updates
- `git_hook.py`: hook produces correct pending decisions given mock diffs and conversation logs; amend detection; TTY vs non-TTY output formats
- `conversation.py`: correct chunk boundaries, overlap, noise reduction, metadata; oversized chunks split at tool call boundaries
- `programs/`: each DSPy program produces correctly structured output given fixture inputs (schema validity, not LLM quality)
- `coverage_reporter.py`: correct calculations given mock pytest output
- `sync.py`: spec and test files updated correctly given approved decisions; no partial writes

---

## Out of Scope for v0.1.0

- Web UI or dashboard
- Support for non-Python projects or non-pytest test frameworks
- Multi-user or team sync features
- Undoing decisions that are already committed (users should use their coding agent and commit; the resulting spec conflict will be surfaced during the next review)
- Integration with issue trackers (GitHub Issues, Linear, etc.)
- Automatic re-mapping of decisions to new SHAs after a rebase

---

## Glossary

| Term | Definition |
|---|---|
| **Spec** | One or more markdown files describing intended program behavior |
| **Requirement** | A single, atomic, testable statement of behavior extracted from the spec |
| **Decision** | A choice made in staged code (by user or LLM) that may not yet be captured in the spec |
| **Decision Log** | The append-only `.plumb/decisions.jsonl` file |
| **Chunk** | A user turn plus all following assistant turns up to the next user turn; the unit passed to `DecisionExtractor` |
| **Sync** | Updating the spec and tests to reflect approved decisions |
| **Broken Reference** | A decision whose `commit_sha` is no longer reachable in git history |
| **Conversational Review** | The review loop driven by the Claude Code skill, using per-decision commands |
| **Interactive Review** | The review loop driven by `plumb review` in a terminal |
| **Plumb** | This library |
