# Plumb

### A tool to keep things true.

Plumb keeps your spec, tests, and code in sync during AI-assisted development.

When you work with Claude Code, decisions get made — a caching strategy is chosen, an API contract changes, a behavior is refined. These decisions live in conversation history and staged diffs, but they never make it back to the spec or tests. Over time, the spec drifts from reality, tests cover the wrong behavior, and the codebase becomes its own undocumented source of truth.

Plumb fixes this by intercepting `git commit` via a pre-commit hook. It analyzes your staged changes and Claude Code conversation, extracts the decisions that were made, and gates the commit on your review. Approved decisions are automatically synced back to the spec and tests. Rejected decisions trigger code modifications to undo them. The result: every committed state has a spec and test suite that could reconstruct the program.

## Install

```
pip install plumb-dev
```

or

```
uv add plumb-dev
```

## Quick Start

```
cd your-project
plumb init
```

This will:

1. Ask for paths to your spec markdown and test directory
2. Create a `.plumb/` directory for state (commit this to version control)
3. Install a git pre-commit hook
4. Install a Claude Code skill file at `.claude/SKILL.md`
5. Add a Plumb block to `CLAUDE.md`
6. Parse your spec into requirements

From here, just work normally. Plumb activates when you commit.

## How It Works

### Committing inside Claude Code

1. You run `git commit` (or Claude Code does)
2. The pre-commit hook fires, analyzes the staged diff and conversation log
3. It writes pending decisions and exits non-zero, aborting the commit
4. Claude Code's skill reads the output and presents each decision:
   > **Question:** Should we cache API responses in memory or on disk?
   > **Decision made:** In-memory cache using a dict.
   > Approve, reject, or edit?
5. You respond in chat. The skill calls `plumb approve`, `plumb reject`, or `plumb edit`
6. Rejected decisions trigger `plumb modify`, which rewrites the staged code
7. Once all decisions are resolved, `git commit` runs again and lands

### Committing from the terminal

Same flow, but you drive it with `plumb review` instead of the skill.

## Commands

| Command | What it does |
|---|---|
| `plumb init` | Initialize Plumb in a git repo |
| `plumb status` | Show spec files, requirements, pending decisions, coverage |
| `plumb diff` | Preview what decisions Plumb would extract from staged changes |
| `plumb review` | Interactively review pending decisions in the terminal |
| `plumb approve <id>` | Approve a decision and sync it to spec/tests |
| `plumb reject <id> --reason "..."` | Reject a decision |
| `plumb edit <id> "new text"` | Amend a decision's text and approve it |
| `plumb modify <id>` | Auto-modify staged code to satisfy a rejected decision |
| `plumb sync` | Sync all unsynced approved/edited decisions to spec and tests |
| `plumb parse-spec` | Re-parse spec files into requirements |
| `plumb coverage` | Report code coverage, spec-to-test, and spec-to-code coverage |

## Coverage

Plumb tracks three dimensions of coverage:

- **Code coverage** — pytest line coverage via `pytest --cov`
- **Spec-to-test** — which requirements have corresponding tests
- **Spec-to-code** — which requirements have corresponding implementations

Run `plumb coverage` to see all three.

## Project State

All Plumb state lives in `.plumb/` at the repo root:

```
.plumb/
├── config.json          # Spec paths, test paths, settings
├── decisions.jsonl      # Append-only log of all decisions
└── requirements.json    # Parsed requirements from the spec
```

Commit this directory to version control.

## Requirements

- Python 3.10+
- A git repository
- An `ANTHROPIC_API_KEY` environment variable (for LLM-powered analysis)

Note: `plumb init`, `plumb status`, and `plumb review` work without an API key. The key is only needed when the hook analyzes diffs or when syncing decisions to spec/tests.

## License

MIT
