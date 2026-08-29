# Record Mode and `plumb search` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `record` mode (decisions appended after every commit, no gate, commit not slowed) chosen at `plumb init`, and a `plumb search` command that enumerates the decision log with DuckDB and ranks by relevance, date, or confidence.

**Architecture:** Mode lives in `PlumbConfig` (+ `PLUMB_MODE` override). The existing pre-commit extraction pipeline is factored into `extract_decisions()`; review mode keeps calling it pre-commit with the staged diff, record mode calls it post-commit with the landed commit's diff via a detached `plumb record-extract <sha>` worker that writes `status="recorded"` rows with `commit_sha` set. Search is one DuckDB query over `.plumb/decisions/*.jsonl` for filters/sorts plus an in-process BM25 for relevance. Design: `docs/plans/2026-08-29-record-mode-design.md`.

**Tech Stack:** Python 3.10+, pydantic v2, click/rich, GitPython, DuckDB (already a dependency; no extensions), pytest. Run everything with `uv run`.

**Conventions for the executor:**
- @superpowers:test-driven-development — failing test first, every task.
- Baseline: `uv run pytest tests/ -q` → **670 passed, 0 failed** (bare run; `tests/test_generated.py` now collects). Do not regress.
- Never run `plumb hook`, `plumb diff`, `plumb sync`, or `plumb record-extract` without `--wait` against this repo — they call an LLM. In tests, patch `plumb.git_hook._analyze_diff`, `plumb.programs.validate_api_access`, `plumb.programs.run_with_retries`, `plumb.git_hook._synthesize_questions`, and `plumb.git_hook.read_conversation_with_refs` the way `tests/test_git_hook.py` already does.
- This repo's own pre-commit hook is disabled (`.git/hooks/pre-commit.disabled`). Commit normally; don't re-enable it.
- Commit after every task with the message given, plus this repo's `Co-Authored-By` / `Claude-Session` trailers if you are Claude.

---

## Task 1: Mode config, `effective_mode`, `PLUMB_MODE`

**Files:**
- Modify: `plumb/config.py` (`PlumbConfig`)
- Test: `tests/test_config.py` (append)

**Step 1: Write the failing test**

```python
# append to tests/test_config.py
def test_mode_defaults_and_validation():
    from plumb.config import PlumbConfig, effective_mode
    cfg = PlumbConfig()
    assert cfg.mode == "review" and cfg.record_threshold is None
    assert effective_mode(cfg) == ("review", "default")
    assert effective_mode(PlumbConfig(mode="record")) == ("record", "config")
    import pytest
    with pytest.raises(ValueError):
        PlumbConfig(mode="gate")
    with pytest.raises(ValueError):
        PlumbConfig(record_threshold=1.5)


def test_plumb_mode_env_overrides_config(monkeypatch):
    from plumb.config import PlumbConfig, effective_mode
    monkeypatch.setenv("PLUMB_MODE", "record")
    assert effective_mode(PlumbConfig(mode="review")) == ("record", "env")
    monkeypatch.setenv("PLUMB_MODE", "bogus")
    assert effective_mode(PlumbConfig(mode="review")) == ("review", "config")  # invalid env ignored
    monkeypatch.setenv("PLUMB_MODE", "")
    assert effective_mode(PlumbConfig(mode="review")) == ("review", "config")


def test_old_config_without_mode_loads(tmp_repo):
    from plumb.config import PlumbConfig, save_config, load_config
    save_config(tmp_repo, PlumbConfig(spec_paths=["s.md"]))
    import json
    p = tmp_repo / ".plumb" / "config.json"
    data = json.loads(p.read_text()); data.pop("mode"); data.pop("record_threshold")
    p.write_text(json.dumps(data))
    assert load_config(tmp_repo).mode == "review"
```

**Step 2:** `uv run pytest tests/test_config.py -k mode -v` → FAIL.

**Step 3: Implement** in `plumb/config.py`:

```python
from typing import Literal
from pydantic import field_validator

MODES = ("review", "record")

class PlumbConfig(BaseModel):
    ...existing fields...
    mode: str = "review"                       # "review" | "record"
    record_threshold: Optional[float] = None   # confidence floor for auto-record

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {v!r}")
        return v

    @field_validator("record_threshold")
    @classmethod
    def _valid_threshold(cls, v):
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("record_threshold must be between 0 and 1")
        return v


def effective_mode(cfg: "PlumbConfig | None") -> tuple[str, str]:
    """(mode, source) where source is 'env' | 'config' | 'default'."""
    env = os.environ.get("PLUMB_MODE", "").strip().lower()
    if env in MODES:
        return env, "env"
    if cfg is not None and cfg.mode in MODES and cfg.mode != "review":
        return cfg.mode, "config"
    if cfg is not None and cfg.mode == "review":
        return "review", "config" if _mode_was_explicit(cfg) else "default"
    return "review", "default"
```

Keep `effective_mode` simple: return `("review", "default")` only when `cfg is None`; otherwise `(cfg.mode, "config")` unless the env overrides. Drop `_mode_was_explicit` — adjust the first test's expectation to `("review", "config")` for a default-constructed config if you find distinguishing explicit-vs-default is not worth it (it isn't). Also note `load_config` returns `None` on a `ValueError` from a bad `mode` in an old file — that is acceptable; `plumb status` already handles `None`.

**Step 4:** tests PASS. **Step 5: Commit** — `feat(config): mode and record_threshold with PLUMB_MODE override`

---

## Task 2: `plumb mode` command and the `plumb init` prompt

**Files:**
- Modify: `plumb/cli.py` (`init`, new `mode` command; hook installation helper)
- Test: `tests/test_cli.py` (append)

**Step 1: Write the failing tests**

```python
def test_mode_command_prints_and_sets(initialized_repo, monkeypatch):
    from click.testing import CliRunner
    from plumb.cli import cli
    from plumb.config import load_config
    monkeypatch.chdir(initialized_repo)
    r = CliRunner().invoke(cli, ["mode"])
    assert r.exit_code == 0 and "review" in r.output
    r = CliRunner().invoke(cli, ["mode", "record"])
    assert r.exit_code == 0
    assert load_config(initialized_repo).mode == "record"
    assert (initialized_repo / ".git" / "hooks" / "pre-commit").read_text().strip().endswith("plumb hook\nexit $?")
    r = CliRunner().invoke(cli, ["mode", "gate"])
    assert r.exit_code != 0


def test_init_prompts_for_mode(tmp_repo, monkeypatch):
    # Follow the existing init tests' pattern for driving _prompt_with_suggestions / click.prompt;
    # answer spec="spec.md", tests="tests/", mode="record".
    ...
    assert load_config(tmp_repo).mode == "record"
```

Read the existing `init` tests first and copy their prompt-patching approach exactly; if `init` uses `_prompt_with_suggestions` for spec/tests, add a third `click.prompt(..., type=click.Choice(["review", "record"]), default="review")` for the mode and patch it the same way.

**Step 3: Implement**
- In `init`, after the test-path prompt: `mode = click.prompt("How should Plumb handle decisions? (review: stop each commit for approval; record: record after each commit, review later)", type=click.Choice(["review", "record"]), default="review")`; set `cfg.mode = mode`.
- Factor the hook-installation lines of `init` (the `pre-commit` and `post-commit` writes, ~`cli.py:247-256`) into `_install_hooks(repo_root)` and call it from both `init` and `mode`.
- New command:
  ```python
  @cli.command()
  @click.argument("new_mode", required=False, type=click.Choice(["review", "record"]))
  def mode(new_mode):
      """Show or set how Plumb handles decisions (review | record)."""
      ...load config (error if missing)...
      if new_mode is None:
          m, src = effective_mode(cfg); console.print(f"{m}  (from {src})"); return
      cfg.mode = new_mode; save_config(repo_root, cfg); _install_hooks(repo_root)
      console.print(f"Mode set to {new_mode}.")
  ```

**Step 4/5:** tests PASS; commit `feat(cli): plumb mode command; plumb init asks review vs record`

---

## Task 3: `recorded` status and `approved_by`

**Files:**
- Modify: `plumb/decision_log.py` (`Decision.approved_by`; `deduplicate_decisions` existing-filter), `plumb/sync.py` (`to_sync` filter, both in `sync_decisions` and the CLI pre-check in `cli.py`), `plumb/cli.py` (`status`: recorded/unsynced counter)
- Test: `tests/test_decision_log.py`, `tests/test_sync.py`, `tests/test_cli.py` (append)

**Tests:**
```python
def test_approved_by_roundtrip(initialized_repo):
    from plumb.decision_log import Decision, append_decisions, read_all_decisions
    append_decisions(initialized_repo, [Decision(id="dec-r1", status="recorded", decision="x", approved_by="auto", branch="main")], branch="main")
    d = {x.id: x for x in read_all_decisions(initialized_repo)}["dec-r1"]
    assert (d.status, d.approved_by) == ("recorded", "auto")

def test_dedup_treats_recorded_as_existing():
    from plumb.decision_log import Decision, deduplicate_decisions
    existing = [Decision(id="dec-old", status="recorded", question="Cache?", decision="In-memory dict cache.")]
    cand = [Decision(id="dec-new", status="pending", question="Cache?", decision="In-memory dict cache.")]
    assert deduplicate_decisions(cand, existing_decisions=existing, use_llm=False) == []

def test_sync_includes_recorded(initialized_repo):
    # mirror an existing test in tests/test_sync.py that patches the LLM programs and asserts a decision is synced;
    # seed status="recorded" instead of "approved" and assert synced_at is set afterwards.
    ...

def test_status_shows_recorded_unsynced(initialized_repo, monkeypatch):
    # seed two recorded (unsynced) decisions; invoke `status`; assert "2 recorded, unsynced" in output
    ...
```

**Implement:** `approved_by: Optional[str] = None` on `Decision` (comment: `"user" | "auto"`); grep `("approved", "edited"` and `("approved", "edited", "synced")` across `plumb/` and add `"recorded"` to each; `plumb approve` sets `approved_by="user"` (find where `status="approved"` is written in `cli.py` and `review`); `plumb status` prints `Recorded, unsynced: N` when N > 0.

Commit: `feat(decisions): recorded status and approved_by provenance; sync/dedup/status aware`

---

## Task 4: Factor `extract_decisions()` out of the hook

**Files:**
- Modify: `plumb/git_hook.py`
- Test: `tests/test_git_hook.py` (append one test; all existing must pass unchanged)

**Test:**
```python
def test_extract_decisions_is_mode_agnostic(initialized_repo):
    """extract_decisions() returns the merged, deduped, question-synthesized list without writing anything."""
    from plumb.git_hook import extract_decisions
    from plumb.config import load_config
    from plumb.decision_log import Decision, read_all_decisions
    mock = [Decision(id="dec-x", status="pending", question="Q?", decision="A.", made_by="user", confidence=0.9, branch="main")]
    with patch("plumb.programs.validate_api_access"), \
         patch("plumb.git_hook._analyze_diff", return_value="summary"), \
         patch("plumb.git_hook._extract_decisions_from_conversation", return_value=mock), \
         patch("plumb.git_hook._synthesize_questions", side_effect=lambda ds: ds):
        out = extract_decisions(initialized_repo, load_config(initialized_repo), diff="+x", branch="main")
    assert [d.id for d in out] == ["dec-x"]
    assert read_all_decisions(initialized_repo) == []   # nothing written
```

**Implement:** move steps 4–9 of `_run_hook_inner` (broken-ref check on existing decisions, `validate_api_access`, `_analyze_diff`, conversation/diff extraction, `deduplicate_decisions(..., use_llm=True)`, `_synthesize_questions`) into

```python
def extract_decisions(repo_root, config, diff: str, branch: str,
                      since_commit: str | None = None, since_datetime: str | None = None,
                      timings: list | None = None) -> list[Decision]:
```

`_extract_decisions_from_conversation` currently reads `config.last_commit`/`config.last_extracted_at` itself; give it explicit `since_commit`/`since_datetime` parameters (defaulting from `config`) so record mode can pass the previous commit. `_run_hook_inner` becomes: load config → staged diff → amend detection → `decisions = extract_decisions(...)` → write pending → pending check. Keep the `_timed` instrumentation by passing `timings` through (or drop the per-step timing inside `extract_decisions` and time it as one block — simpler; do that).

Commit: `refactor(hook): extract_decisions() shared by review and record modes`

---

## Task 5: Record path — `record_extract`, short-circuit pre-commit, detached post-commit worker

**Files:**
- Create: `plumb/record.py`
- Modify: `plumb/git_hook.py` (`run_hook` short-circuit; `run_post_commit` spawn), `plumb/cli.py` (`record-extract` command)
- Test: `tests/test_record.py`

**Step 1: Write the failing tests**

```python
# tests/test_record.py
from datetime import datetime, timezone
from unittest.mock import patch
from git import Repo
from plumb.config import load_config, save_config
from plumb.decision_log import Decision, read_all_decisions


def _mock(conf, id_="dec-m"):
    return Decision(id=id_, status="pending", question="Q?", decision="A.", made_by="agent",
                    confidence=conf, branch="main", created_at=datetime.now(timezone.utc).isoformat())


def _commit(repo_root, name="a.py", text="x = 1\n", msg="c"):
    (repo_root / name).write_text(text)
    r = Repo(repo_root); r.index.add([name]); return r.index.commit(msg).hexsha


def test_record_extract_writes_recorded_with_commit_sha(initialized_repo):
    from plumb.record import record_extract
    cfg = load_config(initialized_repo); cfg.mode = "record"; save_config(initialized_repo, cfg)
    sha = _commit(initialized_repo)
    with patch("plumb.git_hook.extract_decisions", return_value=[_mock(0.9)]):
        written = record_extract(initialized_repo, sha)
    assert len(written) == 1
    d = read_all_decisions(initialized_repo)[0]
    assert (d.status, d.approved_by, d.commit_sha) == ("recorded", "auto", sha)


def test_record_extract_threshold_splits(initialized_repo):
    from plumb.record import record_extract
    cfg = load_config(initialized_repo); cfg.mode = "record"; cfg.record_threshold = 0.8; save_config(initialized_repo, cfg)
    sha = _commit(initialized_repo)
    with patch("plumb.git_hook.extract_decisions", return_value=[_mock(0.9, "hi"), _mock(0.5, "lo")]):
        record_extract(initialized_repo, sha)
    by = {d.id: d for d in read_all_decisions(initialized_repo)}
    assert by["hi"].status == "recorded" and by["hi"].commit_sha == sha
    assert by["lo"].status == "pending" and by["lo"].commit_sha == sha and by["lo"].approved_by is None


def test_record_extract_uses_commit_diff_not_staged(initialized_repo):
    from plumb.record import record_extract
    sha = _commit(initialized_repo, text="committed = 1\n")
    (initialized_repo / "b.py").write_text("staged_only = 1\n"); Repo(initialized_repo).index.add(["b.py"])
    seen = {}
    def fake(repo_root, config, diff, branch, **kw):
        seen["diff"] = diff; seen.update(kw); return []
    with patch("plumb.git_hook.extract_decisions", side_effect=fake):
        record_extract(initialized_repo, sha)
    assert "committed = 1" in seen["diff"] and "staged_only" not in seen["diff"]


def test_record_extract_amend_deletes_replaced_commits_decisions(initialized_repo):
    from plumb.record import record_extract
    from plumb.decision_log import append_decisions
    sha1 = _commit(initialized_repo)
    cfg = load_config(initialized_repo); cfg.last_commit = sha1; save_config(initialized_repo, cfg)
    append_decisions(initialized_repo, [Decision(id="dec-old", status="recorded", decision="old", commit_sha=sha1, branch="main")], branch="main")
    r = Repo(initialized_repo)
    (initialized_repo / "a.py").write_text("x = 2\n"); r.index.add(["a.py"])
    sha2 = r.index.commit("c", parent_commits=[r.head.commit.parents[0]] if r.head.commit.parents else []).hexsha  # simulate amend: same parent as sha1
    with patch("plumb.git_hook.extract_decisions", return_value=[]):
        record_extract(initialized_repo, sha2)
    assert all(d.id != "dec-old" for d in read_all_decisions(initialized_repo))


def test_run_hook_is_noop_in_record_mode(initialized_repo):
    from plumb.git_hook import run_hook
    cfg = load_config(initialized_repo); cfg.mode = "record"; save_config(initialized_repo, cfg)
    (initialized_repo / "z.py").write_text("z = 1\n"); Repo(initialized_repo).index.add(["z.py"])
    with patch("plumb.git_hook.extract_decisions") as ex:
        assert run_hook(initialized_repo) == 0
    ex.assert_not_called()


def test_post_commit_spawns_worker_in_record_mode(initialized_repo, monkeypatch):
    from plumb.git_hook import run_post_commit
    cfg = load_config(initialized_repo); cfg.mode = "record"; save_config(initialized_repo, cfg)
    sha = _commit(initialized_repo)
    spawned = {}
    monkeypatch.setattr("plumb.record.spawn_worker", lambda repo_root, s: spawned.update(sha=s))
    run_post_commit(initialized_repo)
    assert spawned["sha"] == sha
    assert load_config(initialized_repo).last_commit == sha


def test_worker_lock_serializes(initialized_repo):
    from plumb.record import record_lock
    with record_lock(initialized_repo) as got:
        assert got is True
        with record_lock(initialized_repo, wait=False) as got2:
            assert got2 is False
```

**Step 3: Implement `plumb/record.py`**

```python
"""Record mode: extract decisions for a landed commit, outside the commit path."""
from __future__ import annotations

import contextlib, fcntl, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from git import Repo

from plumb.config import load_config, effective_mode
from plumb.decision_log import Decision, append_decisions, delete_decisions_by_commit, find_decision_branch
from plumb.traces.repo import commit_datetime


def commit_diff(repo: Repo, sha: str) -> str:
    c = repo.commit(sha)
    return repo.git.diff(f"{c.parents[0].hexsha}..{sha}") if c.parents else repo.git.show(sha, "--format=", "--patch")


def record_extract(repo_root, sha: str) -> list[Decision]:
    """Extract decisions for commit `sha` and append them as recorded/pending."""
    from plumb import git_hook  # late import: git_hook imports decision_log etc.
    repo_root = Path(repo_root); repo = Repo(repo_root)
    cfg = load_config(repo_root)
    if cfg is None:
        return []
    c = repo.commit(sha)
    branch = git_hook._get_branch_name(repo)
    prev = c.parents[0].hexsha if c.parents else None
    # amend: the replaced commit's decisions are gone with it
    if cfg.last_commit and prev and cfg.last_commit != prev and _same_parent(repo, cfg.last_commit, sha):
        delete_decisions_by_commit(repo_root, cfg.last_commit, branch=branch)
    diff = git_hook._filter_diff_paths(repo, cfg, commit_diff(repo, sha))   # reuse the .plumbignore/managed-path filter; add this helper if the hook filters by file list rather than diff text
    decisions = git_hook.extract_decisions(repo_root, cfg, diff=diff, branch=branch,
                                           since_commit=prev, since_datetime=None)
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for d in decisions:
        auto = cfg.record_threshold is None or (d.confidence is not None and d.confidence >= cfg.record_threshold)
        out.append(d.model_copy(update={
            "status": "recorded" if auto else "pending",
            "approved_by": "auto" if auto else None,
            "commit_sha": sha, "branch": branch, "created_at": d.created_at or now,
        }))
    if out:
        append_decisions(repo_root, out, branch=branch)
    return out


def _same_parent(repo: Repo, old_sha: str, new_sha: str) -> bool:
    try:
        o, n = repo.commit(old_sha), repo.commit(new_sha)
        return bool(o.parents) and bool(n.parents) and o.parents[0].hexsha == n.parents[0].hexsha
    except Exception:
        return False


@contextlib.contextmanager
def record_lock(repo_root, wait: bool = True):
    path = Path(repo_root) / ".plumb" / "record.lock"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False; return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def spawn_worker(repo_root, sha: str) -> None:
    """Launch `plumb record-extract <sha>` detached; the commit returns immediately."""
    log = open(Path(repo_root) / ".plumb" / "record.log", "ab")
    subprocess.Popen([sys.executable, "-m", "plumb.cli", "record-extract", sha],
                     cwd=str(repo_root), stdout=log, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
```

Check `plumb/cli.py` has a `if __name__ == "__main__": cli()` guard so `python -m plumb.cli` works; add one if not. `_filter_diff_paths`: look at `_get_staged_diff_filtered` — it filters by staged file list, then re-diffs; for a commit, compute the file list with `repo.git.diff("--name-only", f"{prev}..{sha}")`, apply the same managed/ignore filters, then `repo.git.diff(f"{prev}..{sha}", "--", *files)`. Factor the filter into `_filter_paths(repo, config, paths) -> list[str]` shared by both.

`git_hook.run_hook`: at the top of `_run_hook_inner` after loading config, `if effective_mode(config)[0] == "record": return 0`.

`git_hook.run_post_commit`: after updating `last_commit` (and existing `_stamp_commit_sha`), `if effective_mode(config)[0] == "record": from plumb.record import spawn_worker; spawn_worker(repo_root, new_sha)`. Note the existing stamping only affects decisions with `commit_sha is None` — record rows already carry it, so the two coexist.

CLI:
```python
@cli.command(name="record-extract")
@click.argument("sha")
@click.option("--wait", is_flag=True, help="Run inline and print results (default when invoked by the worker)")
def record_extract_cmd(sha, wait):
    from plumb.record import record_extract, record_lock
    repo_root = find_repo_root(); ...
    with record_lock(repo_root) as ok:
        written = record_extract(repo_root, sha)
    console.print(f"Recorded {sum(d.status == 'recorded' for d in written)} decision(s), {sum(d.status == 'pending' for d in written)} pending for {sha[:12]}.")
```
(The worker always runs inline once it holds the lock; `--wait` exists for humans/tests and is a no-op flag in v1 — keep it for the documented interface.) `plumb status`: if `.plumb/record.lock` is held (`record_lock(wait=False)` yields False), print "Recording in progress…".

Commit: `feat(record): post-commit extraction with recorded status, detached worker, and record-extract command`

---

## Task 6: `plumb search`

**Files:**
- Create: `plumb/search.py`
- Modify: `plumb/cli.py` (new `search` command)
- Test: `tests/test_search.py`

**Step 1: Write the failing tests**

```python
# tests/test_search.py
from datetime import datetime, timezone, timedelta
from plumb.decision_log import Decision, FileRef, append_decisions
from plumb.search import search_decisions, bm25_scores, tokenize


def _seed(repo):
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [
        Decision(id="d1", status="recorded", agent="claude", made_by="user", confidence=0.9, branch="main",
                 question="Cache strategy?", decision="Use an in-memory dict cache with TTL.",
                 file_refs=[FileRef(file="src/cache.py", lines=[1, 9])], commit_sha="aaa",
                 created_at=(t0 + timedelta(days=1)).isoformat()),
        Decision(id="d2", status="approved", agent="codex", made_by="agent", confidence=0.6, branch="main",
                 question="Auth tokens?", decision="Tokens expire after 30 minutes of inactivity.",
                 file_refs=[FileRef(file="src/auth.py", lines=[42, 58])], commit_sha="bbb",
                 created_at=(t0 + timedelta(days=2)).isoformat()),
        Decision(id="d3", status="pending", agent="pi", made_by="agent", confidence=None, branch="feat",
                 question="Cache eviction?", decision="Evict least-recently-used cache entries.",
                 created_at=(t0 + timedelta(days=3)).isoformat()),
        Decision(id="d4", status="ignored", decision="cache cache cache", created_at=t0.isoformat()),
    ]
    append_decisions(repo, rows[:2], branch="main"); append_decisions(repo, rows[2:3], branch="feat"); append_decisions(repo, rows[3:], branch="main")


def test_tokenize_and_bm25_prefer_matching_docs():
    assert tokenize("Use an In-Memory dict cache, with TTL.") == ["use", "an", "in", "memory", "dict", "cache", "with", "ttl"]
    docs = ["cache with ttl", "auth tokens expire", "cache eviction lru"]
    scores = bm25_scores(docs, "cache ttl")
    assert scores[0] > scores[2] > scores[1] == 0


def test_search_relevance_default_and_ignored_excluded(initialized_repo):
    _seed(initialized_repo)
    hits = search_decisions(initialized_repo, query="cache")
    assert [h.decision.id for h in hits] == ["d1", "d3"]        # d4 ignored by default
    assert hits[0].score >= hits[1].score


def test_search_date_sort_and_filters(initialized_repo):
    _seed(initialized_repo)
    assert [h.decision.id for h in search_decisions(initialized_repo)] == ["d3", "d2", "d1"]   # newest first
    assert [h.decision.id for h in search_decisions(initialized_repo, agent=["codex"])] == ["d2"]
    assert [h.decision.id for h in search_decisions(initialized_repo, status=["recorded", "pending"])] == ["d3", "d1"]
    assert [h.decision.id for h in search_decisions(initialized_repo, branch="feat")] == ["d3"]
    assert [h.decision.id for h in search_decisions(initialized_repo, file="src/auth.py")] == ["d2"]
    assert [h.decision.id for h in search_decisions(initialized_repo, made_by="user")] == ["d1"]
    assert [h.decision.id for h in search_decisions(initialized_repo, since="2026-08-02T12:00:00Z")] == ["d3"]
    assert [h.decision.id for h in search_decisions(initialized_repo, status=["ignored"])] == ["d4"]


def test_search_confidence_sort_and_limit(initialized_repo):
    _seed(initialized_repo)
    assert [h.decision.id for h in search_decisions(initialized_repo, sort="confidence")] == ["d1", "d2", "d3"]  # nulls last
    assert len(search_decisions(initialized_repo, limit=2)) == 2


def test_search_since_git_ref(initialized_repo):
    _seed(initialized_repo)
    hits = search_decisions(initialized_repo, since="HEAD")   # HEAD is the fixture's initial commit (today) → nothing seeded after it
    assert hits == []
```

And a CLI smoke test in `tests/test_cli.py`: `search cache --json` returns a JSON list with ids `d1`,`d3`; `search --sort date --limit 1` prints `d3`; `search --file src/auth.py` prints `d2`.

**Step 3: Implement `plumb/search.py`**

```python
"""plumb search: DuckDB enumerates and filters the decision log; BM25 ranks."""
from __future__ import annotations

import math, re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from plumb.decision_log import Decision, _decisions_dir, _clean_duckdb_row
from plumb.traces.repo import parse_ts, commit_datetime

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def bm25_scores(docs: list[str], query: str, k1: float = 1.5, b: float = 0.75) -> list[float]:
    q = tokenize(query)
    toks = [tokenize(d) for d in docs]
    n = len(docs) or 1
    avgdl = (sum(len(t) for t in toks) / n) or 1.0
    df: dict[str, int] = {}
    for t in toks:
        for term in set(t):
            df[term] = df.get(term, 0) + 1
    scores = []
    for t in toks:
        tf: dict[str, int] = {}
        for term in t:
            tf[term] = tf.get(term, 0) + 1
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            f = tf[term]
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * len(t) / avgdl))
        scores.append(s)
    return scores


@dataclass
class Hit:
    decision: Decision
    score: float


def _resolve_since(repo_root: Path, since: Optional[str]) -> Optional[datetime]:
    if not since:
        return None
    dt = parse_ts(since)
    return dt if dt is not None else commit_datetime(repo_root, since)


def search_decisions(repo_root, query: str = "", *, sort: Optional[str] = None,
                     status: Optional[list[str]] = None, agent: Optional[list[str]] = None,
                     branch: Optional[str] = None, file: Optional[str] = None,
                     made_by: Optional[str] = None, since: Optional[str] = None,
                     limit: Optional[int] = None) -> list[Hit]:
    import duckdb
    repo_root = Path(repo_root)
    d = _decisions_dir(repo_root)
    if not d.exists() or not list(d.glob("*.jsonl")):
        return []
    where, params = [], []
    if status:
        where.append(f"status IN ({','.join('?' * len(status))})"); params += status
    else:
        where.append("status <> 'ignored'")
    if agent:
        where.append(f"agent IN ({','.join('?' * len(agent))})"); params += agent
    if branch:
        where.append("branch = ?"); params.append(branch)
    if made_by:
        where.append("made_by = ?"); params.append(made_by)
    if file:
        where.append("len(list_filter(file_refs, r -> r.file = ?)) > 0"); params.append(file)
    since_dt = _resolve_since(repo_root, since)
    if since_dt is not None:
        where.append("try_cast(created_at AS TIMESTAMPTZ) >= ?"); params.append(since_dt)
    if query:
        # cheap prefilter: every query token must appear somewhere in the text
        for tok in tokenize(query):
            where.append("regexp_matches(lower(coalesce(question,'') || ' ' || coalesce(decision,'') || ' ' || coalesce(user_note,'')), ?)")
            params.append(re.escape(tok))
    sql = f"""
        WITH raw AS (SELECT *, ROW_NUMBER() OVER () AS _n
                     FROM read_json_auto('{d / '*.jsonl'}', format='newline_delimited', union_by_name=true)),
             latest AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY _n DESC) AS _rn FROM raw)
        SELECT * EXCLUDE (_n, _rn) FROM latest WHERE _rn = 1 AND {' AND '.join(where)}
    """
    con = duckdb.connect(":memory:")
    rel = con.execute(sql, params)
    cols = [c[0] for c in rel.description]
    rows = [Decision(**_clean_duckdb_row(dict(zip(cols, r)))) for r in rel.fetchall()]
    con.close()

    sort = sort or ("relevance" if query else "date")
    if sort == "relevance" and query:
        scores = bm25_scores([f"{x.question or ''} {x.decision or ''} {x.user_note or ''}" for x in rows], query)
        hits = sorted((Hit(x, s) for x, s in zip(rows, scores)), key=lambda h: (-h.score, h.decision.created_at or ""))
    elif sort == "confidence":
        hits = sorted((Hit(x, 0.0) for x in rows), key=lambda h: (h.decision.confidence is None, -(h.decision.confidence or 0), h.decision.created_at or ""))
    else:
        hits = sorted((Hit(x, 0.0) for x in rows), key=lambda h: h.decision.created_at or "", reverse=True)
    return hits[:limit] if limit else hits
```

Notes: `file_refs` in DuckDB is a `STRUCT(file VARCHAR, lines BIGINT[])[]`; if `list_filter` with a lambda referencing `r.file` fails on the installed DuckDB, fall back to `list_contains(list_transform(file_refs, r -> r.file), ?)`. `union_by_name=true` is needed because older rows lack the provenance columns; if an all-old shard makes DuckDB infer `file_refs` as `JSON`/`VARCHAR`, cast: `CAST(file_refs AS STRUCT(file VARCHAR, lines BIGINT[])[])`. Check `_clean_duckdb_row` handles a `NULL` `file_refs` (returns `[]`). The `read_all_decisions` query in `decision_log.py` does not pass `union_by_name`; if you find it necessary here, add it there too and run its tests.

CLI (`plumb/cli.py`):

```python
@cli.command()
@click.argument("query", nargs=-1)
@click.option("--sort", type=click.Choice(["relevance", "date", "confidence"]), default=None)
@click.option("--status", multiple=True); @click.option("--agent", multiple=True)
@click.option("--branch"); @click.option("--file"); @click.option("--made-by", type=click.Choice(["user", "agent"]))
@click.option("--since", help="ISO date or git ref"); @click.option("--limit", type=int, default=50)
@click.option("--json", "as_json", is_flag=True)
def search(query, sort, status, agent, branch, file, made_by, since, limit, as_json): ...
```
Human output, one decision per two lines: `dec-…  recorded  claude  user  0.90  2026-08-02  aaa  session 019a… turns 4-5` then the decision text wrapped (reuse the `textwrap` pattern from `plumb log`), and `files: src/cache.py:1-9` when file_refs exist. `--json`: `json.dumps([h.decision.model_dump() | {"score": h.score} for h in hits], indent=2)`.

Commit: `feat(cli): plumb search — DuckDB enumeration with filters, BM25 relevance, date/confidence sorts`

---

## Task 7: `plumb review --recorded`

**Files:** `plumb/cli.py` (`review`), `tests/test_cli.py`.

**Test:** seed two `recorded` decisions; invoke `review --recorded` with input `a\nr\n` (and `--reason` prompt input for reject if `review` prompts); assert the first becomes `approved` with `approved_by == "user"`, the second `rejected`, and that no `modify` was invoked (patch `plumb.cli.run_modify` or whatever reject calls in review mode and assert not called when `--recorded`).

**Implement:** `@click.option("--recorded", is_flag=True)` → `filter_decisions(repo_root, status="recorded")`; approve path sets `approved_by="user"`; reject path skips the modify step when `--recorded` (print "Code already committed; rejection recorded, no automatic modification.").

Commit: `feat(cli): plumb review --recorded`

---

## Task 8: Mode-aware instruction block; spec, README, skill

**Files:** `plumb/cli.py` (`_update_claude_md` block selection), both `SKILL.md` copies, `plumb_spec.md`, `README.md`, `docs/plans/2026-08-29-record-mode-design.md` (Status), `tests/test_cli.py`.

- `_update_claude_md(repo_root, cfg)` picks the block by `cfg.mode`: review block unchanged; record block: "Plumb records decisions automatically after each commit (mode: record). Before ending a session run `plumb log --since <base>` and mention notable recorded decisions. Use `plumb search <terms>` before proposing a decision that may contradict a prior one. Pending decisions (below the record threshold) are the user's to resolve — never approve/reject on their behalf." Test: with `cfg.mode="record"`, `CLAUDE.md` and `AGENTS.md` contain `plumb search` and not `AskUserQuestion`; with `review`, the reverse. `plumb mode` must call `_update_claude_md` after saving.
- Skill: add `plumb mode`, `plumb search`, `plumb record-extract`, `plumb review --recorded` rows; a short "Record mode" section.
- Spec: modes and config fields, `recorded` status + `approved_by` in the schema JSON, post-commit extraction flow, `plumb search`/`plumb mode`/`record-extract` command sections, sync filter, `plumb status` counters. README: init step mentions the mode question; command table rows.
- Design doc Status → Implemented.

Commit: `docs: record mode and plumb search`

---

## Out of scope

- Auto-reject/auto-modify in record mode; DuckDB `fts`; a search index.
- Migrating this repo's 470 legacy `commit_sha: null` decisions.
- Re-enabling this repo's pre-commit hook (user's call; `plumb mode` will reinstall hooks in other repos).
