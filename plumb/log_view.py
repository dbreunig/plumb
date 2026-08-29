"""Grouping and evidence verification behind `plumb log`."""
from __future__ import annotations

from collections import OrderedDict

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


def _as_hook_saw(turns, max_tokens: int = 6000):
    """Apply exactly the transforms the hook applies between parse() and the
    digest: reduce_noise, ordinal sort, and chunk_conversation's per-turn
    truncation of any single turn over the token budget. No re-chunking: the
    truncation rule is per-turn, so it can be reproduced without grouping."""
    from plumb.conversation import estimate_tokens, reduce_noise, render_turn

    out = []
    for t in sorted(reduce_noise(turns), key=lambda t: t.ordinal):
        if estimate_tokens(render_turn(t)) > max_tokens:
            t = t.model_copy(update={"content": t.content[: max_tokens * 4]})
        out.append(t)
    return out


def verify_evidence(d: Decision) -> str:
    """'ok' | 'stale' | 'unverifiable'.

    Re-parses the decision's transcript with no cutoff, applies the same
    transforms the hook applied, and recomputes the digest over
    turn_start..turn_end. 'stale' means the transcript changed or the range is
    gone; 'unverifiable' means we cannot even attempt it.
    """
    if not (d.agent and d.session_id and d.source_path and d.turn_range and d.evidence_digest):
        return "unverifiable"
    source = _source_for(d.agent)
    if source is None:
        return "unverifiable"
    from plumb.conversation import evidence_digest_for
    from plumb.traces import SessionRef
    ref = SessionRef(agent=d.agent, session_id=d.session_id, path=d.source_path, cwd="")
    try:
        turns = _as_hook_saw(source.parse(ref, None))
    except Exception:
        return "unverifiable"
    start, end = d.turn_range[0], d.turn_range[1]
    if not any(start <= t.ordinal <= end for t in turns):
        return "stale"
    return "ok" if evidence_digest_for(turns, start, end) == d.evidence_digest else "stale"
