# Record Mode: Approval-Free Decision Capture

**Date:** 2026-08-29
**Status:** Proposed

## Problem

Plumb currently has one posture: **gate**. The pre-commit hook extracts decisions
from the staged diff and conversation, writes them as `pending`, and exits non-zero
to block the commit until a human approves, rejects, or edits each one.

This is the right default for attended work, but it doesn't fit unattended or
high-trust workflows: an agent running autonomously has no human available to
answer the gate, and a developer who trusts the extraction may not want to
adjudicate every decision at every commit. Both cases want Plumb to keep doing
its core job — capturing decisions and keeping the spec current — without a
synchronous approval step.

## Decision

Add a second mode, **record**, alongside the existing gate behavior. In record
mode, Plumb extracts decisions exactly as it does today, but marks them accepted
as-made, lets the commit through, and accumulates the results in the append-only
log. Review moves from synchronous-and-blocking to asynchronous-and-optional:
you can always contest a recorded decision after the fact, but nothing waits on
you.

The framing shifts from "Plumb asks permission" to "Plumb takes minutes." The
spec becomes a trailing record of what happened rather than a gate on what's
allowed.

## Design

### Config

Add to `PlumbConfig`:

```python
mode: str = "gate"                    # "gate" | "record"
record_threshold: float | None = None # confidence floor for auto-record; None = record all
```

- Set via a new `plumb mode <gate|record>` command, or chosen at `plumb init`.
- Per-invocation override for agents running unattended without touching the
  repo's committed config: a `PLUMB_MODE` environment variable, checked before
  the config field.

### Decision status and provenance

Do **not** reuse the `"approved"` status. That word currently means "a human
said yes," and `deduplicate_decisions()` treats approved/synced decisions as
validated choices that must never be re-proposed. Blurring the two would poison
that guarantee.

Instead:

- New status: `"recorded"`. Recorded decisions sync like approved ones (the
  sync filter becomes `status in ("approved", "edited", "recorded")`) and count
  as "existing" for dedup, but display distinctly in `plumb status` and
  `plumb review`.
- New field on `Decision`: `approved_by: Optional[str]` — `"user"`, `"agent"`,
  or `"auto"` — so provenance survives even if a status is later upgraded.
- Recorded decisions remain contestable: `plumb reject <id>` on a recorded
  decision works after the fact and triggers the existing `modify` path. This
  post-hoc revocability is the safety valve that replaces the up-front gate.

### Hook behavior

In record mode, `_run_hook_inner` runs steps 1–9 unchanged (diff, amend
detection, extraction, dedup, question synthesis), then:

- Step 10 writes decisions with `status="recorded"` and `approved_by="auto"`
  when `confidence >= record_threshold` (or always, when threshold is None).
  Below-threshold decisions are written as ordinary `pending`.
- The final pending check never returns 1. Pending decisions accumulate
  non-blocking and surface in `plumb status` for batch review later.
- Recorded decisions get `commit_sha` stamped by the post-commit hook, since
  the commit they belong to actually lands. This also makes amend detection and
  `delete_decisions_by_commit` cleanup more reliable than in gate mode, where
  decisions are extracted before their commit exists.

The threshold gives a spectrum of postures with one knob:

| Posture | `mode` | `record_threshold` | Effect |
|---|---|---|---|
| Gate (today) | `gate` | — | Every decision blocks the commit |
| Triage | `record` | e.g. `0.7` | High-confidence auto-recorded; the rest queue as pending, non-blocking |
| Full autopilot | `record` | `None` | Everything recorded |

### Sync timing

Three options considered:

1. **Sync inline pre-commit**, staging spec changes into the same commit —
   atomic, but slow, and surprising: the commit that lands isn't the commit
   that was reviewed.
2. **Sync post-commit** as an automatic follow-up commit — clean history, but
   commit noise, and an unattended LLM rewrite of the spec with nobody
   watching.
3. **Lazy sync with a visible debt counter** — recorded decisions accumulate
   as unsynced; `plumb status` shows "N recorded, unsynced"; `plumb sync`
   flushes on demand.

**Chosen: option 3.** The append-only log stays the moment-to-moment source of
truth, and spec regeneration remains a deliberate, reviewable event — which
matters because spec updates are themselves LLM output. Possible later
additions: auto-flush at a configurable threshold, or a pre-push nudge.

### Reviewing the record

Since nobody sees decisions at commit time, record mode needs a good
after-the-fact surface:

- `plumb log` — recorded decisions newest-first, grouped by commit, showing
  `made_by`, `confidence`, and sync state. `--since <ref>` to scope to recent
  work.
- `plumb review --recorded` — walk recent auto-recorded decisions with the
  same approve/reject/edit verbs. Approve upgrades `approved_by` to `"user"`;
  reject invokes the existing rollback/`modify` path; edit amends and re-syncs.

### Latency (follow-up)

Gate mode pays the multi-second LLM pipeline pre-commit because the pipeline's
output gates the commit. In record mode nothing gates the commit, so extraction
can move to the post-commit hook (which already exists for `last_commit`
bookkeeping) or a detached background process. This is a meaningful UX win but
is deliberately staged last — the mode is correct, just slower, without it.

### Skill / CLAUDE.md changes

`_update_claude_md()` writes a mode-appropriate block:

- **Gate mode:** unchanged — present each pending decision via
  `AskUserQuestion`; never resolve decisions on the user's behalf.
- **Record mode:** drop the approval choreography. Instead, instruct the agent
  to run `plumb log --since-last-push` before ending a work session and mention
  notable recorded decisions in its summary — the human hears about the
  interesting ones without gating on any of them.

### Non-goals

Record mode never auto-rejects or auto-edits. Append-and-accept is safe because
it is reversible and auditable; auto-rejection would mean unattended code
rewrites via `modify`, a much sharper tool. Rejection always requires an
explicit human (or human-directed) command.

## Implementation Order

1. Config field (`mode`, `record_threshold`, `PLUMB_MODE` override) +
   `"recorded"` status + `approved_by` provenance field.
2. Hook branch on mode: non-blocking exit, threshold split, post-commit
   `commit_sha` stamping.
3. `plumb log` and sync-debt display in `plumb status`; sync filter includes
   `"recorded"`.
4. `plumb review --recorded` and the mode-aware CLAUDE.md/skill block.
5. Move extraction post-commit for latency.

Steps 1–3 form a small, coherent first PR.
