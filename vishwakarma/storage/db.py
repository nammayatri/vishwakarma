"""
Storage backend — incidents, investigations, dedup state, patterns, evidence.

Two backends behind one connection API (selected at init_db time):
  sqlite    — default; zero-dependency local/OSS quickstart (VK_DB_PATH)
  postgres  — shared control-plane DB for multi-pod (VK_PG_DSN / storage.dsn)

The query modules (queries.py, patterns.py, evidence.py, investigations.py)
are backend-agnostic: they call conn.execute(sql, params) with sqlite-style
`?` placeholders and portable SQL (ON CONFLICT upserts, no sqlite-only
functions). The PGConnection adapter translates placeholders and mimics the
sqlite3 connection interface, so callers never branch on backend.

Schema notes:
  - Timestamps are stored as epoch floats. SQLite type REAL is translated to
    DOUBLE PRECISION for Postgres (REAL in PG is float4 — too coarse for
    epoch-seconds).
  - pgvector tables are created only when the `vector` extension is
    available (Cloud SQL has it; a bare local PG may not). Embedding writes
    are a Phase-2 concern; storage degrades gracefully without it.
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_lock = threading.Lock()
_conn = None  # sqlite3.Connection | PGConnection
_backend: str = "sqlite"
_db_path: str = "/data/vishwakarma.db"
_dsn: str = ""
_vector_available: bool = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    source         TEXT,
    severity       TEXT,
    status         TEXT DEFAULT 'open',
    question       TEXT,
    analysis       TEXT,
    tool_outputs   TEXT,       -- JSON array
    meta           TEXT,       -- JSON object (cost, tokens, duration)
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    resolved_at    REAL,
    labels         TEXT,       -- JSON object
    slack_ts       TEXT,       -- Slack message ts for threading
    pdf_path       TEXT
);

CREATE TABLE IF NOT EXISTS dedup_state (
    fingerprint    TEXT PRIMARY KEY,
    incident_id    TEXT NOT NULL,
    expires_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oracle_sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,           -- first question (auto-title)
    messages    TEXT NOT NULL,           -- JSON array of full message history
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS investigations (
    id            TEXT PRIMARY KEY,      -- = incident_id
    alert_key     TEXT,
    cloud         TEXT,                  -- 'aws' | 'gcp' | 'both' | ''
    status        TEXT DEFAULT 'queued', -- queued|running|awaiting_fix_review|done|failed
    phase         TEXT,                  -- enrich|recon|hypothesize|verify|synthesize|fix
    step          INTEGER DEFAULT 0,
    messages      TEXT,                  -- JSON: conversation so far (resumable state)
    findings      TEXT,                  -- JSON: per-cloud sub-agent results
    code_session  TEXT,                  -- JSON: coding-agent session id + transcript
    attempt       INTEGER DEFAULT 0,
    worker_id     TEXT,
    heartbeat_at  REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings_json (
    kind        TEXT NOT NULL,           -- 'incident' | 'runbook' | 'code'
    ref_id      TEXT NOT NULL,
    vec         TEXT NOT NULL,           -- JSON array of floats (fallback store;
                                         -- pgvector tables used when extension exists)
    created_at  REAL NOT NULL,
    PRIMARY KEY (kind, ref_id)
);

CREATE TABLE IF NOT EXISTS runbooks (
    id            TEXT PRIMARY KEY,      -- slug, e.g. "rds-cpu-high"
    title         TEXT NOT NULL,
    content_md    TEXT NOT NULL,
    cloud_type    TEXT DEFAULT 'any',    -- 'aws' | 'gcp' | 'both' | 'any'
    keywords      TEXT DEFAULT '[]',     -- JSON array (portable across backends)
    services      TEXT DEFAULT '[]',     -- JSON array — applicable services facet
    author        TEXT,
    version       INTEGER DEFAULT 1,
    status        TEXT DEFAULT 'active', -- 'active' | 'needs-review' | 'demoted'
    hit_count     INTEGER DEFAULT 0,
    miss_count    INTEGER DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_runbook_map (
    alert_pattern TEXT NOT NULL,         -- normalized alert key
    runbook_id    TEXT NOT NULL,
    priority      INTEGER DEFAULT 100,
    PRIMARY KEY (alert_pattern, runbook_id)
);

CREATE TABLE IF NOT EXISTS tool_effectiveness (
    alert_key   TEXT NOT NULL,           -- normalized alert key
    toolset     TEXT NOT NULL,
    hits        INTEGER DEFAULT 0,        -- contributed to a confirmed RCA
    updated_at  REAL NOT NULL,
    PRIMARY KEY (alert_key, toolset)
);

CREATE INDEX IF NOT EXISTS idx_tooleff_alert ON tool_effectiveness(alert_key);

CREATE TABLE IF NOT EXISTS correlated_alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id     TEXT NOT NULL,         -- the incident this alert was grouped into
    alert_title   TEXT,
    alert_labels  TEXT,                  -- JSON
    created_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_correlated_parent ON correlated_alerts(parent_id);

CREATE TABLE IF NOT EXISTS cross_cloud_findings (
    incident_id   TEXT NOT NULL,
    cloud         TEXT NOT NULL,         -- 'aws' | 'gcp'
    rca           TEXT,                  -- this cloud's RCA text
    meta          TEXT,                  -- JSON
    created_at    REAL NOT NULL,
    PRIMARY KEY (incident_id, cloud)
);

CREATE TABLE IF NOT EXISTS cross_cloud_synthesis (
    incident_id   TEXT PRIMARY KEY,      -- claimed once by the synthesizing worker
    worker_id     TEXT,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor       TEXT,                    -- role/user that did it
    action      TEXT NOT NULL,           -- e.g. runbook.save, fix.approve
    target      TEXT,                    -- the object id
    detail      TEXT                     -- JSON
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_embeddings_kind ON embeddings_json(kind);
CREATE INDEX IF NOT EXISTS idx_runbooks_status ON runbooks(status);
CREATE INDEX IF NOT EXISTS idx_alert_map ON alert_runbook_map(alert_pattern);
CREATE INDEX IF NOT EXISTS idx_incidents_source ON incidents(source);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at);
CREATE INDEX IF NOT EXISTS idx_incidents_source_created ON incidents(source, created_at);
CREATE INDEX IF NOT EXISTS idx_oracle_sessions_updated ON oracle_sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_investigations_status ON investigations(status);
CREATE INDEX IF NOT EXISTS idx_investigations_heartbeat ON investigations(heartbeat_at);
"""

# pgvector embedding tables — Postgres only, created when the extension exists.
VECTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS incident_embeddings (
    incident_id  TEXT PRIMARY KEY,
    embedding    VECTOR(%(dim)s) NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS runbook_embeddings (
    runbook_id   TEXT PRIMARY KEY,
    embedding    VECTOR(%(dim)s) NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS code_embeddings (
    id           TEXT PRIMARY KEY,      -- repo:path:symbol
    repo         TEXT NOT NULL,
    path         TEXT NOT NULL,
    symbol       TEXT,
    embedding    VECTOR(%(dim)s) NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL
);
"""
EMBEDDING_DIM = 1536


class PGConnection:
    """
    Adapter that makes a psycopg2 connection look like the sqlite3 connection
    the query modules expect:

      conn.execute(sql, params) -> cursor      (sqlite-style `?` placeholders)
      conn.executescript(sql)   -> None
      conn.commit()

    Cursors use DictCursor so rows support BOTH index access (row[0]) and
    key access (row["col"]) and dict(row) — matching sqlite3.Row usage in the
    query modules. Reconnects once on a dropped connection.
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pg = self._connect()

    def _connect(self):
        import psycopg2
        conn = psycopg2.connect(self._dsn)
        conn.autocommit = False
        return conn

    @staticmethod
    def _translate(sql: str) -> str:
        # sqlite `?` placeholders -> psycopg2 `%s`. Our SQL never contains a
        # literal '?' outside placeholders, and any literal '%' must be
        # doubled for psycopg2.
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, params=()):  # noqa: ANN001 — mirrors sqlite3 API
        import psycopg2
        from psycopg2.extras import DictCursor
        for attempt in (1, 2):
            try:
                cur = self._pg.cursor(cursor_factory=DictCursor)
                cur.execute(self._translate(sql), tuple(params))
                return cur
            except psycopg2.OperationalError:
                if attempt == 2:
                    raise
                log.warning("Postgres connection lost — reconnecting")
                try:
                    self._pg.close()
                except Exception:
                    pass
                self._pg = self._connect()
            except Exception:
                # Failed statement poisons the transaction — roll back so the
                # connection stays usable for subsequent queries.
                self._pg.rollback()
                raise

    def executescript(self, sql: str):
        # psycopg2 supports multi-statement strings in a single execute.
        cur = self._pg.cursor()
        cur.execute(sql)

    def commit(self):
        self._pg.commit()

    def rollback(self):
        self._pg.rollback()

    def close(self):
        try:
            self._pg.close()
        except Exception:
            pass


def _to_pg_schema(schema: str) -> str:
    """Translate the shared (SQLite-dialect) schema to Postgres."""
    return (schema
            .replace(" REAL", " DOUBLE PRECISION")
            .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))


def init_db(db_path: str | None = None, dsn: str | None = None) -> None:
    """
    Initialize storage and create schema.

    dsn (or a previously-configured one) selects the Postgres backend;
    otherwise SQLite at db_path. Safe to call repeatedly.
    """
    global _db_path, _dsn, _conn, _backend, _vector_available
    if db_path:
        _db_path = db_path
    if dsn:
        _dsn = dsn

    with _lock:
        if _dsn:
            _backend = "postgres"
            _conn = PGConnection(_dsn)
            _conn.executescript(_to_pg_schema(SCHEMA))
            from vishwakarma.storage.patterns import PATTERNS_SCHEMA
            _conn.executescript(_to_pg_schema(PATTERNS_SCHEMA))
            from vishwakarma.storage.evidence import EVIDENCE_SCHEMA
            _conn.executescript(_to_pg_schema(EVIDENCE_SCHEMA))
            # Commit base schema BEFORE attempting pgvector — a failed
            # CREATE EXTENSION rolls back its transaction, and must not
            # take the uncommitted schema down with it.
            _conn.commit()
            _vector_available = _try_enable_pgvector(_conn)
            _conn.commit()
            log.info(
                f"Database initialized (postgres, pgvector={'on' if _vector_available else 'off'})"
            )
        else:
            _backend = "sqlite"
            Path(_db_path).parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(_db_path, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            _conn.commit()
            from vishwakarma.storage.patterns import PATTERNS_SCHEMA
            _conn.executescript(PATTERNS_SCHEMA)
            from vishwakarma.storage.evidence import EVIDENCE_SCHEMA
            _conn.executescript(EVIDENCE_SCHEMA)
            _conn.commit()
            log.info(f"Database initialized at {_db_path}")


def _try_enable_pgvector(conn: PGConnection) -> bool:
    """Enable pgvector + create embedding tables if the extension exists."""
    try:
        conn.executescript("CREATE EXTENSION IF NOT EXISTS vector")
        conn.executescript(VECTOR_SCHEMA % {"dim": EMBEDDING_DIM})
        return True
    except Exception as e:
        conn.rollback()
        log.info(f"pgvector unavailable — embedding tables skipped ({str(e).strip()[:80]})")
        return False


def get_backend() -> str:
    return _backend


def vector_available() -> bool:
    return _vector_available


def _get_conn():
    global _conn
    if _conn is None:
        init_db()
    return _conn  # type: ignore
