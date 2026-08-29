"""plumb search: DuckDB enumerates and filters the decision log; BM25 ranks.

One DuckDB query reads every ``.plumb/decisions/*.jsonl`` shard with
latest-line-wins dedup (the same shape as ``read_all_decisions``) and applies
every filter in SQL. Relevance is an in-process BM25 over
``question + decision + user_note`` on the filtered rows; DuckDB's ``fts``
extension is a network download the wheel does not bundle, so v1 does not
depend on it. The ranking is isolated in ``bm25_scores`` so swapping it is
local.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from plumb.decision_log import Decision, _clean_duckdb_row, _decisions_dir
from plumb.traces.repo import commit_datetime, parse_ts

_TOKEN = re.compile(r"[a-z0-9]+")

# Statuses hidden unless --status names them explicitly.
_HIDDEN_BY_DEFAULT_SQL = "status <> 'ignored' AND status NOT LIKE 'rejected%'"

_FILE_REFS_TYPE = "STRUCT(file VARCHAR, lines BIGINT[])[]"

# Text BM25 scores and the query prefilter look at.
_TEXT_COLS = ("question", "decision", "user_note")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def bm25_scores(docs: list[str], query: str, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Okapi BM25 score of *query* against each doc (0.0 when nothing matches)."""
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
    """ISO date/datetime or git ref -> aware datetime. Raises ValueError when
    *since* is neither."""
    if not since:
        return None
    dt = parse_ts(since)
    if dt is None:
        dt = commit_datetime(repo_root, since)
    if dt is None:
        raise ValueError(f"cannot resolve '{since}' as a date or git ref")
    return dt


def _text_expr(present: set[str]) -> str:
    parts = [f"coalesce(try_cast({c} AS VARCHAR), '')" for c in _TEXT_COLS if c in present]
    return "lower(" + " || ' ' || ".join(parts) + ")" if parts else "''"


def search_decisions(
    repo_root,
    query: str = "",
    *,
    sort: Optional[str] = None,
    status: Optional[list[str]] = None,
    agent: Optional[list[str]] = None,
    branch: Optional[str] = None,
    file: Optional[str] = None,
    made_by: Optional[str] = None,
    since: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[Hit]:
    """Enumerate, filter, and rank decisions across every branch shard.

    *sort* is ``relevance`` (default when *query* is given), ``date`` (newest
    first; default otherwise), or ``confidence`` (desc, nulls last). Without
    an explicit *status*, ``ignored`` and ``rejected*`` rows are hidden.
    """
    import duckdb

    repo_root = Path(repo_root)
    d = _decisions_dir(repo_root)
    if not d.exists() or not list(d.glob("*.jsonl")):
        return []
    since_dt = _resolve_since(repo_root, since)

    # read_json_auto cannot take its path as a parameter; escape it instead.
    glob = str(d / "*.jsonl").replace("'", "''")
    source = f"read_json_auto('{glob}', format='newline_delimited', union_by_name=true)"

    con = duckdb.connect(":memory:")
    try:
        # ISO strings with an offset infer as UTC-naive TIMESTAMP; keep every
        # timestamp cast in UTC so --since compares apples to apples.
        con.execute("SET TimeZone = 'UTC'")
        # Older shards lack the provenance columns (agent, session_id, ...);
        # only reference what this log actually has.
        present = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}

        where: list[str] = []
        params: list = []

        def col_in(col: str, values: list[str]) -> None:
            if col not in present:
                where.append("FALSE")
                return
            where.append(f"{col} IN ({','.join('?' * len(values))})")
            params.extend(values)

        if status:
            col_in("status", list(status))
        else:
            where.append(_HIDDEN_BY_DEFAULT_SQL)
        if agent:
            col_in("agent", list(agent))
        if branch:
            col_in("branch", [branch])
        if made_by:
            col_in("made_by", [made_by])
        if file:
            if "file_refs" in present:
                where.append(
                    f"len(list_filter(try_cast(file_refs AS {_FILE_REFS_TYPE}), r -> r.file = ?)) > 0"
                )
                params.append(file)
            else:
                where.append("FALSE")
        if since_dt is not None:
            if "created_at" in present:
                where.append("try_cast(created_at AS TIMESTAMP) >= ?")
                params.append(since_dt.astimezone(timezone.utc).replace(tzinfo=None))
            else:
                where.append("FALSE")
        if query:
            # Cheap prefilter: any query token appears somewhere in the text;
            # BM25 then orders, so partial matches rank instead of vanishing.
            toks = tokenize(query)
            text = _text_expr(present)
            if toks:
                where.append("(" + " OR ".join(f"regexp_matches({text}, ?)" for _ in toks) + ")")
                params.extend(re.escape(t) for t in toks)

        select_cols = [c for c in Decision.model_fields if c in present]
        select_list = ", ".join(
            f"try_cast(file_refs AS {_FILE_REFS_TYPE}) AS file_refs" if c == "file_refs" else c
            for c in select_cols
        )
        sql = f"""
            WITH raw AS (SELECT *, ROW_NUMBER() OVER () AS _n FROM {source}),
                 latest AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY _n DESC) AS _rn FROM raw)
            SELECT {select_list} FROM latest WHERE _rn = 1 AND {' AND '.join(where)}
        """
        rel = con.execute(sql, params)
        cols = [c[0] for c in rel.description]
        rows = [Decision(**_clean_duckdb_row(dict(zip(cols, r)))) for r in rel.fetchall()]
    finally:
        con.close()

    sort = sort or ("relevance" if query else "date")
    if sort == "relevance" and query:
        scores = bm25_scores(
            [" ".join(getattr(x, c) or "" for c in _TEXT_COLS) for x in rows], query
        )
        hits = sorted(
            (Hit(x, s) for x, s in zip(rows, scores)),
            key=lambda h: (h.score, h.decision.created_at or ""),
            reverse=True,
        )
    elif sort == "confidence":
        hits = sorted(
            (Hit(x, 0.0) for x in rows),
            key=lambda h: (
                h.decision.confidence is None,
                -(h.decision.confidence or 0.0),
                h.decision.created_at or "",
            ),
        )
    else:
        hits = sorted((Hit(x, 0.0) for x in rows), key=lambda h: h.decision.created_at or "", reverse=True)
    return hits[:limit] if limit else hits
