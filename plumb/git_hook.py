from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from git import Repo

from plumb import PlumbAuthError
from plumb.config import load_config, save_config, find_repo_root
from plumb.ignore import parse_plumbignore, is_ignored
from plumb.conversation import (
    read_conversation_with_refs,
    parents_from_refs,
    reduce_noise,
    chunk_conversation,
)
from plumb.decision_log import (
    Decision,
    generate_decision_id,
    read_all_decisions,
    append_decisions,
    delete_decisions_by_commit,
    deduplicate_decisions,
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
    repo_root: Path,
    config,
    diff_summary: str,
    since_commit: str | None = None,
    since_datetime: str | None = None,
    hunks: dict[str, list[list[int]]] | None = None,
) -> list[Decision]:
    """Stage 1+2: read every agent's sessions, chunk per session, run
    DecisionExtractor per chunk, and stamp provenance + deterministic file_refs.

    ``since_commit`` / ``since_datetime`` bound which transcript turns are read.
    Either defaults to the config's ``last_commit`` / ``last_extracted_at`` when
    ``None`` (review mode); record mode passes the previous commit explicitly.
    ``hunks`` (file -> [[start, end], ...]) is the line-range source for
    file_refs; when ``None`` the staged hunks are read (review mode), record
    mode passes the landed commit's hunks."""
    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.decision_extractor import DecisionExtractor
    from plumb.traces.hunks import staged_hunks, file_refs_for

    if since_commit is None:
        since_commit = config.last_commit
    if since_datetime is None:
        since_datetime = config.last_extracted_at
    turns, refs = read_conversation_with_refs(
        repo_root,
        since_commit=since_commit,
        since_datetime=since_datetime,
    )
    if not turns:
        return []

    turns = reduce_noise(turns)
    chunks = chunk_conversation(turns, parents=parents_from_refs(refs))

    configure_dspy()
    extractor = DecisionExtractor()
    now = datetime.now(timezone.utc).isoformat()
    repo = Repo(repo_root)
    branch = _get_branch_name(repo)
    if hunks is None:
        hunks = staged_hunks(repo)

    all_decisions: list[Decision] = []
    for chunk in chunks:
        try:
            extracted = run_with_retries(extractor, chunk.text, diff_summary)
        except Exception:
            continue
        ref = refs.get((chunk.agent, chunk.session_id))
        # Digest and file_refs cover only turn_start..turn_end (not the
        # one-turn overlap the extractor sees) so both are reproducible
        # from (source_path, turn_range) alone.
        edited = [
            p
            for t in chunk.turns
            if chunk.turn_start <= t.ordinal <= chunk.turn_end
            for tc in t.tool_calls
            if tc.category in ("Edit", "Write")
            for p in tc.file_paths
        ]
        file_refs = file_refs_for(edited, hunks, repo_root, cwd=ref.cwd if ref else None)
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
                    conversation_available=True,
                    created_at=now,
                    agent=chunk.agent,
                    session_id=chunk.session_id,
                    parent_session_id=chunk.parent_session_id,
                    source_path=ref.path if ref else None,
                    turn_range=[chunk.turn_start, chunk.turn_end],
                    evidence_digest=chunk.evidence_digest(),
                    file_refs=file_refs,
                    conversation_truncated=chunk.truncated,
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


class _StageTimer:
    """Context manager that appends ``(label, seconds)`` to ``timings`` on exit."""

    def __init__(self, timings: list | None, label: str):
        self.timings = timings
        self.label = label

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *args):
        if self.timings is not None:
            self.timings.append((self.label, time.monotonic() - self.start))


def extract_decisions(
    repo_root,
    config,
    diff: str,
    branch: str,
    since_commit: str | None = None,
    since_datetime: str | None = None,
    hunks: dict[str, list[list[int]]] | None = None,
    timings: list | None = None,
) -> list[Decision]:
    """Stages 1–2 for one diff: analyze, read transcripts since the cutoff, extract,
    dedup against the existing log, synthesize questions. Writes nothing.

    Shared by review mode (pre-commit, staged diff) and record mode (post-commit,
    landed commit's diff). ``since_commit`` / ``since_datetime`` default to the
    config's cutoffs when ``None``; ``hunks`` defaults to the staged hunks. If
    ``timings`` is given, ``(label, seconds)`` is appended per stage."""
    repo_root = Path(repo_root)
    repo = Repo(repo_root)

    def _timed(label):
        return _StageTimer(timings, label)

    # Existing decisions (flag unreachable commit refs)
    with _timed("Check broken refs"):
        existing_decisions = read_all_decisions(repo_root)
        existing_decisions = _check_broken_refs(repo, existing_decisions)

    # Validate API access before any LLM work
    with _timed("Validate API"):
        from plumb.programs import validate_api_access
        validate_api_access()

    # Analyze diff
    with _timed("Analyze diff"):
        diff_summary = _analyze_diff(diff)

    # Extract decisions from conversation (or diff-only fallback)
    with _timed("Extract decisions"):
        decisions = _extract_decisions_from_conversation(
            repo_root,
            config,
            diff_summary,
            since_commit=since_commit,
            since_datetime=since_datetime,
            hunks=hunks,
        )
        if not decisions:
            decisions = _extract_decisions_from_diff(diff_summary, branch)

    # Merge/dedup (also filter against already-resolved decisions)
    with _timed("Dedup"):
        decisions = deduplicate_decisions(
            decisions, existing_decisions=existing_decisions, use_llm=True
        )

    # Synthesize questions for questionless decisions
    with _timed("Synthesize questions"):
        decisions = _synthesize_questions(decisions)

    return decisions


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
    timings: list[tuple[str, float]] = []

    def _timed(label):
        return _StageTimer(timings, label)

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
            delete_decisions_by_commit(repo_root, config.last_commit, branch=branch)

    # 4. Analyze, extract, dedup, synthesize (shared with record mode)
    conv_decisions = extract_decisions(
        repo_root, config, diff, branch, timings=timings
    )

    # 5. Write decisions (unless dry_run)
    with _timed("Write decisions"):
        if not dry_run and conv_decisions:
            append_decisions(repo_root, conv_decisions, branch=branch)
            config.last_extracted_at = datetime.now(timezone.utc).isoformat()
            save_config(repo_root, config)

    # 6. Check pending decisions
    with _timed("Check pending"):
        if dry_run:
            _print_timings()
            if conv_decisions:
                print(_format_tty_output(conv_decisions))
            else:
                print("No decisions detected in staged changes.")
            return 0

        all_decisions = read_all_decisions(repo_root)
        pending = [d for d in all_decisions if d.status == "pending"]

    if pending:
        _print_timings()
        is_tty = sys.stdout.isatty()
        if is_tty:
            print(_format_tty_output(pending))
        else:
            print(_format_json_output(pending))
        return 1

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
        # Resolve the previous cutoff before overwriting it: decisions created
        # after it (by this commit's pre-commit pass) belong to the new HEAD.
        from plumb.traces.repo import commit_datetime
        prev_dt = commit_datetime(repo_root, config.last_commit) if config.last_commit else None

        new_sha = str(repo.head.commit)
        branch = _get_branch_name(repo)
        config.last_commit = new_sha
        config.last_commit_branch = branch
        config.last_extracted_at = None
        save_config(repo_root, config)

        if prev_dt is not None:
            _stamp_commit_sha(repo_root, new_sha, branch, prev_dt)
    except Exception:
        pass


def _stamp_commit_sha(repo_root: Path, new_sha: str, branch: str, prev_dt) -> int:
    """Set commit_sha on uncommitted decisions created since the previous
    commit on this branch. Returns the number stamped."""
    from plumb.decision_log import find_decision_branch, read_all_decisions, update_decision_status
    from plumb.traces.repo import parse_ts

    stamped = 0
    for d in read_all_decisions(repo_root):
        if d.commit_sha is not None or d.status == "ignored":
            continue
        if d.branch and d.branch != branch:
            continue
        created = parse_ts(d.created_at)
        if created is None or created < prev_dt:
            continue
        shard = find_decision_branch(repo_root, d.id)
        if shard is None:
            continue
        update_decision_status(repo_root, d.id, branch=shard, commit_sha=new_sha)
        stamped += 1
    return stamped
