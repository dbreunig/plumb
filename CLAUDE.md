<!-- plumb:start -->
## Plumb (Spec/Test/Code Sync)

This project uses Plumb to keep the spec, tests, and code in sync.

- **Spec:** plumb_spec.md
- **Tests:** tests/
- **Decision log:** `.plumb/decisions.jsonl`

### When working in this project:

- Run `plumb status` before beginning work to understand current alignment.
- Run `plumb diff` before committing to preview what Plumb will capture.
- When `git commit` is intercepted by Plumb, **use `AskUserQuestion`** to present
  each pending decision via the native multiple-choice UI. Options: Approve,
  Ignore, Reject. Then run the corresponding `plumb` command.
  **NEVER approve, reject, or edit decisions on the user's behalf.** This is
  non-negotiable.
- After all decisions are resolved, re-run `git commit`.
- Use `plumb coverage` to identify what needs to be implemented or tested next.
- Never edit `.plumb/decisions.jsonl` directly.
- Treat the spec markdown files as the source of truth for intended behavior.
  Plumb will keep them updated as decisions are approved.
<!-- plumb:end -->
