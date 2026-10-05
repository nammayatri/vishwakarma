import json
import logging
import time
import uuid

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)

_COLUMNS = (
    "id", "incident_id", "alert_title", "platform", "channel", "thread_ts", "message_ts",
    "mirror_channel", "mirror_thread_ts", "mirror_message_ts", "namespace", "service", "pod",
    "command", "queries_ran", "evidence", "is_approved", "status", "approved_by", "approved_via",
    "approved_at", "executed_at", "output", "created_at",
)


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    if isinstance(d.get("queries_ran"), str) and d["queries_ran"]:
        try:
            d["queries_ran"] = json.loads(d["queries_ran"])
        except Exception:
            pass
    return d


def create_action(*, incident_id: str, alert_title: str, platform: str, channel: str,
                  thread_ts: str, namespace: str, service: str, pod: str, command: str,
                  queries_ran: list[str], evidence: str) -> str:
    action_id = uuid.uuid4().hex[:16]
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO remediation_actions (id, incident_id, alert_title, platform, channel, "
            "thread_ts, namespace, service, pod, command, queries_ran, evidence, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (action_id, incident_id, alert_title, platform, channel, thread_ts, namespace,
             service, pod, command, json.dumps(queries_ran), evidence, "pending", time.time()),
        )
        conn.commit()
    return action_id


def get_action(action_id: str) -> dict | None:
    conn = _get_conn()
    return _row(conn.execute("SELECT * FROM remediation_actions WHERE id = ?", (action_id,)).fetchone())


def set_message_refs(action_id: str, *, message_ts: str = "", channel: str = "",
                     mirror_channel: str = "", mirror_thread_ts: str = "",
                     mirror_message_ts: str = "") -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE remediation_actions SET message_ts = ?, channel = CASE WHEN ? != '' THEN ? ELSE channel END, "
            "mirror_channel = ?, mirror_thread_ts = ?, mirror_message_ts = ? WHERE id = ?",
            (message_ts, channel, channel, mirror_channel, mirror_thread_ts, mirror_message_ts, action_id),
        )
        conn.commit()


def claim_decision(action_id: str, *, approved: bool, approver: str, via: str, ttl_seconds: int) -> str:
    """Atomically moves pending -> approved/rejected. Returns the resulting
    status, or the existing non-pending status (so a second click never
    re-executes), or 'expired' if the TTL elapsed."""
    now = time.time()
    conn = _get_conn()
    with _lock:
        row = conn.execute("SELECT status, created_at FROM remediation_actions WHERE id = ?",
                           (action_id,)).fetchone()
        if row is None:
            return "missing"
        status, created_at = row["status"], row["created_at"]
        if status != "pending":
            return status
        if now - created_at > ttl_seconds:
            conn.execute("UPDATE remediation_actions SET status = 'expired' WHERE id = ? AND status = 'pending'",
                         (action_id,))
            conn.commit()
            return "expired"
        new_status = "approved" if approved else "rejected"
        cur = conn.execute(
            "UPDATE remediation_actions SET status = ?, is_approved = ?, approved_by = ?, "
            "approved_via = ?, approved_at = ? WHERE id = ? AND status = 'pending'",
            (new_status, 1 if approved else 0, approver, via, now, action_id),
        )
        conn.commit()
        return new_status if cur.rowcount == 1 else "raced"


def record_result(action_id: str, *, ok: bool, output: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE remediation_actions SET status = ?, executed_at = ?, output = ? WHERE id = ?",
            ("executed" if ok else "failed", time.time(), output[:4000], action_id),
        )
        conn.commit()


def latest_pending(channel: str, ttl_seconds: int) -> dict | None:
    conn = _get_conn()
    cutoff = time.time() - ttl_seconds
    rows = conn.execute(
        "SELECT * FROM remediation_actions WHERE status = 'pending' AND created_at >= ? "
        "AND (channel = ? OR mirror_channel = ?) ORDER BY created_at DESC LIMIT 2",
        (cutoff, channel, channel),
    ).fetchall()
    return _row(rows[0]) if len(rows) == 1 else None


def list_actions(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM remediation_actions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def only_pending(ttl_seconds: int) -> dict | None:
    conn = _get_conn()
    cutoff = time.time() - ttl_seconds
    rows = conn.execute(
        "SELECT * FROM remediation_actions WHERE status = 'pending' AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT 2", (cutoff,),
    ).fetchall()
    return _row(rows[0]) if len(rows) == 1 else None
