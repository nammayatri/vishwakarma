"""
Runbook storage — DB-backed runbooks + alert→runbook mapping.

Moves runbooks out of the image so authoring needs no deploy (Slack/UI/API
paths all write here). The repo's plugins/runbooks/*.md + agents.json seed
the table on first boot, so existing behavior carries over and OSS users
get working defaults.

Two tables (see db.py schema):
  runbooks          — content + metadata (cloud_type, keywords, services,
                      status, hit/miss counters)
  alert_runbook_map — normalized-alert-key → runbook ids (explicit fast path;
                      self-populates from confirmed matches)

Self-curation: hit/miss counters from the ✅/❌ feedback loop; a runbook
whose misses dominate gets status='demoted' and drops out of recall.
"""
import json
import logging
import re
import time
from pathlib import Path

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)

DEMOTE_MIN_MISSES = 3        # need at least this many misses to demote
DEMOTE_MISS_RATIO = 0.7      # demote when misses/(hits+misses) >= this

# Words stripped from alert names during normalization — env/severity noise
# that varies between otherwise-identical alerts.
_NOISE_WORDS = ("production", "prod", "staging", "stage", "dev", "critical",
                "warning", "high", "low", "alert")


def normalize_alert_key(alert_name: str) -> str:
    """
    'RDS-CPU-Production-High' → 'rdscpu'. Stable across naming variants so
    one map row catches CloudWatch/Prometheus/GCP spellings.
    """
    s = re.sub(r"[^a-z0-9]+", " ", (alert_name or "").lower())
    parts = [p for p in s.split() if p not in _NOISE_WORDS]
    return "".join(parts)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def save_runbook(
    runbook_id: str,
    title: str,
    content_md: str,
    cloud_type: str = "any",
    keywords: list[str] | None = None,
    services: list[str] | None = None,
    author: str = "",
    status: str = "active",
) -> str:
    now = time.time()
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO runbooks
              (id, title, content_md, cloud_type, keywords, services, author,
               version, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title      = excluded.title,
              content_md = excluded.content_md,
              cloud_type = excluded.cloud_type,
              keywords   = excluded.keywords,
              services   = excluded.services,
              version    = runbooks.version + 1,
              updated_at = excluded.updated_at
            """,
            (runbook_id, title, content_md, cloud_type,
             json.dumps([k.lower() for k in (keywords or [])]),
             json.dumps(services or []), author, status, now, now),
        )
        conn.commit()
    return runbook_id


def get_runbook(runbook_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM runbooks WHERE id = ?", (runbook_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_runbooks(status: str | None = "active", cloud: str | None = None) -> list[dict]:
    """List runbooks, optionally filtered by status and cloud eligibility."""
    conn = _get_conn()
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if cloud:
        where.append("cloud_type IN (?, 'both', 'any')")
        params.append(cloud)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM runbooks {clause} ORDER BY hit_count DESC, updated_at DESC",
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_runbook(runbook_id: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM alert_runbook_map WHERE runbook_id = ?", (runbook_id,))
        conn.execute("DELETE FROM runbooks WHERE id = ?", (runbook_id,))
        conn.commit()


# ── Alert map ─────────────────────────────────────────────────────────────────

def map_alert(alert_name: str, runbook_id: str, priority: int = 100) -> None:
    """Upsert an explicit alert→runbook row (also called on ✅-confirmed matches)."""
    key = normalize_alert_key(alert_name)
    if not key:
        return
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO alert_runbook_map (alert_pattern, runbook_id, priority)
            VALUES (?, ?, ?)
            ON CONFLICT(alert_pattern, runbook_id) DO UPDATE SET
              priority = excluded.priority
            """,
            (key, runbook_id, priority),
        )
        conn.commit()


def mapped_runbook_ids(alert_name: str) -> list[str]:
    """Explicit map hits for a normalized alert key, priority order."""
    key = normalize_alert_key(alert_name)
    if not key:
        return []
    conn = _get_conn()
    rows = conn.execute(
        "SELECT runbook_id FROM alert_runbook_map WHERE alert_pattern = ? ORDER BY priority",
        (key,),
    ).fetchall()
    return [r[0] for r in rows]


# ── Feedback / self-curation ──────────────────────────────────────────────────

def mark_runbook_hit(runbook_id: str, alert_name: str = "") -> None:
    """✅-confirmed RCA used this runbook — strengthen it + self-populate the map."""
    now = time.time()
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE runbooks SET hit_count = hit_count + 1, updated_at = ? WHERE id = ?",
            (now, runbook_id),
        )
        conn.commit()
    if alert_name:
        map_alert(alert_name, runbook_id)


def mark_runbook_miss(runbook_id: str) -> None:
    """❌-rejected RCA — weaken; demote when misses dominate."""
    now = time.time()
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE runbooks SET miss_count = miss_count + 1, updated_at = ? WHERE id = ?",
            (now, runbook_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT hit_count, miss_count FROM runbooks WHERE id = ?", (runbook_id,)
        ).fetchone()
        if row:
            hits, misses = int(row[0]), int(row[1])
            total = hits + misses
            if misses >= DEMOTE_MIN_MISSES and total and misses / total >= DEMOTE_MISS_RATIO:
                conn.execute(
                    "UPDATE runbooks SET status = 'demoted', updated_at = ? WHERE id = ?",
                    (now, runbook_id),
                )
                conn.commit()
                log.warning(f"Runbook '{runbook_id}' demoted ({misses}/{total} misses)")


# ── Seeding from repo files ───────────────────────────────────────────────────

def seed_from_files(agents_json_path: str | Path | None = None) -> int:
    """
    Import plugins/agents/agents.json entries + their runbook .md files into
    the tables. Idempotent: existing rows are updated, versions bump only when
    re-seeded. Keeps file-based runbooks working as OSS defaults.
    Returns number of runbooks seeded.
    """
    if agents_json_path is None:
        agents_json_path = (
            Path(__file__).parent.parent / "plugins" / "agents" / "agents.json"
        )
    agents_json_path = Path(agents_json_path)
    if not agents_json_path.exists():
        log.info("seed_from_files: no agents.json found — skipping")
        return 0

    try:
        agents = json.loads(agents_json_path.read_text()).get("agents", [])
    except Exception as e:
        log.warning(f"seed_from_files: cannot parse agents.json: {e}")
        return 0

    n = 0
    for entry in agents:
        rb_ref = entry.get("runbook", "")
        rb_id = entry.get("id") or normalize_alert_key(entry.get("description", ""))
        if not rb_ref or not rb_id:
            continue
        rb_path = (agents_json_path.parent / rb_ref).resolve()
        if not rb_path.exists():
            log.warning(f"seed_from_files: missing runbook file for '{rb_id}': {rb_path}")
            continue
        try:
            content = rb_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            log.warning(f"seed_from_files: cannot read {rb_path}: {e}")
            continue
        save_runbook(
            runbook_id=rb_id,
            title=entry.get("description", rb_id)[:200],
            content_md=content,
            cloud_type=entry.get("cloud_type", "any"),
            keywords=entry.get("keywords", []),
            author="seed:agents.json",
        )
        n += 1
    log.info(f"Seeded {n} runbooks from {agents_json_path}")
    return n


# ── Internal ──────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    d = dict(row)
    for field in ("keywords", "services"):
        if isinstance(d.get(field), str):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                d[field] = []
    return d
