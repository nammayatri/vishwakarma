"""
SQLite → Postgres one-shot migration.

Copies incident history, oracle sessions, evidence snapshots, learned
baselines, and RCA patterns into the shared control-plane Postgres so the
learning loops (incident-RAG seeds, pattern replay, baselines) keep their
memory. Idempotent: upserts by primary key, safe to re-run.

dedup_state is intentionally NOT migrated (ephemeral, Redis owns it now).

Usage:  vk migrate-db --from /data/vishwakarma.db --to postgresql://...
"""
import json
import logging
import sqlite3

log = logging.getLogger(__name__)

# table -> (columns, conflict_key)
_TABLES: dict[str, tuple[list[str], str]] = {
    "incidents": (
        ["id", "title", "source", "severity", "status", "question", "analysis",
         "tool_outputs", "meta", "created_at", "updated_at", "resolved_at",
         "labels", "slack_ts", "pdf_path"],
        "id",
    ),
    "oracle_sessions": (
        ["id", "title", "messages", "created_at", "updated_at"],
        "id",
    ),
    "evidence_snapshots": (
        ["id", "alert_name", "scenario", "root_cause_type", "metrics",
         "outcome", "incident_id", "created_at"],
        "id",
    ),
    "learned_baselines": (
        ["alert_name", "metric_name", "mean", "stddev", "min_val", "max_val",
         "sample_count", "last_updated"],
        "alert_name, metric_name",
    ),
    "rca_patterns": (
        ["id", "alert_name", "root_cause_type", "root_cause_detail",
         "investigation_steps", "verification_keywords", "verification_anti_keywords",
         "fix", "confidence", "hit_count", "miss_count", "first_seen", "last_seen",
         "last_incident_id", "status"],
        "id",
    ),
}


def migrate_sqlite_to_postgres(sqlite_path: str, pg_dsn: str) -> dict[str, int]:
    """Copy all supported tables. Returns {table: rows_migrated}."""
    from vishwakarma.storage.db import init_db, _get_conn, _lock, get_backend

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    # Initialize target schema on Postgres
    init_db(dsn=pg_dsn)
    assert get_backend() == "postgres", "target must be postgres"
    dst = _get_conn()

    counts: dict[str, int] = {}
    for table, (cols, conflict_key) in _TABLES.items():
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError as e:
            log.warning(f"Skipping {table}: {e}")
            counts[table] = 0
            continue

        placeholders = ", ".join(["?"] * len(cols))
        col_list = ", ".join(cols)
        update_cols = [c for c in cols if c not in conflict_key.replace(" ", "").split(",")]
        update_set = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_key}) DO UPDATE SET {update_set}"
        )

        n = 0
        with _lock:
            for row in rows:
                d = dict(row)
                params = [d.get(c) for c in cols]
                dst.execute(sql, params)
                n += 1
            dst.commit()
        counts[table] = n
        log.info(f"Migrated {n} rows: {table}")

    src.close()
    return counts
