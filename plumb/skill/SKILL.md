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
