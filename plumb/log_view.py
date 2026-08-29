"""Grouping and evidence verification behind `plumb log`."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from plumb.decision_log import Decision

UNCOMMITTED = "uncommitted"
UNKNOWN_AGENT = "unknown"


def group_decisions(decisions: list[Decision]) -> "OrderedDict[str, OrderedDict[str, list[Decision]]]":
    """{commit_sha | 'uncommitted': {agent | 'unknown': [decisions]}}.

    Uncommitted first, then commits by newest created_at descending; agents
    sorted by name; decisions in input order.
    """
    by_commit: dict[str, list[Decision]] = {}
    for d in decisions:
        by_commit.setdefault(d.commit_sha or UNCOMMITTED, []).append(d)
    commits = [k for k in by_commit if k != UNCOMMITTED]
    commits.sort(key=lambda k: max((x.created_at or "") for x in by_commit[k]), reverse=True)
    ordered: "OrderedDict[str, OrderedDict[str, list[Decision]]]" = OrderedDict()
    for k in ([UNCOMMITTED] if UNCOMMITTED in by_commit else []) + commits:
        agents: dict[str, list[Decision]] = {}
        for d in by_commit[k]:
            agents.setdefault(d.agent or UNKNOWN_AGENT, []).append(d)
        ordered[k] = OrderedDict(sorted(agents.items()))
    return ordered


def _source_for(agent: str):
    from plumb.traces import all_sources
    return next((s for s in all_sources() if s.name == agent), None)


def verify_evidence(d: Decision) -> str:
    """'ok' | 'stale' | 'missing' | 'unverifiable'.

    Re-parses the decision's transcript with no cutoff, applies the same
    per-turn transforms the hook applied (prepare_for_digest), and recomputes
    the digest over turn_start..turn_end. 'stale' means the transcript changed
    or the range is gone; 'missing' means the transcript file no longer exists;
    'unverifiable' means we cannot even attempt it (no provenance, unknown
    agent, or the transcript failed to parse).
    """
    if not (d.agent and d.session_id and d.source_path and d.turn_range and d.evidence_digest):
        return "unverifiable"
    if not Path(d.source_path).is_file():
        return "missing"
    source = _source_for(d.agent)
    if source is None:
        return "unverifiable"
    from plumb.conversation import evidence_digest_for, prepare_for_digest
    from plumb.traces import SessionRef
    # parse() reads ref.path only; cwd is used by discover(), which we skip.
    ref = SessionRef(agent=d.agent, session_id=d.session_id, path=d.source_path, cwd="")
    try:
        turns = prepare_for_digest(source.parse(ref, None))
    except Exception:
        return "unverifiable"
    start, end = d.turn_range[0], d.turn_range[1]
    if not any(start <= t.ordinal <= end for t in turns):
        return "stale"
    return "ok" if evidence_digest_for(turns, start, end) == d.evidence_digest else "stale"
