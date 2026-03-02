from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from git import Repo

from plumb import PlumbAuthError
from plumb.config import load_config, save_config, find_repo_root
from plumb.ignore import parse_plumbignore, is_ignored
from plumb.conversation import (
    read_conversation,
    reduce_noise,
    chunk_conversation,
)
from plumb.decision_log import (
    Decision,
    generate_decision_id,
    read_decisions,
    append_decisions,
    delete_decisions_by_commit,
    deduplicate_decisions,
    filter_decisions,
)


def _get_staged_diff(repo: Repo) -> str:
    return repo.git.diff("--cached")


def _get_plumb_managed_paths(config) -> list[str]:
    """Return paths managed by plumb that should be excluded from diff analysis."""
    return [".plumb/"] + list(config.spec_paths)


def _get_staged_diff_filtered(repo: Repo, config) -> str:
    """Get staged diff excluding plumb-managed and ignored files."""
    managed = _get_plumb_managed_paths(config)
    ignore_patterns = parse_plumbignore(repo.working_dir)
    staged_files = repo.git.diff("--cached", "--name-only").splitlines()
    if not staged_files:
        return ""
    unmanaged = [
        f for f in staged_files
        if not any(f == m or f.startswith(m) for m in managed)
        and not is_ignored(f, ignore_patterns)
    ]
    if not unmanaged:
        return ""
    return repo.git.diff("--cached", "--", *unmanaged)


def _get_branch_name(repo: Repo) -> str:
    try:
        return str(repo.active_branch)
    except TypeError:
        return "HEAD"


def _detect_amend(repo: Repo, last_commit: str | None) -> bool:
    """Compare HEAD's parent SHA to last_commit. If equal, this is an amend."""
    if not last_commit:
        return False
    try:
        head = repo.head.commit
        if head.parents:
            parent_sha = str(head.parents[0])
            return parent_sha == last_commit
    except Exception:
        pass
    return False


def _check_broken_refs(repo: Repo, decisions: list[Decision]) -> list[Decision]:
    """Flag decisions with unreachable commit SHAs."""
    updated = []
    for d in decisions:
        if d.commit_sha:
            try:
                repo.commit(d.commit_sha)
                d_copy = d.model_copy(update={"ref_status": "ok"})
            except Exception:
                d_copy = d.model_copy(update={"ref_status": "broken"})
            updated.append(d_copy)
        else:
            updated.append(d)
    return updated


def _analyze_diff(diff: str) -> str:
    """Run DiffAnalyzer on the staged diff. Returns summary string."""
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.diff_analyzer import DiffAnalyzer

    configure_dspy()
    analyzer = DiffAnalyzer()
    summaries = run_with_retries(analyzer, diff)
    lines = []
    for s in summaries:
        lines.append(f"[{s.change_type}] {', '.join(s.files_changed)}: {s.summary}")
    return "\n".join(lines)


def _extract_decisions_from_conversation(
    repo_root: Path, config, diff_summary: str
) -> list[Decision]:
    """Read conversation log, chunk it, run DecisionExtractor per chunk."""
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.decision_extractor import DecisionExtractor

    turns = read_conversation(
        repo_root,
        config_path=config.claude_log_path,
        since_commit=config.last_commit,
        since_datetime=config.last_extracted_at,
    )
    if not turns:
        return []

    turns = reduce_noise(turns)
    chunks = chunk_conversation(turns)

    configure_dspy()
    extractor = DecisionExtractor()
    now = datetime.now(timezone.utc).isoformat()
    branch = _get_branch_name(Repo(repo_root))

    all_decisions: list[Decision] = []
    for chunk in chunks:
        try:
            extracted = run_with_retries(
                extractor, chunk.text, diff_summary
            )
        except Exception:
            continue
        for ed in extracted:
            if not ed.spec_relevant:
                continue
            all_decisions.append(
                Decision(
                    id=generate_decision_id(),
                    status="pending",
                    question=ed.question,
                    decision=ed.decision,
                    made_by=ed.made_by,
                    branch=branch,
                    confidence=ed.confidence,
                    chunk_index=chunk.chunk_index,
                    conversation_available=True,
                    created_at=now,
                )
            )
    return all_decisions


def _extract_decisions_from_diff(diff_summary: str, branch: str) -> list[Decision]:
    """Fallback: extract decisions from diff summary alone."""
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.decision_extractor import DecisionExtractor

    configure_dspy()
    extractor = DecisionExtractor()
    now = datetime.now(timezone.utc).isoformat()

    try:
        extracted = run_with_retries(
            extractor,
            f"No conversation available. Diff summary:\n{diff_summary}",
            diff_summary,
        )
    except Exception:
        return []

    decisions = []
    for ed in extracted:
        if not ed.spec_relevant:
            continue
        decisions.append(
            Decision(
                id=generate_decision_id(),
                status="pending",
                question=ed.question,
                decision=ed.decision,
                made_by=ed.made_by,
                branch=branch,
                confidence=ed.confidence,
                conversation_available=False,
                created_at=now,
            )
        )
    return decisions


def _synthesize_questions(decisions: list[Decision]) -> list[Decision]:
    """For decisions with no question, run QuestionSynthesizer."""
    import dspy
    from plumb.programs import configure_dspy, run_with_retries, get_program_lm
    from plumb.programs.question_synthesizer import QuestionSynthesizer

    configure_dspy()
    synth = QuestionSynthesizer()
    override_lm = get_program_lm("question_synthesizer")
    result = []
    for d in decisions:
        if not d.question and d.decision:
            try:
                if override_lm:
                    with dspy.context(lm=override_lm):
                        question = run_with_retries(synth, d.decision)
                else:
                    question = run_with_retries(synth, d.decision)
                d = d.model_copy(update={"question": question})
            except Exception:
                pass
        result.append(d)
    return result


def _format_tty_output(pending: list[Decision]) -> str:
    """Human-readable summary for TTY output."""
    lines = [f"\nPlumb found {len(pending)} pending decision(s):\n"]
    for i, d in enumerate(pending, 1):
        lines.append(f"  {i}. [{d.id}]")
        if d.question:
            lines.append(f"     Question: {d.question}")
        if d.decision:
            lines.append(f"     Decision: {d.decision}")
        lines.append(f"     Made by: {d.made_by or 'unknown'} (confidence: {d.confidence or 'N/A'})")
        lines.append("")
    lines.append("Run 'plumb review' to approve, reject, or edit these decisions.")
    return "\n".join(lines)


def _format_json_output(pending: list[Decision]) -> str:
    """Machine-readable JSON for non-TTY (subprocess) output."""
    return json.dumps(
        {
            "pending_decisions": len(pending),
            "decisions": [
                {
                    "id": d.id,
                    "question": d.question,
                    "decision": d.decision,
                    "made_by": d.made_by,
                    "confidence": d.confidence,
                }
                for d in pending
            ],
        },
        indent=2,
    )


def run_hook(repo_root: str | Path | None = None, dry_run: bool = False) -> int:
    """Central hook orchestrator. Returns exit code (0 = allow commit, 1 = block).

    Top-level try/except: on ANY internal error, print warning to stderr, return 0.
    Never block commits due to internal Plumb errors.
    Auth errors block commits — a missing/invalid API key must be fixed.
    """
    try:
        return _run_hook_inner(repo_root, dry_run)
    except PlumbAuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Warning: Plumb encountered an error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 0


def _run_hook_inner(repo_root: str | Path | None, dry_run: bool) -> int:
    import time

    timings: list[tuple[str, float]] = []

    def _timed(label):
        """Context manager that records elapsed time for a stage."""
        class _Timer:
            def __enter__(self):
                self.start = time.monotonic()
                return self
            def __exit__(self, *args):
                timings.append((label, time.monotonic() - self.start))
        return _Timer()

    def _print_timings():
        total = sum(t for _, t in timings)
        print(f"\n[timing] Hook total: {total:.2f}s", file=sys.stderr)
        for label, elapsed in timings:
            pct = (elapsed / total * 100) if total > 0 else 0
            print(f"[timing]   {label}: {elapsed:.2f}s ({pct:.0f}%)", file=sys.stderr)

    # 1. Load config
    with _timed("Load config"):
        if repo_root is None:
            repo_root = find_repo_root()
        if repo_root is None:
            return 0
        repo_root = Path(repo_root)

        config = load_config(repo_root)
        if config is None:
            return 0

        repo = Repo(repo_root)

    # 2. Get staged diff and branch (excluding plumb-managed files)
    with _timed("Staged diff"):
        diff = _get_staged_diff_filtered(repo, config)
        if not diff:
            return 0

        branch = _get_branch_name(repo)

    # 3. Amend detection
    with _timed("Amend detection"):
        if _detect_amend(repo, config.last_commit):
            delete_decisions_by_commit(repo_root, config.last_commit)

    # 4. Check broken refs
    with _timed("Check broken refs"):
        existing_decisions = read_decisions(repo_root)
        existing_decisions = _check_broken_refs(repo, existing_decisions)

    # 5. Validate API access before any LLM work
    with _timed("Validate API"):
        from plumb.programs import validate_api_access
        validate_api_access()

    # 6. Analyze diff
    with _timed("Analyze diff"):
        diff_summary = _analyze_diff(diff)

    # 7. Extract decisions from conversation (or diff-only fallback)
    with _timed("Extract decisions"):
        conv_decisions = _extract_decisions_from_conversation(
            repo_root, config, diff_summary
        )
        if not conv_decisions:
            conv_decisions = _extract_decisions_from_diff(diff_summary, branch)

    # 8. Merge/dedup (also filter against already-resolved decisions)
    with _timed("Dedup"):
        conv_decisions = deduplicate_decisions(conv_decisions, existing_decisions=existing_decisions, use_llm=True)

    # 9. Synthesize questions for questionless decisions
    with _timed("Synthesize questions"):
        conv_decisions = _synthesize_questions(conv_decisions)

    # 10. Write decisions (unless dry_run)
    with _timed("Write decisions"):
        if not dry_run and conv_decisions:
            append_decisions(repo_root, conv_decisions)
            config.last_extracted_at = datetime.now(timezone.utc).isoformat()
            save_config(repo_root, config)

    # 11. Check pending decisions
    with _timed("Check pending"):
        if dry_run:
            _print_timings()
            if conv_decisions:
                print(_format_tty_output(conv_decisions))
            else:
                print("No decisions detected in staged changes.")
            return 0

        all_decisions = read_decisions(repo_root)
        pending = [d for d in all_decisions if d.status == "pending"]

    if pending:
        _print_timings()
        is_tty = sys.stdout.isatty()
        if is_tty:
            print(_format_tty_output(pending))
        else:
            print(_format_json_output(pending))
        return 1

    # No pending decisions — run coverage
    with _timed("Coverage report"):
        try:
            from plumb.coverage_reporter import print_coverage_report
            print_coverage_report(repo_root)
        except Exception:
            pass

    _print_timings()
    return 0


def run_post_commit(repo_root: str | Path | None = None) -> None:
    """Post-commit hook: update last_commit to the newly created commit SHA.

    Called after git successfully creates a commit, so HEAD now points to
    the actual commit (not its parent). This ensures the next pre-commit
    hook only reads conversation since this commit.
    """
    try:
        if repo_root is None:
            repo_root = find_repo_root()
        if repo_root is None:
            return
        repo_root = Path(repo_root)

        config = load_config(repo_root)
        if config is None:
            return

        repo = Repo(repo_root)
        config.last_commit = str(repo.head.commit)
        config.last_commit_branch = _get_branch_name(repo)
        config.last_extracted_at = None
        save_config(repo_root, config)
    except Exception:
        pass
