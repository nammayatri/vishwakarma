"""
Learned tool routing — which toolsets actually solved each alert class.

On a ✅-confirmed RCA, the toolsets that produced its evidence are credited
for that alert class. Tool selection then biases the curated subset toward
historically-useful toolsets — so a class of alert that always needed the
database toolset keeps getting it even if the keyword rules wouldn't.
"""
import logging
import time

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)


def record_effective(alert_key: str, toolsets: set[str] | list[str]) -> None:
    if not alert_key or not toolsets:
        return
    now = time.time()
    conn = _get_conn()
    with _lock:
        for ts in set(toolsets):
            conn.execute(
                """
                INSERT INTO tool_effectiveness (alert_key, toolset, hits, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(alert_key, toolset) DO UPDATE SET
                  hits = tool_effectiveness.hits + 1, updated_at = excluded.updated_at
                """,
                (alert_key, ts, now),
            )
        conn.commit()


def top_toolsets(alert_key: str, min_hits: int = 2, limit: int = 6) -> set[str]:
    """Toolsets that have repeatedly helped this alert class."""
    if not alert_key:
        return set()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT toolset FROM tool_effectiveness WHERE alert_key = ? AND hits >= ? "
        "ORDER BY hits DESC LIMIT ?",
        (alert_key, min_hits, limit),
    ).fetchall()
    return {r[0] for r in rows}


def tools_to_toolsets(tool_names: set[str], toolsets: list) -> set[str]:
    """Map executed tool names back to their owning toolset names."""
    out: set[str] = set()
    for ts in toolsets:
        try:
            ts_tools = {t.name for t in ts.get_tools()}
        except Exception:
            continue
        if tool_names & ts_tools:
            out.add(ts.name)
    return out
