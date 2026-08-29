# Multi-Agent Traces: Find, Normalize, Extract

**Date:** 2026-08-29
**Status:** Proposed
**Companion:** [Record mode](2026-08-29-record-mode-design.md) owns stage 3 of the
pipeline described here.

## Problem

Plumb reads one trace format: Claude Code's project-scoped JSONL under
`~/.claude/projects/`. Everything about that reader is Claude-specific, and
several of its shortcuts limit extraction quality even for Claude:

- **Discovery** encodes the repo path by replacing `/` with `-`
  (`plumb/claude_session.py:24`). Claude Code also maps `.` to `-`, so any repo
  path containing a dot — including every `.claude/worktrees/*` checkout —
  silently fails discovery and falls through to diff-only extraction.
- **Tool calls** collapse to the literal string `[tool: Edit]`
  (`claude_session.py:82-88`). No file path, no arguments. Tool results are
  dropped entirely (`:67`). Only `content[0]` of each assistant entry is read
  (`:74`).
- **Subagents** are excluded outright: `isSidechain` or `isMeta` → skip (`:57`).
- **Sessions** are merged into one timestamp-sorted stream (`:180-191`). Two
  concurrent sessions interleave into the same chunks.
- **Provenance** is a `chunk_index` that has no meaning across runs.
- `Decision.file_refs` is never populated at extraction time, though
  `sync.py:357` depends on it.

Developers increasingly run more than one agent against a repo — Claude Code
and Codex on the same branch, Pi for a subtask, a Copilot session in CI. Plumb
should capture decisions from all of them, and it should not need to know which
agent made a decision after stage 1.

## Decision

Restructure decision capture as a three-stage pipeline where the first two
stages are agent-agnostic after a thin per-agent adapter:

```
1. find + normalize    TraceSource.discover → parse → Turn / ToolCall
2. extract + enrich    chunk per session → extractor → dedup → file_refs, provenance
3. resolve + store     gate: pending → human review → log
                       record: threshold → recorded | pending → log
```

Stages 1 and 2 have no knowledge of mode. Stage 3 is specified by the record
mode design. Triage (confidence threshold) is not a separate stage; it only
decides the *initial status* written in stage 3.

Plumb does **not** mirror traces. The transcript on disk is the evidence store.
The append-only decision log holds a pointer back into it (agent, session,
file, turn range, digest) so a decision can be located and contested later.
No SQLite, no sync daemon, no watermarks.

## Stage 1: Find and normalize

### The `TraceSource` adapter

```python
class TraceSource(Protocol):
    name: str                                    # "claude" | "codex" | "pi" | ...

    def discover(self, repo_root: Path, since: datetime) -> list[SessionRef]:
        """Sessions that touched this repo and were active after `since`."""

    def parse(self, ref: SessionRef, since: datetime) -> list[Turn]:
        """Normalized turns for one session, in order, after `since`."""
```

```python
@dataclass
class SessionRef:
    agent: str
    session_id: str
    path: Path
    cwd: str
    branch: Optional[str]
    parent_session_id: Optional[str]     # set for subagents / sidechains

@dataclass
class ToolCall:
    name: str                            # raw, e.g. "apply_patch"
    category: str                        # Read|Edit|Write|Bash|Grep|Glob|Task|Tool|Other
    file_path: Optional[str]
    input_summary: str                   # first ~120 chars of the salient arg
    result_summary: Optional[str]        # truncated result, Bash/Edit only

@dataclass
class Turn:
    agent: str
    session_id: str
    ordinal: int                         # position within the session
    role: str                            # "user" | "assistant"
    timestamp: Optional[str]
    content: str
    tool_calls: list[ToolCall]
```

No registry. Sources are a list in `plumb/traces/__init__.py`; add a third
entry when there is a third adapter.

### Discovery by `cwd`, not by path encoding

Every agent lays out its session directory differently, but the agents we
cover all record the working directory *inside* the transcript. Discovery is
therefore uniform:

1. Glob the agent's candidate files whose mtime is after the cutoff (cheap).
2. Read the file head for `cwd`.
3. Keep the session if `cwd` resolves to the same repo as `repo_root`.

"Same repo" is compared on `git rev-parse --git-common-dir`, not
`--show-toplevel`, so a linked worktree under `.claude/worktrees/x` attributes
to the main repo. Its decisions already shard by branch
(`.plumb/decisions/<branch>.jsonl`), so worktree sessions land in the right
file without special handling.

This replaces `encode_project_path` and fixes the dotted-path bug as a side
effect. For Claude the candidate glob is `~/.claude/projects/*/*.jsonl` — every
project directory, filtered by in-file `cwd` — which is a small cost at commit
cadence and removes the encoding dependency entirely.

### Agents covered

| Agent | Files | `cwd` / branch | Tool call shape | Subagents |
|---|---|---|---|---|
| **Claude Code** | `~/.claude/projects/<enc>/<session>.jsonl`; subagents in `<enc>/subagents/agent-*.jsonl` and `isSidechain` entries | `cwd`, `gitBranch` on user entries | `tool_use` blocks in `message.content[]`; results as `tool_result` user entries | linked to the spawning `Task`/`Agent` call by `toolUseResult.agentId` |
| **Codex** | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (+ `archived_sessions/`) | `turn_context.payload.cwd`, `git.branch` | `function_call` / `custom_tool_call` with `name`; `function_call_output` | `spawn_agent` → child rollout |
| **Pi** | `~/.pi/agent/sessions/<project>/<session>.jsonl`; subagents at `<project>/<session>/<agent>.jsonl` | `cwd` on the `{"type":"session"}` header line | `toolCall` blocks with `name`; results keyed by `message.toolCallId` | child file nested under parent's directory; header may carry `branchedFrom` |
| **Copilot CLI** | `~/.copilot/session-state/<uuid>/events.jsonl` (or bare `<uuid>.jsonl`) | `context.cwd`, branch | tool request events with `name` | — |

All four share the shape "JSONL, one record per line, `cwd` in-file", so one
adapter interface gets validated by four real implementations before anything
exotic.

**Pi wrinkle.** Pi's log is tree-structured: entries carry `id` and
`parentId`, and a rewind leaves the abandoned branch in the file. Only the
active ancestry is the conversation. The adapter walks from the last entry up
via `parentId` and emits that path in order; alternate branches are ignored.
This is worth doing right because a rewind is itself a strong decision signal
(the user rejected what came before), but v1 just reads the active path.

**Deferred.** Gemini CLI (`~/.gemini/tmp/<hash>/chats/*.json`, attribution via
Gemini's project-hash map), Cursor (`~/.cursor/projects/<enc>/agent-transcripts/*.txt`,
plain text, encoded-dir attribution only), and OpenCode (three-way join across
`storage/{session,message,part}/`). Each needs a bespoke attribution trick; add
on request.

### Tool taxonomy

Nine categories, ported from agentsview's `taxonomy.go` as a Python dict:

| Category | Claude | Codex | Pi | Copilot |
|---|---|---|---|---|
| Read | `Read` | `list_files` | `read_file`, `find` | `view` |
| Edit | `Edit` | `apply_patch` | `str_replace` | `edit_file` |
| Write | `Write`, `NotebookEdit` | — | `create_file` | — |
| Bash | `Bash` | `shell_command`, `exec_command`, `shell` | `run_command` | `shell` |
| Grep | `Grep` | — | `grep` | `grep` |
| Glob | `Glob` | — | — | `glob` |
| Task | `Task`, `Agent` | `spawn_agent` | — | — |
| Tool | `Skill`, MCP tools | — | — | `report_intent` |
| Other | anything else | | | |

`file_path` is extracted for Read/Edit/Write from the agent-specific argument
(`file_path`, `path`, the patch header for `apply_patch`). Unknown names fall
through to `Other` and are still emitted — the extractor sees them, it just
can't categorize them.

### Rendering turns for the extractor

Chunk text keeps the existing `[role]: content` form but tool calls render with
substance:

```
[assistant]: I'll switch the cache to an in-memory dict.
  [Edit plumb/cache.py]
  [Bash: pytest tests/test_cache.py -x]  → 12 passed
```

Each chunk is prefixed with `[agent=codex session=019a… turns 12–19]` so the
extractor can attribute `made_by` and so provenance round-trips.

## Stage 2: Extract and enrich

### Per-session segmentation

Turns are grouped by `(agent, session_id)` before chunking. The existing
user-turn-plus-assistant-run chunking (`conversation.py:163`) runs *within* a
session; sessions are never interleaved. Subagent sessions are chunked
separately with `parent_session_id` set. Dedup (`decision_log.py:390`) already
handles the same decision surfacing from a parent and its subagent.

The wall-clock cutoff (`last_extracted_at` / last commit time) stays as the
coarse filter. Per-session watermarks would be a small amount of state in
`.plumb/` but are not needed until interleaving or amend problems appear in
practice.

### Enrichment

Deterministic, no model calls:

- `file_refs` = `{file_path from Edit/Write calls in the chunk} ∩ {paths in the staged diff}`.
  Populated at extraction time; the LLM no longer has to name files.
- `made_by` — the extractor already infers user vs. agent; with multiple
  agents the `agent` field says which one.

### Provenance on the `Decision`

New fields:

```python
agent: Optional[str]                     # "claude" | "codex" | "pi" | "copilot"
session_id: Optional[str]
parent_session_id: Optional[str]
source_path: Optional[str]               # transcript file
turn_range: Optional[tuple[int, int]]    # ordinals within that session
evidence_digest: Optional[str]           # sha256 of the chunk text sent to the extractor
```

`chunk_index` is removed. `plumb log` and `plumb review --recorded` show
`codex · session 019a… · turns 12–19`, and a human can open the file.

Verification is on demand: `plumb log --verify` re-reads each `turn_range`,
recomputes the digest, and sets `ref_status="stale"` on mismatch (compaction,
rewritten transcript, deleted file). Nothing runs in the background. The
transcript can rot and the log stays honest about it.

### Line attribution (follow-up)

`FileRef` already has a `lines` slot (`decision_log.py:15`) and the spec shows
`{"file": "src/auth.py", "lines": [42, 58]}`, but nothing populates or follows
it. `ref_status` checks only that `commit_sha` is reachable
(`git_hook.py:76`); it says nothing about lines. Once `file_refs` are
deterministic, lines come in two layers:

| Layer | What | When | Cost |
|---|---|---|---|
| **Stored** | `FileRef.lines` = hunk ranges from the staged diff, for each file in `file_refs` | stage 2 enrichment | free — the hook already has the diff |
| **Resolved** | current lines = `git blame` lines attributed to `commit_sha` | on demand: `plumb show <id>`, `plumb coverage`, test-gen context | one `git blame -M -C --line-porcelain` per file |

The stored ranges are a historical fact — "at SHA X this decision touched
lines 42–58" — and never go stale. The resolved ranges are what a consumer
actually wants: blame attributes every current line to the commit that last
touched it, so filtering on the decision's `commit_sha` yields the live
footprint with no stored line numbers to drift. `-M -C` follows moves and
copies. `sync.py` can then hand `TestGenerator` the relevant region instead
of the whole file in `code_context[:16000]`.

Two known limits:

1. **Blame sees only the last toucher.** A later reformat re-attributes the
   region and the decision's blame footprint shrinks to zero.
   `git log -L <start>,<end>:<file> <sha>..HEAD` tracks a *range* forward
   through history and survives this, seeded by the stored hunk — which is why
   storing hunks at commit time matters even though blame is the primary
   resolver.
2. **Rebase and squash change the SHA.** `ref_status="broken"` already flags
   this, and the spec requires manual re-resolution (`plumb_spec.md:588`).
   `git patch-id` is stable across rebases for the same diff content, so a
   broken ref could re-map to its new SHA automatically. Adjacent improvement,
   not part of this design.

Ordering: store hunks as part of step 2 below (trivial once `file_refs` exist);
add blame resolution when the first consumer needs lines.

## Stage 3: Resolve and store

Owned by the record mode design. The only contract stages 1–2 impose: every
decision is written to the log exactly once at creation (status `pending` or
`recorded`), and review is a status transition appended later. Gate mode
blocks on the transition; record mode does not.

Two consequences worth stating:

- **Record mode is the multi-agent posture.** The gate-mode choreography
  (`AskUserQuestion` → `plumb approve`) is Claude-Code-specific. Other agents in
  gate mode see a non-zero exit and hook stdout, then the human runs
  `plumb review` in a terminal. Record mode needs no agent-side UI at all.
- **Instructions go to `AGENTS.md` too.** `_update_claude_md()` writes the same
  block to `AGENTS.md`, which Codex, Pi, Copilot, Gemini, and Cursor read.

## Non-goals

- Mirroring traces into a database. Plumb's artifact is the decision log.
- A provider registry, plugin system, or config-driven session roots. Four
  adapters in a list is fine until it isn't.
- Health scoring or tool-failure heuristics. Retry/churn counts are cheap to
  compute from `Turn.tool_calls` and would sharpen the record threshold, but
  they are a refinement after this lands.
- Gemini, Cursor, OpenCode, or any agent whose trace does not record `cwd`.

## Implementation order

1. `plumb/traces/`: `TraceSource`, `SessionRef`, `Turn`, `ToolCall`, taxonomy
   dict. Port Claude to it — `cwd`-based discovery, all content blocks, tool
   arguments and results, subagent files with `parent_session_id`. Delete
   `encode_project_path` and the legacy `locate_conversation_log` path in
   `conversation.py:34`.
2. Provenance fields on `Decision`; per-session chunking; `file_refs` from edit
   paths with `lines` filled from the staged-diff hunks; chunk headers with
   agent/session/turns.
3. Codex adapter.
4. Pi adapter (active-ancestry walk).
5. Copilot adapter.
6. `AGENTS.md` write; `plumb log` grouped by commit then agent;
   `plumb log --verify`.

Steps 1–3 are the first PR and land before record mode's `plumb log`, which is
much more useful when every entry already says which agent, which session,
which turns.
