"""plumb search: DuckDB enumerates and filters the decision log; BM25 ranks.

One DuckDB query reads every ``.plumb/decisions/*.jsonl`` shard with
latest-line-wins dedup (the same shape as ``read_all_decisions``) and applies
every filter, sort, and limit in SQL. Column types are declared explicitly
from ``Decision.model_fields`` so older shards that lack the provenance
columns simply yield typed NULLs. Relevance is an in-process BM25 over
``question + decision + user_note`` computed across every row passing the
non-text filters (so idf reflects the whole corpus); DuckDB's ``fts``
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

# Explicit DuckDB types for read_json: no inference, so an all-null or absent
# column never comes back as JSON, and every shard shares one schema.
_COLUMN_TYPES: dict[str, str] = {c: "VARCHAR" for c in Decision.model_fields}
_COLUMN_TYPES.update({
    "file_refs": "STRUCT(file VARCHAR, lines BIGINT[])[]",
    "related_requirement_ids": "VARCHAR[]",
    "turn_range": "BIGINT[]",
    "confidence": "DOUBLE",
    "chunk_index": "BIGINT",
    "conversation_available": "BOOLEAN",
    "conversation_truncated": "BOOLEAN",
})
_COLUMNS_SQL = "{" + ", ".join(f"'{k}': '{v}'" for k, v in _COLUMN_TYPES.items()) + "}"

# Text BM25 scores.
_TEXT_COLS = ("question", "decision", "user_note")

_CREATED_TS = "try_cast(created_at AS TIMESTAMP)"
_ORDER_BY = {
    "date": f"{_CREATED_TS} DESC NULLS LAST, id",
    "confidence": f"confidence DESC NULLS LAST, {_CREATED_TS} DESC NULLS LAST, id",
}


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


def _latest_cte(decisions_dir: Path) -> str:
    # read_json cannot take its path as a parameter; escape it instead.
    glob = str(decisions_dir / "*.jsonl").replace("'", "''")
    return f"""
        WITH raw AS (SELECT *, ROW_NUMBER() OVER () AS _n
                     FROM read_json('{glob}', format='newline_delimited', columns={_COLUMNS_SQL})),
             latest AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY _n DESC) AS _rn FROM raw)
    """


def _row_to_decision(cols: list[str], row: tuple) -> Decision:
    cleaned = _clean_duckdb_row(dict(zip(cols, row)))
    # A column absent from an older shard arrives as NULL; let the model's
    # defaults stand in (file_refs -> [], ref_status -> "ok", ...).
    return Decision(**{k: v for k, v in cleaned.items() if v is not None})


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

    *sort* is ``relevance`` (default when *query* is given; falls back to
    ``date`` without one), ``date`` (newest first; default otherwise), or
    ``confidence`` (desc, nulls last). Without an explicit *status*,
    ``ignored`` and ``rejected*`` rows are hidden. ``limit`` of 0/None = all.
    """
    import duckdb

    repo_root = Path(repo_root)
    d = _decisions_dir(repo_root)
    if not d.exists() or not list(d.glob("*.jsonl")):
        return []
    since_dt = _resolve_since(repo_root, since)

    where: list[str] = ["_rn = 1"]
    params: list = []

    def col_in(col: str, values: list[str]) -> None:
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
        where.append("len(list_filter(file_refs, r -> r.file = ?)) > 0")
        params.append(file)
    if since_dt is not None:
        where.append(f"{_CREATED_TS} >= ?")
        params.append(since_dt.astimezone(timezone.utc).replace(tzinfo=None))

    cte = _latest_cte(d)
    cols = list(Decision.model_fields)
    select_full = f"{cte} SELECT {', '.join(cols)} FROM latest WHERE {' AND '.join(where)}"

    sort = sort or ("relevance" if query else "date")
    if sort == "relevance" and not query:
        sort = "date"

    con = duckdb.connect(":memory:")
    try:
        # ISO strings with an offset cast to UTC-naive TIMESTAMP; keep the
        # session in UTC so --since compares apples to apples.
        con.execute("SET TimeZone = 'UTC'")
        if sort == "relevance":
            # Score every row passing the non-text filters so idf sees the
            # whole corpus, then fetch full records only for the survivors.
            light = con.execute(
                f"{cte} SELECT id, {', '.join(_TEXT_COLS)}, created_at FROM latest WHERE {' AND '.join(where)}",
                params,
            ).fetchall()
            scores = bm25_scores([" ".join(c or "" for c in r[1:-1]) for r in light], query)
            ranked = sorted(
                ((s, r[-1] or "", r[0]) for r, s in zip(light, scores) if s > 0),
                reverse=True,
            )
            if limit:
                ranked = ranked[:limit]
            if not ranked:
                return []
            ids = [i for _, _, i in ranked]
            rel = con.execute(
                f"{cte} SELECT {', '.join(cols)} FROM latest WHERE _rn = 1 AND id = ANY(?)",
                [ids],
            )
            by_id = {x.id: x for x in (_row_to_decision(cols, r) for r in rel.fetchall())}
            return [Hit(by_id[i], s) for s, _, i in ranked]

        sql = f"{select_full} ORDER BY {_ORDER_BY[sort]}"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rel = con.execute(sql, params)
        return [Hit(_row_to_decision(cols, r), 0.0) for r in rel.fetchall()]
    finally:
        con.close()
