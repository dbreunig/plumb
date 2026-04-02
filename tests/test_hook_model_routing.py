"""E2E tests: verify that all hook LLM calls respect program_models config.

Uses a transparent CLI shim (shell script) that logs all arguments before
delegating to the real claude binary. This tests the full real code path —
real git, real claude -p, real LLM responses — while capturing every
invocation's --model argument.

Behavioral tests for https://github.com/ktinubu/plumb/issues/3:
- All hook LLM calls should respect program_models config
- Default (no config) should use sonnet
- Invalid CLI models (e.g. groq/) should fail gracefully

All tests: real git, real claude -p (via shim), real plumb hook. No mocks.
Marked slow — requires claude CLI installed and authenticated.
"""

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from git import Repo

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir


needs_claude_cli = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude CLI not installed",
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_dspy_configured():
    """Reset the module-level _configured flag so each test gets a clean
    DSPy configuration (configure_dspy() uses a one-shot guard)."""
    import plumb.programs as prog
    original = prog._configured
    prog._configured = False
    yield
    prog._configured = original


@pytest.fixture
def claude_shim(tmp_path, monkeypatch):
    """Transparent claude proxy that logs args then delegates to the real binary.

    Returns the path to the log file. Each line contains the arguments from
    one invocation of the shim.
    """
    real_claude = shutil.which("claude")
    if real_claude is None:
        pytest.skip("claude CLI not installed")

    bin_dir = tmp_path / "shim_bin"
    bin_dir.mkdir()
    log_file = tmp_path / "claude_calls.log"

    shim = bin_dir / "claude"
    shim.write_text(
        f'#!/bin/sh\n'
        f'echo "$@" >> "{log_file}"\n'
        f'exec "{real_claude}" "$@"\n'
    )
    shim.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    # Ensure no API key is set — force the CLI path
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    return log_file


def parse_shim_log(log_file: Path) -> list[dict]:
    """Parse the shim log file into a list of call records.

    Each record has:
      - 'args': the raw argument string
      - 'model': the --model value (or None if not present)
    """
    if not log_file.exists():
        return []
    calls = []
    for line in log_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        model = None
        match = re.search(r'--model\s+(\S+)', line)
        if match:
            model = match.group(1)
        calls.append({"args": line, "model": model})
    return calls


def create_test_repo(tmp_path: Path, program_models: dict | None = None) -> Path:
    """Create a git repo with plumb initialized and a staged diff.

    The staged diff adds a Python file with an architectural decision
    (auth module using JWT) that should trigger the DecisionExtractor.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)

    # Initial commit
    readme = repo_dir / "README.md"
    readme.write_text("# Test Project\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")

    # Initialize plumb
    ensure_plumb_dir(repo_dir)
    (repo_dir / ".plumb" / "decisions").mkdir(exist_ok=True)

    spec = repo_dir / "spec.md"
    spec.write_text("# Spec\n\n## Features\n\nThe system must authenticate users.\n")

    tests_dir = repo_dir / "tests"
    tests_dir.mkdir(exist_ok=True)

    cfg = PlumbConfig(
        spec_paths=["spec.md"],
        test_paths=["tests/"],
        initialized_at=datetime.now(timezone.utc).isoformat(),
        program_models=program_models or {},
    )
    save_config(repo_dir, cfg)

    # Commit plumb config
    repo.index.add([".plumb/config.json", "spec.md"])
    repo.index.commit("add plumb config")

    # Stage a diff that contains a clear architectural decision
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
    repo.index.add(["auth.py"])

    return repo_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.slow
@needs_claude_cli
class TestHookModelRouting:
    """Verify which --model argument each hook step passes to claude -p."""

    def test_default_config_hook_completes(self, tmp_path, claude_shim, monkeypatch):
        """With no program_models config, the hook should still run and
        make LLM calls using whatever default model is configured."""
        from plumb.git_hook import run_hook

        repo_dir = create_test_repo(tmp_path, program_models={})
        monkeypatch.chdir(repo_dir)
        exit_code = run_hook(repo_dir, dry_run=False)

        calls = parse_shim_log(claude_shim)

        # At least 3 claude -p calls should happen (validate, analyze, extract)
        assert len(calls) >= 3, (
            f"Expected at least 3 claude -p calls, got {len(calls)}. Calls: {calls}"
        )

        # Hook should not crash — returns 0 (allow) or 1 (pending decisions)
        assert exit_code in (0, 1), f"Unexpected exit code: {exit_code}"

    def test_program_models_haiku_overrides_dedup_and_synth(self, tmp_path, claude_shim, monkeypatch):
        """When program_models maps dedup and synthesizer to haiku,
        those steps should use haiku."""
        from plumb.git_hook import run_hook

        repo_dir = create_test_repo(tmp_path, program_models={
            "decision_deduplicator": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
            "question_synthesizer": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
        })
        monkeypatch.chdir(repo_dir)
        run_hook(repo_dir, dry_run=True)

        calls = parse_shim_log(claude_shim)
        models = [c["model"] for c in calls]

        # Dedup and/or synth calls should use haiku from config
        haiku_calls = [m for m in models if m == "claude-haiku-4-5-20251001"]
        assert len(haiku_calls) >= 1, (
            f"Expected at least 1 haiku call for dedup/synth, got none. "
            f"All models: {models}"
        )

    def test_all_programs_respect_haiku_config(self, tmp_path, claude_shim, monkeypatch):
        """When config maps ALL programs to haiku, every call should use haiku.

        This is the synth-setter scenario. Steps 5-7 (validate, analyze,
        extract) must respect diff_analyzer and decision_extractor config
        entries — not ignore them and hardcode sonnet.
        """
        from plumb.git_hook import run_hook

        repo_dir = create_test_repo(tmp_path, program_models={
            "diff_analyzer": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
            "decision_extractor": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
            "decision_deduplicator": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
            "question_synthesizer": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
        })
        monkeypatch.chdir(repo_dir)
        run_hook(repo_dir, dry_run=True)

        calls = parse_shim_log(claude_shim)
        models = [c["model"] for c in calls]

        assert len(models) >= 3, (
            f"Expected at least 3 calls, got {len(calls)}. Calls: {calls}"
        )

        # Call 1 is validate_api_access (smoke test) — uses default, no config key
        # Calls 2+ (analyze, extract, dedup, synth) should all use haiku
        for i, model in enumerate(models[1:], start=2):
            assert model == "claude-haiku-4-5-20251001", (
                f"Call {i} used --model {model}, expected claude-haiku-4-5-20251001. "
                f"All configured program calls should respect program_models config. "
                f"All models: {models}"
            )

    def test_haiku_config_produces_decisions_on_disk(self, tmp_path, monkeypatch):
        """Full end-to-end: configure haiku for all programs, run the hook,
        and verify decisions are extracted and written to disk."""
        from plumb.git_hook import run_hook
        from plumb.decision_log import read_decisions

        repo_dir = create_test_repo(tmp_path, program_models={
            "diff_analyzer": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
            "decision_extractor": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
            "decision_deduplicator": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
            "question_synthesizer": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "max_tokens": 8192,
            },
        })
        monkeypatch.chdir(repo_dir)

        # No decisions before the hook
        repo = Repo(repo_dir)
        branch = str(repo.active_branch)
        assert read_decisions(repo_dir, branch=branch) == []

        exit_code = run_hook(repo_dir, dry_run=False)

        # Hook should block with pending decisions (exit 1)
        assert exit_code == 1, (
            f"Expected exit 1 (pending decisions), got {exit_code}"
        )

        # Decisions should be written to disk
        decisions = read_decisions(repo_dir, branch=branch)
        assert len(decisions) >= 1, (
            f"Expected at least 1 decision written to disk, got {len(decisions)}"
        )

        # Each decision should have the expected fields populated
        for d in decisions:
            assert d.status == "pending"
            assert d.decision, f"Decision {d.id} has empty decision field"
