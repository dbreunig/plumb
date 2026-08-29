# Record Mode and `plumb search`

**Date:** 2026-08-29 (revised after the multi-agent traces work landed)
**Status:** Proposed
**Companion:** [Multi-agent traces](2026-08-29-multi-agent-traces-design.md) —
implemented; owns stages 1–2 (find + normalize, extract + enrich). This doc
owns stage 3 (resolve + store) and the read side.

## User story

1. A developer runs `plumb init` in a project and chooses **record**, not
   **review**.
2. From then on Plumb appends decisions to that project's local decision log
   after every commit, without asking anything and without slowing the commit.
3. The developer searches the log with `plumb search`, which uses DuckDB to
   enumerate decisions and lets them sort by date, relevance, confidence, and
   filter by agent, status, branch, file.

## Problem

Plumb has one posture today: **review** (the pre-commit gate). The hook
extracts decisions from the staged diff and the agents' transcripts, writes
them as `pending`, and exits non-zero until a human approves, ignores, or
rejects each one. Two things are wrong with that as the only posture:

- Unattended and high-trust workflows have nobody to answer the gate. An agent
  running autonomously blocks forever; a developer who trusts extraction still
  pays a review round per commit — and in practice (this repo, 2026-08-29) the
  gate re-extracted re-phrasings of already-approved decisions from the same
  diff, producing three review rounds and a `--no-verify`.
- Even when the gate is wanted, the LLM pipeline runs *inside* `git commit`
  (~30 s), because its output decides whether the commit lands.

Once decisions are recorded rather than gated, the log grows quickly and the
questions change from "approve this?" to "what did we decide about X?", "what
happened on this branch last week?", "which decisions touched `auth.py`?".
`plumb log` answers the second; nothing answers the first and third.

## Decision

Add a second mode, **record**, and a **search** command.

- **Modes are `review` and `record`.** (The earlier draft said "gate"; the
  user-facing word is `review` because that is what the human does.) The
  default stays `review`.
- **Record mode moves extraction out of the commit path.** The pre-commit hook
  does nothing; the post-commit hook launches extraction for the commit that
  just landed. The commit returns immediately. Decisions are written with
  `status="recorded"` and their `commit_sha` set directly — there is no
  "second pass" and no amend guesswork, because the commit already exists.
- **Record never auto-rejects or auto-edits.** Append-and-accept is reversible
  and auditable; rejection stays a human command.
- **Search is DuckDB over the append-only JSONL shards** — the same query
  `read_all_decisions` already uses — with filters and sorts in SQL and a small
  in-process BM25 for relevance. No index, no extra store: the log is the
  database.

## Design

### Config and mode selection

`PlumbConfig` gains:

```python
mode: str = "review"                  # "review" | "record"
record_threshold: float | None = None # confidence floor for auto-record; None = record all
```

Resolution order: `PLUMB_MODE` env var (for agents running unattended without
touching committed config) → `config.mode` → `"review"`.

- `plumb init` asks one more question after spec and test paths:

  ```
  How should Plumb handle decisions?
    review  — stop each commit until you approve/ignore/reject (default)
    record  — record decisions after each commit; review later with plumb log/search
  ```
- `plumb mode` prints the effective mode and where it came from;
  `plumb mode record|review` sets it and (re)installs the hooks so the
  pre-commit hook matches the mode.

### Decision status and provenance

- New status **`recorded`**. It syncs like `approved` (the sync filter becomes
  `status in ("approved", "edited", "recorded")`), counts as "existing" for
  dedup (so the same choice is not re-proposed), and displays distinctly in
  `plumb status`, `plumb log`, `plumb search`.
- New field **`approved_by: Optional[str]`** — `"user"` (a human said yes) or
  `"auto"` (record mode). Survives later status changes so provenance is never
  lost when a recorded decision is upgraded.
- Below-threshold decisions in record mode are written as ordinary `pending`
  and never block; they show up in `plumb status` for batch review.

| Posture | `mode` | `record_threshold` | Effect |
|---|---|---|---|
| Review (today) | `review` | — | Every decision blocks the commit |
| Triage | `record` | e.g. `0.7` | ≥ threshold auto-recorded; the rest `pending`, non-blocking |
| Full autopilot | `record` | `None` | Everything recorded |

### Extraction in record mode

The extraction pipeline (`_run_hook_inner` steps 4–10: broken-ref check,
diff analysis, transcript read, per-session chunking, `DecisionExtractor`,
dedup, question synthesis) is factored into one function used by both modes:

```python
extract_decisions(repo_root, config, diff: str, branch: str,
                  since_commit, since_datetime) -> list[Decision]
```

- **Review mode** calls it from pre-commit with the *staged* diff, as today.
- **Record mode** calls it from post-commit with the *commit's* diff
  (`git diff <sha>~1..<sha>`, or `--root` for the first commit) and the
  transcript window since the previous commit. Each decision is written with
  `status="recorded"`, `approved_by="auto"`, `commit_sha=<sha>` when
  `confidence >= record_threshold` (or always when the threshold is `None`);
  otherwise `status="pending"`.
- **Amends** in record mode: post-commit sees `HEAD~1 == config.last_commit`
  and deletes decisions whose `commit_sha` is the replaced SHA before
  re-extracting — the same rule review mode applies pre-commit, but now
  against a commit that actually exists.

### Not slowing the commit

`run_post_commit` in record mode does not run the LLM inline. It spawns a
detached process — `plumb record-extract <sha>` — with `start_new_session`,
stdout/stderr to `.plumb/record.log`, and returns. The worker takes an
advisory lock on `.plumb/record.lock` so two commits in quick succession
extract sequentially and append safely. `plumb record-extract <sha> --wait`
runs inline (tests, CI, and users who want to watch). `plumb status` shows
"recording in progress for <sha>" while the lock is held.

### Sync timing

Unchanged from the earlier draft: **lazy sync with a visible debt counter**.
Recorded decisions accumulate unsynced; `plumb status` prints
"N recorded, unsynced"; `plumb sync` flushes on demand. Spec rewrites are
themselves LLM output and stay a deliberate, reviewable event.

### Reviewing the record

- `plumb log` (shipped) — grouped by commit then agent, `--since`, `--verify`.
- `plumb review --recorded` — walk recorded decisions with approve / ignore /
  reject / edit. Approve sets `approved_by="user"`; reject marks `rejected`
  with a reason and **does not** run `modify` (the code is already committed;
  rewriting it is a separate, explicit act); edit amends the text and marks
  it for re-sync.

### `plumb search`

```
plumb search [QUERY...] [--sort relevance|date|confidence]
             [--status recorded|approved|pending|...]* [--agent NAME]*
             [--branch NAME] [--file PATH] [--since DATE|REF] [--made-by user|agent]
             [--limit N] [--json]
```

- **Enumeration** is one DuckDB query over `.plumb/decisions/*.jsonl` with
  latest-line-wins dedup (the query `read_all_decisions` uses), then SQL
  `WHERE` for every filter. `--file` matches `file_refs[*].file`; `--since`
  accepts an ISO date or a git ref (resolved to the commit's datetime).
- **Relevance** is BM25 over `question + decision + user_note`, computed in
  process over the filtered rows (hundreds to low thousands; sub-millisecond).
  DuckDB's `fts` extension would do this in SQL but is a network download the
  Python wheel does not bundle, so v1 does not depend on it; the ranking is
  isolated behind `rank(rows, query)` so swapping it in later is local.
- **Default sort** is relevance when a query is given, date (newest first)
  otherwise. `--sort confidence` orders by `confidence` desc, nulls last.
- Output rows: id, status, agent, made_by, confidence, date, commit (short),
  session/turn range when present, the decision text, and matched file refs.
  `--json` emits the full `Decision` records for scripting.
- No query and no filters = the whole log, newest first — the flat counterpart
  to `plumb log`'s grouped view.

### Skill / `CLAUDE.md` / `AGENTS.md`

`_update_claude_md()` writes a mode-appropriate block:

- **review:** unchanged — present each pending decision via `AskUserQuestion`;
  never resolve decisions on the user's behalf.
- **record:** drop the approval choreography. Before ending a session, run
  `plumb log --since <base>` and mention notable recorded decisions; use
  `plumb search` to check prior decisions before proposing a contradicting
  one. Pending (below-threshold) decisions are still the human's to resolve.

### Non-goals

- Auto-reject / auto-modify in record mode.
- A search index, vector search, or any store other than the JSONL shards.
- Reworking review mode's pre-commit latency (record mode is the answer for
  users who care).

## Implementation order

1. Config (`mode`, `record_threshold`, `PLUMB_MODE`, `effective_mode`),
   `plumb mode`, `plumb init` prompt.
2. `recorded` status + `approved_by`; dedup, sync, and `plumb status` aware of
   it (sync-debt counter).
3. Factor `extract_decisions()` out of `_run_hook_inner`; review mode uses it
   unchanged (tests must stay green).
4. Record path: `record_extract(repo_root, sha)` with threshold split and
   direct `commit_sha`; `run_hook` short-circuits in record mode;
   `run_post_commit` spawns the detached worker; `plumb record-extract`.
5. `plumb search` (`plumb/search.py`: DuckDB enumeration + BM25 + CLI).
6. `plumb review --recorded`; mode-aware skill/`CLAUDE.md`/`AGENTS.md`
   block; spec + README.

Steps 1–4 make record mode work; 5 makes it useful; 6 finishes the surface.
