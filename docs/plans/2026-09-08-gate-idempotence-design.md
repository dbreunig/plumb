# Gate idempotence

**Date:** 2026-09-08
**Status:** Implemented

## Problem

In review mode the pre-commit hook re-runs extraction on every commit
attempt. The second attempt happens right after review and `plumb sync`.
Three defects combine into a loop:

1. The hook has no record that the staged diff was already reviewed. It
   extracts whenever the filtered diff is non-empty, and the original code
   diff is still staged.
2. The transcript watermark (`last_extracted_at`) points at the first
   attempt, so extraction reads the review conversation itself. That
   conversation restates every decision, and the extractor re-emits them in
   new words.
3. When the conversation window is empty, the diff-only fallback re-runs on
   the same diff summary and invents rephrasings from scratch. The dedup
   evidence veto compares evidence text, and a diff-only rephrasing never
   overlaps the original conversation evidence, so the veto blocks exactly
   the dedup that would break the loop.

`plumb sync` also stages generated tests, and the diff filter excludes spec
paths but not test paths, so the sync output reads as new work.

The result: after the user resolves every decision, the second commit
attempt produces fresh pending decisions and blocks again.

## Decision

Make the gate idempotent per commit attempt with a stored diff signature.
When the hook blocks, it records a signature of the staged diff. When the
hook runs again with the same signature and zero pending decisions, it
allows the commit without any extraction. The loop becomes structurally
impossible on this path: no LLM call happens, so no rephrasing can occur.

Also exclude the configured test paths from the analyzed diff, so sync's
generated tests never read as new work.

## Design

### Signature

The signature is the first field of `git patch-id --stable` computed over
the filtered staged diff. `patch-id` is stable across whitespace in context
and across recomputation. An empty filtered diff produces no output; the
signature for it is the literal string `"empty"`.

### Gate state

A new file `.plumb/gate.json` holds `{"diff_signature": "...",
"created_at": "..."}`. The file is transient and per-machine:

- `_PLUMB_GITIGNORE` gains a `gate.json` line.
- `ensure_plumb_dir` appends missing lines to an existing `.plumb/.gitignore`
  instead of writing the file only when absent, so initialized repos pick up
  the new entry.

### Hook flow

`_run_hook_inner` changes in three places:

1. **Short-circuit.** After the staged-diff step and amend detection, the
   hook computes the signature and loads the gate state. When the stored
   signature equals the current one and no pending decisions exist, the hook
   deletes the state file and returns 0. Nothing else runs: no transcript
   read, no diff analysis, no extraction, no dedup.
2. **Write on block.** Whenever the hook returns 1, it writes the gate state
   with the current signature. This covers both fresh extractions and runs
   that found nothing new but still block on existing pendings.
3. **Fallback guard.** The diff-only fallback runs only when no gate state
   exists. `extract_decisions` gains an `allow_diff_fallback: bool = True`
   parameter; the pre-commit path passes `False` when gate state is present.
   Record mode and `plumb diff` keep the default.

When the signature differs from the stored one, the agent staged more code
after review. The hook extracts normally, bounded by the existing transcript
watermark, and the fallback guard still applies.

`run_post_commit` deletes `gate.json` next to its existing watermark reset,
so a landed commit always clears the cycle. The `since_commit` cutoff then
puts the review conversation permanently behind the watermark: those turns
predate the commit and are never scanned again.

`plumb diff` (dry run) neither reads nor writes gate state. A preview stays
a preview.

### Test paths leave the analyzed diff

`_get_plumb_managed_paths` gains `config.test_paths`. Consequences:

- Sync's generated tests no longer enter diff analysis or the signature.
- Tests-only commits pass without review, because the filtered diff is
  empty. Conversation evidence about test decisions still reaches the
  extractor on the next code commit; only the diff summary loses test hunks.
- `file_refs` are unaffected. Hunks come from the raw staged diff, so
  decisions still reference test files an agent edited.

### Review cycle after the change

1. Commit attempt 1: hook extracts, writes pendings, writes `gate.json`,
   blocks.
2. User resolves decisions, runs `plumb sync`, commits again.
3. Commit attempt 2: signature matches, pendings are zero, hook returns 0
   with no LLM work. The commit lands.
4. Post-commit clears `gate.json` and moves the watermark.

## Non-goals

- Branch-scoped pending checks. The hook blocks on any pending decision,
  including old ones from other work; clearing that backlog is a separate
  task and a precondition for re-enabling the hook on this repo.
- Record mode changes. Record mode has no gate and no loop.
- LLM dedup tuning. The dedup contract and evidence veto stay as they are.

## Compatibility notes

- `gate.json` is transient. A stale file from an aborted cycle is harmless:
  the signature will not match the next unrelated diff, and post-commit
  deletes it.
- Existing repos gain the `.gitignore` entry on the next command that calls
  `ensure_plumb_dir`.
- Behavior change: tests-only commits no longer gate. The spec must state
  this.
