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

2. **You MUST use `AskUserQuestion` to present decisions.** Do NOT print decision
   details as plain text and ask the user to respond. You MUST use the
   `AskUserQuestion` tool so the user sees the native multiple-choice UI.
   Present each decision with these options:
   - **Approve** (Recommended) — accept it and update the spec
   - **Approve with edits** — modify what the decision says before approving
   - **Ignore** — not spec-relevant; discard permanently
   - **Reject** — undo this change in the staged code

   Include the decision details in the question text:
   ```
   Plumb found [N] decision(s). Decision [X of N]:
   Question: [question]
   Decision: [decision]
   Made by: [made_by] (confidence: [confidence])
   ```

   This is non-negotiable. Presenting decisions as plain text defeats the
   purpose of structured review. Always use `AskUserQuestion`.

3. Based on the user's selection, call the appropriate command:
   - Approve: `plumb approve <id>`
   - Approve with edits: `plumb edit <id> "<new decision text from user>"`
   - Ignore: `plumb ignore <id>`
   - Reject: `plumb reject <id> --reason "..."`
     (modify runs automatically — no separate call needed)

   If the user approved ALL decisions with no edits, use `plumb approve --all`
   instead of approving each one individually.

4. **After all decisions are resolved, run `plumb sync`.** This updates the spec
   files and generates tests for approved decisions. You MUST run sync
   before re-committing — approved decisions that are not synced will leave the
   spec out of date.

5. Stage any files changed by sync (spec files, generated tests), then re-run
   `git commit`. Draft the commit message **after** decision review is complete
   and include a summary of approved decisions (e.g. "Approved: dec-abc123
   (added caching), dec-def456 (fixed retry logic)"). The hook will fire again.
   If there are no pending decisions it will exit 0 and the commit will land.
   If new decisions are found (rare), repeat the review process.

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

- **NEVER approve, reject, or edit decisions without explicit user instruction.**
  Every decision must be presented to the user via `AskUserQuestion`, and the user
  must tell you how to handle each one. Do not batch-approve, auto-approve, or
  assume the user's intent. This is the core purpose of Plumb — human review of
  decisions.
- **ALWAYS use `AskUserQuestion` to present decisions. NEVER print them as plain
  text.** The native multiple-choice UI is the only acceptable way to present
  decisions. If you present decisions as plain text you are doing it wrong.
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
| `plumb hook` | Called automatically by pre-commit hook |
| `plumb check` | Manually scan staged changes for decisions (alias for hook) |
| `plumb approve <id>` | User approves a decision during review |
| `plumb approve --all` | User approves all pending decisions at once |
| `plumb reject <id> --reason "<text>"` | User rejects a decision (auto-modifies code) |
| `plumb ignore <id>` | User marks a decision as not spec-relevant |
| `plumb modify <id>` | Called automatically by reject — do not call directly |
| `plumb edit <id> "<text>"` | User amends decision text before approving |
| `plumb review` | Interactive terminal review (not needed in Claude Code) |
| `plumb sync` | **Run after approving decisions** — updates spec and generates tests |
| `plumb coverage` | Report coverage across all three dimensions |
| `plumb parse-spec` | Re-parse spec after manual edits |
