"""End-to-end test of the record-mode user story against the real LLM.

Phases (in file order; each depends on the previous):
  1. init       — plumb init in a fresh repo, answering `record`
  2. record     — a commit with a matching Claude transcript is recorded by the
                  detached worker with full provenance; a threshold split lands
                  rows as pending
  3. diff       — ignored-only commits are skipped; transcript-less commits are
                  extracted diff-only; amend replaces; `plumb diff` previews;
                  `plumb sync` folds recorded decisions into spec + tests
  4. search     — relevance/date/confidence sorts, filters, --json, pipes
  5. review     — `plumb review --recorded` upgrades a row to approved/user

Costs real Haiku tokens (~15 small calls). Run with:

    PLUMB_E2E=1 uv run pytest tests/e2e/test_record_mode.py -q -p no:cacheprovider

The test plants a fixture transcript under ~/.claude/projects/ (its own
directory, removed on teardown) and never touches this repo's .plumb/.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("PLUMB_E2E"),
        reason="end-to-end LLM test; set PLUMB_E2E=1 to run",
    ),
]

PLUMB_REPO = Path(__file__).resolve().parents[2]
PLUMB = shutil.which("plumb")
COMMIT_FAST_SECONDS = 5.0     # record mode must not gate the commit on the LLM
WORKER_TIMEOUT = 150.0

BURST_CHANGE = '''import time


class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: float, burst: int = 0):
        self.max_calls = max_calls
        self.period = period_seconds
        self.burst = burst
        self._calls: list[float] = []

    def allow(self) -> bool:
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < self.period]
        if len(self._calls) < self.max_calls + self.burst:
            self._calls.append(now)
            return True
        return False
'''


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _env() -> dict:
    env = dict(os.environ)
    env.pop("PLUMB_MODE", None)     # the config, not the env, must drive the mode
    return env


def run(args, cwd, input=None, timeout=180, check=True):
    p = subprocess.run(args, cwd=str(cwd), input=input, text=True,
                       capture_output=True, timeout=timeout, env=_env())
    if check and p.returncode != 0:
        raise AssertionError(f"{args} failed rc={p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    return p


def decisions(repo: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for shard in sorted((repo / ".plumb" / "decisions").glob("*.jsonl")):
        for line in shard.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                latest[row["id"]] = row
    return latest


def rows_for(repo: Path, sha: str) -> list[dict]:
    return [d for d in decisions(repo).values() if d.get("commit_sha") == sha]


def head(repo: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], repo).stdout.strip()


def lock_free(repo: Path) -> bool:
    import fcntl
    path = repo / ".plumb" / "record.lock"
    if not path.exists():
        return True
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return True
        except OSError:
            return False


def wait_worker(repo: Path, sha: str, timeout=WORKER_TIMEOUT) -> None:
    """Wait until the worker for `sha` has finished (log line + lock free)."""
    log = repo / ".plumb" / "record.log"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done = log.exists() and sha[:12] in log.read_text()
        if done and lock_free(repo):
            return
        time.sleep(1.0)
    raise AssertionError(
        f"worker for {sha[:12]} did not finish in {timeout}s; "
        f"log:\n{log.read_text() if log.exists() else '<missing>'}"
    )


def git_commit(repo: Path, message: str, amend=False) -> tuple[str, float]:
    args = ["git", "commit", "-q", "-m", message] + (["--amend"] if amend else [])
    t0 = time.monotonic()
    run(args, repo)
    elapsed = time.monotonic() - t0
    return head(repo), elapsed


def write_transcript(projects_dir: Path, repo: Path, session_id: str) -> Path:
    """A minimal but real-format Claude Code transcript about the burst change."""
    now = datetime.now(timezone.utc)
    ts = lambda s: now.replace(microsecond=0).isoformat().replace("+00:00", f".{s:03d}Z")
    base = {"cwd": str(repo), "gitBranch": "main", "sessionId": session_id,
            "isSidechain": False, "isMeta": False}
    entries = [
        {**base, "type": "user", "timestamp": ts(1), "uuid": "u1", "parentUuid": None,
         "message": {"role": "user",
                     "content": "Add a burst allowance to the rate limiter so short spikes are tolerated."}},
        {**base, "type": "assistant", "timestamp": ts(2), "uuid": "a1", "parentUuid": "u1",
         "message": {"id": "msg_1", "role": "assistant", "content": [
             {"type": "text",
              "text": "I'll add a burst parameter: allow() permits up to max_calls + burst calls per window."}]}},
        {**base, "type": "assistant", "timestamp": ts(3), "uuid": "a2", "parentUuid": "a1",
         "message": {"id": "msg_1", "role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_1", "name": "Edit",
              "input": {"file_path": str(repo / "src/trial/limiter.py"),
                        "old_string": "len(self._calls) < self.max_calls",
                        "new_string": "len(self._calls) < self.max_calls + self.burst"}}]}},
        {**base, "type": "user", "timestamp": ts(4), "uuid": "u2", "parentUuid": "a2",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_1",
              "content": "Edited src/trial/limiter.py", "is_error": False}]}},
        {**base, "type": "assistant", "timestamp": ts(5), "uuid": "a3", "parentUuid": "u2",
         "message": {"id": "msg_2", "role": "assistant", "content": [
             {"type": "text",
              "text": "Done. Decision: burst defaults to 0 so existing callers keep the strict limit."}]}},
    ]
    # projects_dir itself plays the role of one ~/.claude/projects/<project>/ dir
    projects_dir.mkdir(parents=True, exist_ok=True)
    f = projects_dir / f"{session_id}.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return f


# --------------------------------------------------------------------------- #
# World
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def world(tmp_path_factory):
    assert PLUMB, "plumb must be on PATH (editable install)"
    env_src = PLUMB_REPO / ".env"
    assert env_src.exists(), "plumb repo .env with ANTHROPIC_API_KEY required"

    repo = tmp_path_factory.mktemp("plumb-e2e")
    (repo / "src" / "trial").mkdir(parents=True)
    (repo / "tests").mkdir()
    run(["git", "init", "-q", "-b", "main"], repo)
    run(["git", "config", "user.email", "e2e@example.invalid"], repo)
    run(["git", "config", "user.name", "Plumb E2E"], repo)
    (repo / ".env").write_text(
        next(l for l in env_src.read_text().splitlines() if l.startswith("ANTHROPIC_API_KEY=")) + "\n")
    (repo / ".gitignore").write_text(".env\n__pycache__/\n.pytest_cache/\n")
    (repo / "spec.md").write_text(
        "# Trial App Spec\n\n## Overview\nA tiny library for rate limiting API calls.\n\n"
        "## Requirements\n"
        "- `RateLimiter(max_calls, period_seconds)` allows at most `max_calls` calls per `period_seconds`.\n"
        "- `allow()` returns True when a call is permitted and False otherwise.\n"
        "- The limiter uses a sliding window.\n")
    (repo / "src" / "trial" / "__init__.py").write_text('"""Trial app."""\n')
    (repo / "src" / "trial" / "limiter.py").write_text(
        BURST_CHANGE.replace(", burst: int = 0", "").replace("\n        self.burst = burst", "")
        .replace(" + self.burst", ""))
    (repo / "tests" / "test_limiter.py").write_text(
        "from trial.limiter import RateLimiter\n\n\n"
        "def test_allows_up_to_max():\n"
        "    rl = RateLimiter(2, 60)\n"
        "    assert rl.allow() and rl.allow()\n"
        "    assert not rl.allow()\n")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "trial"\nversion = "0.0.1"\nrequires-python = ">=3.10"\n'
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-q", "-m", "Initial rate limiter"], repo)

    projects_dir = Path.home() / ".claude" / "projects" / f"plumb-e2e-{uuid.uuid4().hex[:8]}"
    state = {"repo": repo, "projects_dir": projects_dir, "session": f"e2e-{uuid.uuid4().hex[:8]}"}
    try:
        yield state
    finally:
        shutil.rmtree(projects_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Phase 1: init
# --------------------------------------------------------------------------- #

def test_phase1_init(world):
    repo = world["repo"]
    p = run([PLUMB, "init"], repo, input="spec.md\ntests/\nrecord\n", timeout=300)

    cfg = json.loads((repo / ".plumb" / "config.json").read_text())
    assert cfg["mode"] == "record" and cfg["record_threshold"] is None
    assert cfg["spec_paths"] == ["spec.md"] and cfg["test_paths"] == ["tests/"]
    gi = (repo / ".plumb" / ".gitignore").read_text()
    assert "record.log" in gi and "record.lock" in gi
    assert (repo / ".plumbignore").exists()
    for hook in ("pre-commit", "post-commit"):
        h = repo / ".git" / "hooks" / hook
        assert h.exists() and os.access(h, os.X_OK) and "plumb" in h.read_text()
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (repo / name).read_text()
        assert text.count("plumb:start") == 1
        assert "plumb search" in text and "plumb review --recorded" in text
        assert "AskUserQuestion" not in text
    reqs = json.loads((repo / ".plumb" / "requirements.json").read_text())
    assert len(reqs) >= 3 and all(r["id"].startswith("req-") and r["text"] for r in reqs)
    assert any("sliding" in r["text"].lower() for r in reqs)

    status = run([PLUMB, "status"], repo).stdout
    assert "Mode: record (from config)" in status
    assert "Pending decisions: 0" in status and "Sync debt" not in status
    assert run([PLUMB, "mode"], repo).stdout.split("(")[0].strip() == "record"


# --------------------------------------------------------------------------- #
# Phase 2: record
# --------------------------------------------------------------------------- #

def test_phase2_recorded_with_provenance(world):
    repo = world["repo"]
    time.sleep(1.1)  # transcript timestamps must be strictly after the parent commit
    write_transcript(world["projects_dir"], repo, world["session"])
    (repo / "src" / "trial" / "limiter.py").write_text(BURST_CHANGE)
    run(["git", "add", "src/trial/limiter.py"], repo)
    sha, elapsed = git_commit(repo, "Add burst allowance")
    world["sha_burst"] = sha
    assert elapsed < COMMIT_FAST_SECONDS, f"commit took {elapsed:.1f}s — record mode must not gate"

    wait_worker(repo, sha)
    rows = rows_for(repo, sha)
    assert rows, f"no decisions recorded for {sha[:12]}"
    recorded = [r for r in rows if r["status"] == "recorded"]
    assert recorded, rows
    for r in recorded:
        assert r["approved_by"] == "auto"
        assert r["branch"] == "main"
        assert r["agent"] == "claude"
        assert r["session_id"] == world["session"]
        assert isinstance(r["turn_range"], list) and len(r["turn_range"]) == 2
        assert isinstance(r["evidence_digest"], str) and len(r["evidence_digest"]) == 64
        assert r["conversation_available"] is True
    assert any(r["file_refs"] for r in recorded), "no file_refs on any recorded decision"
    for r in recorded:
        for ref in r["file_refs"]:
            assert ref["file"] == "src/trial/limiter.py"
            assert len(ref["lines"]) == 2

    log_out = run([PLUMB, "log"], repo).stdout
    assert sha[:12] in log_out and "uncommitted" not in log_out
    verify_out = run([PLUMB, "log", "--verify"], repo).stdout
    assert "ok" in verify_out and "stale" not in verify_out
    status = run([PLUMB, "status"], repo).stdout
    assert "recorded, unsynced" in status


def test_phase2_threshold_splits_to_pending(world):
    repo = world["repo"]
    cfg_path = repo / ".plumb" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["record_threshold"] = 0.99
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    try:
        time.sleep(1.1)
        (repo / "src" / "trial" / "limiter.py").write_text(
            BURST_CHANGE
            + "\n    def remaining(self) -> int:\n"
            + '        """Calls still permitted in the current window."""\n'
            + "        now = time.monotonic()\n"
            + "        live = [t for t in self._calls if now - t < self.period]\n"
            + "        return max(0, self.max_calls + self.burst - len(live))\n")
        run(["git", "add", "-A"], repo)
        sha, elapsed = git_commit(repo, "Add remaining() to expose unused quota")
        world["sha_threshold"] = sha
        assert elapsed < COMMIT_FAST_SECONDS
        wait_worker(repo, sha)
        rows = rows_for(repo, sha)
        assert rows
        for r in rows:
            if r["status"] == "recorded":
                assert r["confidence"] is not None and r["confidence"] >= 0.99
            else:
                assert r["status"] == "pending" and r.get("approved_by") is None
    finally:
        cfg = json.loads(cfg_path.read_text())
        cfg["record_threshold"] = None
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Phase 3: diff integration
# --------------------------------------------------------------------------- #

def test_phase3_ignored_only_commit_is_skipped(world):
    repo = world["repo"]
    before = set(decisions(repo))
    (repo / "README.md").write_text("# Trial\n")
    run(["git", "add", "README.md"], repo)
    sha, _ = git_commit(repo, "Add README")
    wait_worker(repo, sha, timeout=30)
    assert rows_for(repo, sha) == []
    assert set(decisions(repo)) == before


def test_phase3_diff_only_extraction(world):
    repo = world["repo"]
    time.sleep(1.1)
    (repo / "src" / "trial" / "limiter.py").write_text(
        (repo / "src" / "trial" / "limiter.py").read_text()
        + "\n\ndef reset(limiter: RateLimiter) -> None:\n    limiter._calls.clear()\n")
    run(["git", "add", "-A"], repo)
    sha, _ = git_commit(repo, "Add reset helper")
    world["sha_reset"] = sha
    wait_worker(repo, sha)
    rows = rows_for(repo, sha)
    assert rows, "diff-only fallback produced no decisions"
    for r in rows:
        assert r["conversation_available"] is False
        assert r.get("agent") is None
        assert r["file_refs"] and all(
            ref["file"] == "src/trial/limiter.py" for ref in r["file_refs"])


def test_phase3_amend_replaces_decisions(world):
    repo = world["repo"]
    old_sha = world["sha_reset"]
    time.sleep(1.1)
    world["iso_before_amend"] = datetime.now(timezone.utc).isoformat()
    (repo / "src" / "trial" / "limiter.py").write_text(
        (repo / "src" / "trial" / "limiter.py").read_text().replace(
            "limiter._calls.clear()", "limiter._calls = []"))
    run(["git", "add", "-A"], repo)
    new_sha, _ = git_commit(repo, "Add reset helper", amend=True)
    world["sha_amend"] = new_sha
    assert new_sha != old_sha
    wait_worker(repo, new_sha)
    assert rows_for(repo, old_sha) == [], "replaced commit's decisions were not deleted"
    assert rows_for(repo, new_sha), "amended commit was not re-extracted"


def test_phase3_plumb_diff_previews_without_writing(world):
    repo = world["repo"]
    before = set(decisions(repo))
    (repo / "src" / "trial" / "limiter.py").write_text(
        (repo / "src" / "trial" / "limiter.py").read_text() + "\n# staged comment\n")
    run(["git", "add", "-A"], repo)
    try:
        out = run([PLUMB, "diff"], repo, timeout=300).stdout
        assert "decision" in out.lower()
        assert set(decisions(repo)) == before, "plumb diff must not write decisions"
    finally:
        run(["git", "checkout", "--", "src/trial/limiter.py"], repo)
        run(["git", "reset", "-q"], repo)


def test_phase3_sync_updates_spec_and_tests(world):
    repo = world["repo"]
    spec_before = (repo / "spec.md").read_text()
    run([PLUMB, "sync"], repo, timeout=600)
    assert (repo / "spec.md").read_text() != spec_before, "sync did not update the spec"
    for r in decisions(repo).values():
        if r["status"] == "recorded":
            assert r["synced_at"], f"recorded decision {r['id']} not marked synced"
    assert "Sync debt" not in run([PLUMB, "status"], repo).stdout
    # generated tests (if any) must pass along with the originals
    run(["python3", "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"], repo, timeout=300)


# --------------------------------------------------------------------------- #
# Phase 4: search
# --------------------------------------------------------------------------- #

def _search_json(repo, *args):
    out = run([PLUMB, "search", *args, "--json"], repo).stdout
    return json.loads(out)


def test_phase4_search(world):
    repo = world["repo"]

    hits = _search_json(repo, "burst")
    assert hits, "no hits for 'burst'"
    assert all(h["score"] > 0 for h in hits)
    assert any(h["commit_sha"] == world["sha_burst"] for h in hits)

    assert run([PLUMB, "search", "zzzznope"], repo).stdout.strip() == "No decisions."

    by_date = _search_json(repo, "--sort", "date")
    stamps = [h["created_at"] for h in by_date]
    assert stamps == sorted(stamps, reverse=True)

    by_conf = _search_json(repo, "--sort", "confidence")
    confs = [h["confidence"] for h in by_conf if h["confidence"] is not None]
    assert confs == sorted(confs, reverse=True)

    recorded = _search_json(repo, "--status", "recorded")
    assert recorded and all(h["status"] == "recorded" for h in recorded)

    by_file = _search_json(repo, "--file", "src/trial/limiter.py")
    assert by_file and all(
        any(ref["file"] == "src/trial/limiter.py" for ref in h["file_refs"]) for h in by_file)

    by_agent = _search_json(repo, "--agent", "claude")
    assert by_agent and all(h["agent"] == "claude" for h in by_agent)

    since_amend = _search_json(repo, "--since", world["iso_before_amend"])
    assert since_amend and all(h["commit_sha"] == world["sha_amend"] for h in since_amend)
    assert len(_search_json(repo, "--since", "2020-01-01")) == len(_search_json(repo))
    assert run([PLUMB, "search", "--since", "bogus-ref"], repo, check=False).returncode == 1

    piped = subprocess.run(f"{PLUMB} search --limit 50 | head -1", shell=True,
                           cwd=str(repo), capture_output=True, text=True, env=_env())
    assert piped.returncode == 0 and "Traceback" not in piped.stderr


# --------------------------------------------------------------------------- #
# Phase 5: review --recorded
# --------------------------------------------------------------------------- #

def test_phase5_review_recorded_upgrades(world):
    repo = world["repo"]
    recorded_ids = [d["id"] for d in decisions(repo).values() if d["status"] == "recorded"]
    assert recorded_ids
    run([PLUMB, "review", "--recorded"], repo, input="a\n" * len(recorded_ids), timeout=120)
    after = decisions(repo)
    for rid in recorded_ids:
        assert after[rid]["status"] == "approved"
        assert after[rid]["approved_by"] == "user"
    approved = _search_json(repo, "--status", "approved")
    assert {h["id"] for h in approved} >= set(recorded_ids)
