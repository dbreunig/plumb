# Plumb

*Plumb is proof of concept software demonstrating how we can manage the "Spec Driven Development Triange". It is very much alpha software, use cautiously. [Read the write up of the talk](https://www.dbreunig.com/2026/03/04/the-spec-driven-development-triangle.html) where I contextualize and introduce the Spec Driven Development Triangle, or [watch the video](https://www.youtube.com/watch?v=8TXAlOFkmk0).*

### A tool to keep things true.

`plumb` keeps your spec, tests, and code in sync during AI-assisted development.

When you work with a coding agent (Claude Code, Codex, Pi, Copilot CLI), decisions get made. A caching strategy is chosen. An API contract changes. A behavior is refined. These decisions live in conversation history and staged diffs, but they never make it back to the spec or tests. Over time the spec drifts from reality, the tests cover the wrong behavior, and the codebase becomes its own undocumented source of truth.

Plumb reads your staged changes and your agents' transcripts, extracts the decisions that were made, and writes them to an append-only log. It runs in one of two modes. In review mode, Plumb stops each commit until you approve, ignore, or reject each decision. In record mode, nothing blocks the commit, and Plumb records the decisions right after it lands. In both modes, `plumb sync` folds the accepted decisions back into the spec and tests, so every committed state has a spec and test suite that could reconstruct the program.

## Install

```
pip install plumb-dev
```

or

```
uv add plumb-dev
```

## Quick start

### Initialize

Plumb defaults to Anthropic's Haiku model, so it needs an `ANTHROPIC_API_KEY`.
Put one in your environment or in a `.env` file at the repo root. If you
configure a different provider with `plumb model`, set that provider's key
instead.

```
export ANTHROPIC_API_KEY=sk-ant-...
```

Then run init inside your project:

```
cd your-project
plumb init
```

Init asks where your spec markdown lives, where your tests live, which
mode Plumb should run in, and which model it should use.

### The two modes

**Review mode** stops each commit until you approve, ignore, or reject the
decisions Plumb found. Use it when you want to check every decision before
it lands, e.g., on a codebase where the spec is the contract.

**Record mode** never blocks a commit. After each commit, a background worker
extracts the decisions and appends them to the log with the commit SHA.
Use it when agents commit often or work unattended, and review the log
later with `plumb log`, `plumb search`, and `plumb review --recorded`.

Review is the default. You can switch at any time with `plumb mode record`
or `plumb mode review`.

### What init sets up

1. A `.plumb/` directory for state. Commit it to version control.
2. A pre-commit hook and a post-commit hook.
3. A Plumb block in `CLAUDE.md` and `AGENTS.md`, and a Claude Code skill
   at `.claude/skills/plumb/SKILL.md`.
4. A `.plumbignore` file that keeps irrelevant files out of analysis.
5. Your spec, parsed into requirements in `.plumb/requirements.json`.

From here, work normally and commit. In review mode the commit stops for
your review, and `plumb sync` folds approved decisions into the spec and
tests. In record mode the commit lands immediately, and you sync when you
choose.

## How it works

### Review mode, inside Claude Code

1. You run `git commit`, or Claude Code does
2. The pre-commit hook fires and analyzes the staged diff and the agent transcripts for this repo
3. It writes pending decisions and exits non-zero, which aborts the commit
4. Claude Code's skill reads the output and presents each decision:
   > **Question:** Should we cache API responses in memory or on disk?
   > **Decision made:** In-memory cache using a dict.
   > Approve, ignore, reject, or edit?
5. You answer in chat. The skill calls `plumb approve`, `plumb ignore`, `plumb reject`, or `plumb edit`
6. Rejected decisions trigger `plumb modify`, which rewrites the staged code
7. The skill runs `plumb sync`, which folds the approved decisions into your spec and tests, and stages the result
8. `git commit` runs again and lands

### Review mode, from the terminal

The flow is the same, but you resolve the decisions with `plumb review`.
Then you run `plumb sync`, stage its output, and commit again.

### Record mode

Nothing intercepts the commit. It lands immediately, and the post-commit
hook starts a background worker. The worker extracts decisions from the
commit's diff and the agent transcripts, and appends them to the log with
the commit SHA. This works the same whether you commit from the terminal
or an agent does.

Inside Claude Code, the Plumb block tells the agent to check `plumb log`
before ending a session and mention notable recorded decisions, and to run
`plumb search` before proposing something that may contradict a prior
decision. You review the record whenever you like with `plumb log`,
`plumb search`, and `plumb review --recorded`, and you run `plumb sync`
when you want the spec and tests updated.

Set `record_threshold` in `.plumb/config.json` to auto-record only
confident decisions. Decisions below the threshold are written as
`pending`, and you review them in a batch whenever you like. `plumb status`
shows how many recorded decisions have not been synced yet. Agents can
force a mode for one run with the `PLUMB_MODE` environment variable,
without touching the committed config. Rejecting a recorded decision never
rewrites code, because the code is already committed.

### Which agents Plumb reads

Plumb reads transcripts from Claude Code, Codex, Pi, and Copilot CLI (the Copilot adapter is unverified against live sessions). Sessions are matched to your repo by the working directory each agent records in its transcript, so linked worktrees attribute to the main repo and no path configuration is needed. Subagent sessions are read too. Every decision records which agent, session, and turn range it came from; `plumb log` shows decisions grouped by commit and agent, and `plumb log --verify` re-checks that evidence against the transcripts on disk.

## Models

Plumb addresses models with litellm strings, so any provider litellm supports
can serve inference. The default is `anthropic/claude-haiku-4-5`. During
`plumb init` you can keep the default or pick from these suggestions:

| Provider | Suggested model | Key to set |
|---|---|---|
| anthropic | `anthropic/claude-haiku-4-5` | `ANTHROPIC_API_KEY` |
| openai | `openai/gpt-4.1-mini` | `OPENAI_API_KEY` |
| groq | `groq/llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| gemini | `gemini/gemini-2.0-flash` | `GEMINI_API_KEY` |
| ollama | `ollama/llama3.1` | `OLLAMA_API_BASE` (a URL, no key) |

Any other litellm string works too. See the
[litellm provider list](https://docs.litellm.ai/docs/providers).

Change the model at any time with `plumb model <litellm-string>`. The command
tests the new model first and saves it only when the test passes. A failed
test leaves your config unchanged. Run `plumb model` with no argument to see
the current model and any per-program overrides.

You can override the model for a single Plumb program in
`.plumb/config.json`, e.g., to run deduplication on Groq:

```json
"program_models": {
  "decision_deduplicator": { "model": "groq/llama-3.3-70b-versatile", "max_tokens": 8192 }
}
```

## Commands

| Command | What it does |
|---|---|
| `plumb init` | Initialize Plumb in a git repo |
| `plumb status` | Show spec files, requirements, pending decisions, coverage |
| `plumb diff` | Preview what decisions Plumb would extract from staged changes |
| `plumb review` | Interactively review pending decisions in the terminal |
| `plumb review --recorded` | Walk auto-recorded decisions; reject records a reason without modifying code |
| `plumb mode [review\|record]` | Show or set how Plumb handles decisions |
| `plumb model [<litellm-string>]` | Show the inference model, or test and set a new one |
| `plumb approve <id>` | Approve a decision and sync it to spec/tests |
| `plumb approve --all` | Approve all pending decisions at once |
| `plumb reject <id> --reason "..."` | Reject a decision |
| `plumb edit <id> "new text"` | Amend a decision's text and approve it |
| `plumb modify <id>` | Auto-modify staged code to satisfy a rejected decision |
| `plumb sync` | Sync all unsynced approved/edited decisions to spec and tests |
| `plumb parse-spec` | Re-parse spec files into requirements |
| `plumb coverage` | Report code coverage, spec-to-test, and spec-to-code coverage |
| `plumb log` | Show decisions grouped by commit and agent; `--since <ref>`, `--verify` |
| `plumb search [query]` | Search the decision log; `--sort`, `--status`, `--agent`, `--branch`, `--file`, `--since`, `--json` |
| `plumb record-extract <sha>` | Record-mode worker launched by the post-commit hook |

## Coverage

Plumb tracks three dimensions of coverage:

- **Code coverage** — pytest line coverage via `pytest --cov`
- **Spec-to-test** — which requirements have corresponding tests
- **Spec-to-code** — which requirements have corresponding implementations

Run `plumb coverage` to see all three.

## `.plumbignore`

Plumb uses a `.plumbignore` file with gitignore-style patterns to exclude files from analysis. This keeps noise out of the decision extraction process — lock files, generated code, and other irrelevant diffs won't produce spurious decisions.

## Project State

All Plumb state lives in `.plumb/` at the repo root:

```
.plumb/
├── .gitignore           # Ignores the runtime files below
├── config.json          # Spec paths, test paths, mode, settings
├── coverage.json        # Cached coverage data
├── decisions/           # Append-only per-branch decision logs (*.jsonl)
├── record.lock          # Record mode: worker lock (not committed)
├── record.log           # Record mode: worker output (not committed)
└── requirements.json    # Parsed requirements from the spec
```

Commit this directory to version control.

## Requirements

- Python 3.10+
- A git repository
- An API key for your inference provider, in the environment or a `.env` file. The default model is Anthropic's, so this is `ANTHROPIC_API_KEY` unless you pick another provider with `plumb model`.

Note: `plumb init` needs the key, because it verifies API access and parses your spec. The hooks and `plumb sync` need it too. `plumb status`, `plumb log`, and `plumb search` work without it. `plumb review` works without it until you reject a decision in review mode, which runs the code modifier.

## License

MIT
