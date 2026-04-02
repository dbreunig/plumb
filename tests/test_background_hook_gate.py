"""E2E tests: background hook analysis with next-commit decision gate.

Validates behavioral contracts from https://github.com/ktinubu/plumb/issues/6:

Test A — Post-commit writes decisions: `run_hook(post_commit=True)` reads the
         just-committed diff (HEAD~1..HEAD), analyzes it, and writes at least
         one decision to `.plumb/decisions/`. Full LLM pipeline, real API.

Test B — Gate blocks on pending decisions: When pending decisions exist from
         a prior background run and nothing is staged, the gate must block
         (exit 1). Currently the hook bails at step 2 (empty diff -> 0),
         completely ignoring pending decisions.

Test C — Gate blocks citing decisions, not auth: When pending decisions exist
         and changes are staged, the gate blocks BECAUSE of pending decisions
         (not because of an auth error). Output mentions "pending decision",
         not backend errors. Proven by removing both LLM backends.

Test D — Gate passes without LLM work: When no pending decisions exist and
         changes are staged, the gate exits 0 without touching the LLM.
         Proven by removing both LLM backends — if the hook tried LLM work
         it would hit PlumbAuthError and exit 1.

All tests: real git, real plumb config, real decision log. No mocks.

See: https://github.com/ktinubu/plumb/issues/6
"""

import io
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest
from git import Repo

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb.decision_log import Decision, append_decisions, read_all_decisions

needs_claude_cli = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude CLI not installed",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_dspy_configured():
    """Reset the module-level _configured flag so each test gets a clean
    DSPy configuration."""
    import plumb.programs as prog
    original = prog._configured
    prog._configured = False
    yield
    prog._configured = original


def _init_plumb_repo(tmp_path: Path) -> Path:
    """Create a git repo with plumb initialized and config committed."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)

    readme = repo_dir / "README.md"
    readme.write_text("# Test Project\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")

    ensure_plumb_dir(repo_dir)
    (repo_dir / ".plumb" / "decisions").mkdir(exist_ok=True)

    spec = repo_dir / "spec.md"
    spec.write_text("# Spec\n\n## Features\n\nThe system must authenticate users.\n")
    (repo_dir / "tests").mkdir(exist_ok=True)

    cfg = PlumbConfig(
        spec_paths=["spec.md"],
        test_paths=["tests/"],
        initialized_at=datetime.now(timezone.utc).isoformat(),
    )
    save_config(repo_dir, cfg)

    repo.index.add([".plumb/config.json", "spec.md"])
    repo.index.commit("add plumb config")

    return repo_dir


def _make_pending_decision() -> Decision:
    return Decision(
        id="dec-bg-001",
        status="pending",
        question="Should auth use JWT or session cookies?",
        decision="Use JWT with RS256 asymmetric signing.",
        made_by="llm",
        branch="main",
        confidence=0.92,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _commit_auth_module(repo_dir: Path) -> None:
    """Commit a Python file containing a clear architectural decision.

    The JWT RS256 vs HS256 choice is explicit enough that the LLM should
    extract at least one spec-relevant decision from it.
    """
    auth_file = repo_dir / "auth.py"
    auth_file.write_text(
        '"""Authentication module using JWT tokens instead of session cookies.\n'
        '\n'
        'Design decision: Use RS256 asymmetric signing for JWT tokens rather\n'
        'than HS256 symmetric signing. This allows services to verify tokens\n'
        'without sharing the signing secret.\n'
        '"""\n'
        '\n'
        'import jwt\n'
        'from datetime import datetime, timedelta\n'
        '\n'
        '\n'
        'def create_token(user_id: str, secret_key: str) -> str:\n'
        '    """Create a JWT token with RS256 signing."""\n'
        '    payload = {\n'
        '        "sub": user_id,\n'
        '        "iat": datetime.utcnow(),\n'
        '        "exp": datetime.utcnow() + timedelta(hours=24),\n'
        '    }\n'
        '    return jwt.encode(payload, secret_key, algorithm="RS256")\n'
        '\n'
        '\n'
        'def verify_token(token: str, public_key: str) -> dict:\n'
        '    """Verify and decode a JWT token."""\n'
        '    return jwt.decode(token, public_key, algorithms=["RS256"])\n'
    )
    Repo(repo_dir).index.add(["auth.py"])
    Repo(repo_dir).index.commit("add auth module with JWT RS256 decision")


def _disable_llm_backends(monkeypatch):
    """Remove both LLM backends so any attempt to call the LLM fails.

    - Empty ANTHROPIC_API_KEY (set, not deleted, so load_dotenv won't restore it)
    - Strip claude from PATH so ClaudeCodeLM fallback also fails
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # Keep only system dirs (git, python, etc.) — exclude claude
    safe_path = "/usr/bin:/bin:/usr/sbin:/sbin"
    monkeypatch.setenv("PATH", safe_path)


# ---------------------------------------------------------------------------
# Test A: Post-commit analysis writes decisions to disk
# ---------------------------------------------------------------------------

@pytest.mark.slow
@needs_claude_cli
class TestPostCommitWritesDecisions:
    """run_hook(post_commit=True) should read the committed diff, run the
    full analysis pipeline, and write at least one decision to disk.

    Fails today: run_hook() does not accept a post_commit parameter.
    """

    def test_post_commit_produces_decision_on_disk(self, tmp_path, monkeypatch):
        """Given a committed diff with a clear architectural decision,
        run_hook(post_commit=True) must write a decision to .plumb/decisions/.

        Fails today: TypeError — post_commit parameter does not exist.
        Once the parameter exists, this also validates that the hook reads
        HEAD~1..HEAD (not --cached) and runs the full extraction pipeline.
        """
        from plumb.git_hook import run_hook

        repo_dir = _init_plumb_repo(tmp_path)
        _commit_auth_module(repo_dir)
        monkeypatch.chdir(repo_dir)

        # Preconditions: nothing staged, committed diff has auth.py
        repo = Repo(repo_dir)
        assert repo.git.diff("--cached", "--name-only") == "", (
            "Precondition: staging area should be empty after commit"
        )
        assert "auth.py" in repo.git.diff("HEAD~1", "HEAD", "--name-only"), (
            "Precondition: HEAD~1..HEAD should contain auth.py"
        )
        assert len(read_all_decisions(repo_dir)) == 0, (
            "Precondition: no decisions should exist before hook runs"
        )

        try:
            run_hook(repo_dir, post_commit=True)
        except TypeError as e:
            if "post_commit" in str(e):
                pytest.fail(
                    "run_hook() does not accept post_commit parameter. "
                    "Issue #6 requires: run_hook(repo_root, dry_run, post_commit)"
                )
            raise

        decisions = read_all_decisions(repo_dir)
        pending = [d for d in decisions if d.status == "pending"]
        assert len(pending) >= 1, (
            f"Expected at least 1 pending decision written to disk after "
            f"post-commit analysis of auth.py (JWT RS256 decision), "
            f"but found {len(pending)}. All decisions: {decisions}"
        )


# ---------------------------------------------------------------------------
# Test B: Gate blocks on pending decisions (nothing staged)
# ---------------------------------------------------------------------------

class TestGateBlocksOnPendingDecisions:
    """When pending decisions exist from a prior background analysis run,
    the pre-commit gate must block the commit (exit 1) — even when nothing
    is staged.

    Fails today: the hook bails at step 2 with exit 0 when the staged diff
    is empty, never checking for pending decisions. The gate from issue #6
    must check pending decisions BEFORE looking at the diff.
    """

    def test_gate_blocks_when_pending_decisions_exist_nothing_staged(self, tmp_path):
        """Pending decisions + nothing staged = commit blocked (exit 1).

        Current behavior: empty diff -> return 0 (decisions ignored).
        Gate behavior:    pending decisions found -> return 1.
        """
        from plumb.git_hook import run_hook

        repo_dir = _init_plumb_repo(tmp_path)
        append_decisions(repo_dir, [_make_pending_decision()], branch="main")

        # Preconditions
        assert Repo(repo_dir).git.diff("--cached", "--name-only") == ""
        pending = [d for d in read_all_decisions(repo_dir) if d.status == "pending"]
        assert len(pending) == 1

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = run_hook(repo_dir)

        assert exit_code == 1, (
            f"Expected exit 1 (blocked by pending decisions from prior "
            f"background run), got {exit_code}. The gate must check pending "
            f"decisions even when nothing is staged.\n"
            f"stdout: {stdout_buf.getvalue()!r}\n"
            f"stderr: {stderr_buf.getvalue()!r}"
        )


# ---------------------------------------------------------------------------
# Test C: Gate blocks citing pending decisions, not auth errors
# ---------------------------------------------------------------------------

class TestGateBlocksCitingDecisions:
    """When pending decisions exist and changes are staged, the gate must
    block BECAUSE of pending decisions — the output should mention
    "pending decision", not backend auth errors.

    Proven by removing both LLM backends (API key + CLI). If the hook
    tried to run the LLM pipeline, it would fail with a backend error.
    The gate should never reach the pipeline.

    Fails today: the hook runs the full LLM pipeline (step 5: validate API),
    which raises PlumbAuthError. The user sees a backend error instead of
    being told about pending decisions.
    """

    def test_gate_output_mentions_pending_decisions_not_backend_error(self, tmp_path, monkeypatch):
        """Pending decisions + staged changes + no LLM backend = exit 1
        with "pending decisions" in output (not auth/backend errors).
        """
        from plumb.git_hook import run_hook

        repo_dir = _init_plumb_repo(tmp_path)
        append_decisions(repo_dir, [_make_pending_decision()], branch="main")

        feature = repo_dir / "feature.py"
        feature.write_text("def new_feature():\n    return True\n")
        Repo(repo_dir).index.add(["feature.py"])

        _disable_llm_backends(monkeypatch)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = run_hook(repo_dir)

        all_output = (stdout_buf.getvalue() + stderr_buf.getvalue()).lower()

        assert exit_code == 1, (
            f"Expected exit 1 (blocked), got {exit_code}"
        )

        assert "pending" in all_output and "decision" in all_output, (
            f"Gate should mention 'pending decisions' in output, but got:\n"
            f"  stdout: {stdout_buf.getvalue()!r}\n"
            f"  stderr: {stderr_buf.getvalue()!r}"
        )

        assert "api_key" not in all_output and "no llm backend" not in all_output, (
            f"Gate should NOT mention backend errors (it shouldn't reach "
            f"the LLM pipeline), but output contains:\n"
            f"  {stderr_buf.getvalue()!r}"
        )


# ---------------------------------------------------------------------------
# Test D: Gate passes without LLM work
# ---------------------------------------------------------------------------

class TestGatePassesWithoutLLMWork:
    """When no pending decisions exist and changes are staged, the
    pre-commit gate must exit 0 without touching the LLM.

    Proven by removing both LLM backends (API key + CLI). If the hook
    tried LLM work, it would hit PlumbAuthError and exit 1.

    Fails today: the hook always runs the full LLM pipeline on staged
    diffs, so it hits the auth wall and exits 1.
    """

    def test_gate_passes_no_pending_no_llm(self, tmp_path, monkeypatch):
        """No pending decisions + staged changes + no LLM backend = exit 0.

        Current behavior: staged diff -> validate_api_access() ->
        no backend -> PlumbAuthError -> exit 1.

        Gate behavior: no pending decisions -> exit 0 (LLM never invoked).
        """
        from plumb.git_hook import run_hook

        repo_dir = _init_plumb_repo(tmp_path)

        feature = repo_dir / "feature.py"
        feature.write_text("def new_feature():\n    return True\n")
        Repo(repo_dir).index.add(["feature.py"])

        _disable_llm_backends(monkeypatch)

        stderr_buf = io.StringIO()

        with redirect_stderr(stderr_buf):
            exit_code = run_hook(repo_dir)

        assert exit_code == 0, (
            f"Expected exit 0 (no pending decisions, gate should pass without "
            f"LLM work), but got {exit_code}. Both LLM backends are disabled, "
            f"so if the hook tried validate_api_access() it would fail.\n"
            f"stderr: {stderr_buf.getvalue()!r}"
        )
