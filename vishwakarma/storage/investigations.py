"""
Durable investigation jobs — checkpoint/resume state machine.

Every investigation is a durable, resumable row (id = incident_id), not a
fire-and-forget coroutine. The executor checkpoints `messages` + `phase` +
`step` at each step boundary; on crash, another worker claims the job and
resumes from the last checkpoint instead of restarting.

Status flow: queued → running → (awaiting_fix_review) → done | failed
Backend-agnostic: works on SQLite and Postgres via storage.db.
"""
import json
import logging
import time

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # after this many claims, mark failed instead of retrying

VALID_STATUSES = {"queued", "running", "awaiting_fix_review", "done", "failed",
                  "aborting", "aborted"}


def create_investigation(
    incident_id: str,
    alert_key: str = "",
    cloud: str = "",
) -> str:
    """Create (or return existing) investigation row. Idempotent by id."""
    now = time.time()
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO investigations (id, alert_key, cloud, status, step, attempt, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', 0, 0, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (incident_id, alert_key, cloud, now, now),
        )
        conn.commit()
    return incident_id


def claim_investigation(incident_id: str, worker_id: str) -> dict | None:
    """
    Claim a queued/orphaned investigation for this worker.

    Returns the row (including any checkpoint to resume from), or None if
    it's already done/failed or the attempt budget is exhausted.
    Increments `attempt` on every claim.
    """
    now = time.time()
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM investigations WHERE id = ?", (incident_id,)
        ).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        if d["status"] in ("done", "failed"):
            return None
        if d["attempt"] >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE investigations SET status='failed', updated_at=? WHERE id=?",
                (now, incident_id),
            )
            conn.commit()
            log.warning(f"Investigation {incident_id} exceeded {MAX_ATTEMPTS} attempts — failed")
            return None
        conn.execute(
            """
            UPDATE investigations
            SET status='running', worker_id=?, attempt=attempt+1,
                heartbeat_at=?, updated_at=?
            WHERE id=?
            """,
            (worker_id, now, now, incident_id),
        )
        conn.commit()
        d["attempt"] += 1
        d["worker_id"] = worker_id
        return d


def checkpoint_investigation(
    incident_id: str,
    messages: list | None = None,
    phase: str | None = None,
    step: int | None = None,
    findings: dict | None = None,
    code_session: dict | None = None,
) -> None:
    """Persist resumable state at a step boundary. Only provided fields update."""
    now = time.time()
    sets = ["heartbeat_at=?", "updated_at=?"]
    params: list = [now, now]
    if messages is not None:
        sets.append("messages=?")
        params.append(json.dumps(messages))
    if phase is not None:
        sets.append("phase=?")
        params.append(phase)
    if step is not None:
        sets.append("step=?")
        params.append(step)
    if findings is not None:
        sets.append("findings=?")
        params.append(json.dumps(findings))
    if code_session is not None:
        sets.append("code_session=?")
        params.append(json.dumps(code_session))
    params.append(incident_id)

    conn = _get_conn()
    with _lock:
        conn.execute(
            f"UPDATE investigations SET {', '.join(sets)} WHERE id=?", params
        )
        conn.commit()


def heartbeat(incident_id: str) -> None:
    """Cheap liveness update (between checkpoints)."""
    now = time.time()
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE investigations SET heartbeat_at=?, updated_at=? WHERE id=?",
            (now, now, incident_id),
        )
        conn.commit()


def finish_investigation(incident_id: str, status: str = "done") -> None:
    """Mark terminal state. messages are kept for the incident record/UI.
    NEVER overwrites a user 'aborted' state — aborted stays aborted forever."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status}")
    now = time.time()
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE investigations SET status=?, updated_at=? WHERE id=? AND status != 'aborted'",
            (status, now, incident_id),
        )
        conn.commit()


def get_investigation(incident_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM investigations WHERE id=?", (incident_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def find_orphaned(stale_seconds: int = 180, limit: int = 20) -> list[dict]:
    """
    Investigations whose worker stopped heartbeating — candidates for
    re-claim by another worker (the orchestrator's reaper calls this).
    """
    cutoff = time.time() - stale_seconds
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM investigations
        WHERE status='running' AND heartbeat_at < ?
        ORDER BY heartbeat_at ASC LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_active(limit: int = 50) -> list[dict]:
    """Queued + running investigations (for the UI live view / ops)."""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM investigations
        WHERE status IN ('queued','running','awaiting_fix_review')
        ORDER BY updated_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    d = dict(row)
    for field in ("messages", "findings", "code_session"):
        if isinstance(d.get(field), str) and d[field]:
            try:
                d[field] = json.loads(d[field])
            except Exception:
                pass
    return d
