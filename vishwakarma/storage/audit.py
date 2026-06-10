"""
Audit log — who changed what, when. Backs the SECURITY.md promise that
runbook edits and fix approvals are attributable. Never raises.
"""
import json
import logging
import time

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)


def audit(actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
    try:
        conn = _get_conn()
        with _lock:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                (time.time(), actor or "?", action, target, json.dumps(detail or {})),
            )
            conn.commit()
    except Exception as e:
        log.debug(f"Audit write failed: {e}")


def list_audit(limit: int = 100) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT ts, actor, action, target, detail FROM audit_log ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("detail"), str) and d["detail"]:
            try:
                d["detail"] = json.loads(d["detail"])
            except Exception:
                pass
        out.append(d)
    return out
