"""
Phase 0 storage tests — dual-backend parity, durable investigations,
Redis dedup, and SQLite→Postgres migration.

Requires local services (skipped gracefully when absent):
  Postgres:  postgresql://postgres@localhost:5432/vk_test  (created/dropped per run)
  Redis:     redis://localhost:6379/15                     (scratch db, flushed)

Run:  pytest tests/test_storage_phase0.py -v
"""
import subprocess
import sys
import tempfile
import threading
import time

import pytest

PG_DSN = "postgresql://postgres@localhost:5432/vk_test"
REDIS_URL = "redis://localhost:6379/15"


def _pg_available() -> bool:
    try:
        import psycopg2
        psycopg2.connect("postgresql://postgres@localhost:5432/postgres").close()
        return True
    except Exception:
        return False


def _redis_available() -> bool:
    try:
        import redis
        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


needs_pg = pytest.mark.skipif(not _pg_available(), reason="local Postgres not available")
needs_redis = pytest.mark.skipif(not _redis_available(), reason="local Redis not available")


def _reset_modules():
    """Storage modules hold module-level connection globals — reload per backend."""
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]


def _fresh_pg():
    subprocess.run(
        ["psql", "-h", "localhost", "-U", "postgres",
         "-c", "DROP DATABASE IF EXISTS vk_test;", "-c", "CREATE DATABASE vk_test;"],
        capture_output=True,
    )


def _init(backend: str):
    _reset_modules()
    from vishwakarma.storage import db as dbmod
    if backend == "sqlite":
        dbmod.init_db(db_path=tempfile.mktemp(suffix=".db"))
    else:
        _fresh_pg()
        dbmod.init_db(dsn=PG_DSN)
    return dbmod


# ── Backend parity ─────────────────────────────────────────────────────────────

def _run_query_suite(backend: str) -> dict:
    _init(backend)
    from vishwakarma.storage import queries as q

    q.save_incident(incident_id="i1", title="RDS CPU", question="q",
                    analysis="missing index", source="am", severity="critical",
                    labels={"a": 1}, slack_ts="1.2")
    q.save_incident(incident_id="i1", title="RDS CPU", question="q",
                    analysis="UPDATED", source="am", severity="critical")
    q.save_incident(incident_id="i2", title="Redis evict", question="q2",
                    analysis="maxmemory", source="cw", severity="high")
    fp = q.alert_fingerprint({"alertname": "X"})
    q.set_dedup(fp, "i1", 60)
    q.set_dedup(fp, "i2", 60)
    q.save_oracle_session("s1", [{"role": "user", "content": "x"}])

    inc = q.get_incident("i1")
    return {
        "analysis": inc["analysis"],
        "labels": inc["labels"],
        "slack_ts": inc["slack_ts"],
        "count": len(q.list_incidents()),
        "search": len(q.search_incidents("UPDATED")),
        "dedup": q.check_dedup(fp),
        "oracle": len(q.load_oracle_session("s1")),
        "stats_total": q.get_stats()["total"],
    }


@needs_pg
def test_backend_parity():
    assert _run_query_suite("sqlite") == _run_query_suite("postgres")


# ── Durable investigations ─────────────────────────────────────────────────────

@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=needs_pg)])
def test_investigation_lifecycle(backend):
    _init(backend)
    from vishwakarma.storage import investigations as inv
    from vishwakarma.storage.db import _get_conn, _lock

    inv.create_investigation("inc-1", alert_key="rds", cloud="aws")
    inv.create_investigation("inc-1", alert_key="OTHER")  # idempotent no-op
    assert inv.get_investigation("inc-1")["alert_key"] == "rds"

    claimed = inv.claim_investigation("inc-1", "w1")
    assert claimed["attempt"] == 1

    msgs = [{"role": "user", "content": "alert"}]
    inv.checkpoint_investigation("inc-1", messages=msgs, step=3, phase="recon")
    row = inv.get_investigation("inc-1")
    assert row["step"] == 3 and row["messages"] == msgs

    # crash → orphan → re-claim resumes from checkpoint
    conn = _get_conn()
    with _lock:
        conn.execute("UPDATE investigations SET heartbeat_at=? WHERE id=?",
                     (time.time() - 999, "inc-1"))
        conn.commit()
    assert any(o["id"] == "inc-1" for o in inv.find_orphaned(180))
    resumed = inv.claim_investigation("inc-1", "w2")
    assert resumed["attempt"] == 2 and resumed["messages"] == msgs

    inv.finish_investigation("inc-1", "done")
    assert inv.claim_investigation("inc-1", "w3") is None  # idempotent re-delivery


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=needs_pg)])
def test_investigation_attempt_budget(backend):
    _init(backend)
    from vishwakarma.storage import investigations as inv

    inv.create_investigation("inc-2")
    for w in ("a", "b", "c"):
        assert inv.claim_investigation("inc-2", w) is not None
    assert inv.claim_investigation("inc-2", "d") is None
    assert inv.get_investigation("inc-2")["status"] == "failed"


# ── Dedup lock ─────────────────────────────────────────────────────────────────

def _race(dedup, fp: str) -> int:
    wins = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        if dedup.try_acquire(fp):
            wins.append(1)

    ts = [threading.Thread(target=worker) for _ in range(16)]
    for t in ts: t.start()
    for t in ts: t.join()
    return len(wins)


def test_dedup_inmemory_race():
    from vishwakarma.storage import dedup
    dedup.init_dedup("")
    assert _race(dedup, "mem-race") == 1
    dedup.release("mem-race")
    assert dedup.try_acquire("mem-race")
    dedup.release("mem-race")


@needs_redis
def test_dedup_redis_race_and_ttl():
    import redis as redis_lib
    redis_lib.Redis.from_url(REDIS_URL).flushdb()
    from vishwakarma.storage import dedup
    dedup.init_dedup(REDIS_URL)
    assert _race(dedup, "r-race") == 1
    dedup.release("r-race")

    assert dedup.try_acquire("r-ttl", ttl=1)
    assert not dedup.try_acquire("r-ttl", ttl=1)
    time.sleep(1.2)
    assert dedup.try_acquire("r-ttl", ttl=1)  # leaked lock self-expired
    dedup.release("r-ttl")


def test_dedup_redis_down_fallback():
    from vishwakarma.storage import dedup
    dedup.init_dedup("redis://localhost:1/0")  # dead port
    assert dedup.try_acquire("fb")
    assert not dedup.try_acquire("fb")
    dedup.release("fb")


# ── Migration ──────────────────────────────────────────────────────────────────

@needs_pg
def test_migration_roundtrip_idempotent():
    # seed sqlite
    _reset_modules()
    from vishwakarma.storage import db as dbmod
    from vishwakarma.storage import queries as q
    from vishwakarma.storage import patterns as pat
    sqlite_path = tempfile.mktemp(suffix=".db")
    dbmod.init_db(db_path=sqlite_path)
    for i in range(3):
        q.save_incident(incident_id=f"m{i}", title=f"A{i}", question="q",
                        analysis=f"rc {i}", source="am", severity="high")
    pat.save_pattern(pattern_id="p1", alert_name="A0", root_cause_type="oom",
                     root_cause_detail="heap", investigation_steps=[],
                     verification_keywords=["x"])

    _fresh_pg()
    _reset_modules()
    from vishwakarma.storage.migrate import migrate_sqlite_to_postgres
    counts = migrate_sqlite_to_postgres(sqlite_path, PG_DSN)
    assert counts["incidents"] == 3 and counts["rca_patterns"] == 1

    # idempotent re-run, then verify via the query API
    _reset_modules()
    from vishwakarma.storage.migrate import migrate_sqlite_to_postgres as mig2
    mig2(sqlite_path, PG_DSN)
    _reset_modules()
    from vishwakarma.storage import db as db3
    from vishwakarma.storage import queries as q3
    db3.init_db(dsn=PG_DSN)
    rows = [i for i in q3.list_incidents(limit=100) if i["id"].startswith("m")]
    assert len(rows) == 3  # no duplicates
    assert q3.get_incident("m1")["analysis"] == "rc 1"
