"""
Vector store — semantic similarity over incidents / runbooks / code.

Two storage paths behind one API:
  pgvector       — when the Postgres `vector` extension exists (Cloud SQL prod):
                   true ANN with the <=> cosine operator.
  embeddings_json — fallback for SQLite and PG-without-pgvector: vectors as
                   JSON + brute-force cosine in Python. Fine for the corpus
                   sizes involved (thousands of incidents, hundreds of
                   runbooks); pgvector is the scale path.

API:
  upsert_embedding(kind, ref_id, vec)
  search_similar(kind, query_vec, top_k) -> [(ref_id, cosine_score), ...]
  delete_embedding(kind, ref_id)
"""
import json
import logging
import math
import time

from vishwakarma.storage.db import _get_conn, _lock, get_backend, vector_available

log = logging.getLogger(__name__)

_PG_TABLES = {
    "incident": ("incident_embeddings", "incident_id"),
    "runbook": ("runbook_embeddings", "runbook_id"),
    "code": ("code_embeddings", "id"),
}


def _use_pgvector() -> bool:
    return get_backend() == "postgres" and vector_available()


def upsert_embedding(kind: str, ref_id: str, vec: list[float]) -> None:
    now = time.time()
    conn = _get_conn()
    if _use_pgvector() and kind in _PG_TABLES:
        table, key = _PG_TABLES[kind]
        vec_lit = "[" + ",".join(f"{v:.7g}" for v in vec) + "]"
        if kind == "code":
            sql = (
                f"INSERT INTO {table} (id, repo, path, symbol, embedding, updated_at) "
                f"VALUES (?, '', '', '', ?, ?) "
                f"ON CONFLICT(id) DO UPDATE SET embedding = excluded.embedding, "
                f"updated_at = excluded.updated_at"
            )
        else:
            sql = (
                f"INSERT INTO {table} ({key}, embedding, created_at) VALUES (?, ?, ?) "
                f"ON CONFLICT({key}) DO UPDATE SET embedding = excluded.embedding, "
                f"created_at = excluded.created_at"
            )
        with _lock:
            conn.execute(sql, (ref_id, vec_lit, now))
            conn.commit()
        return
    # JSON fallback
    with _lock:
        conn.execute(
            """
            INSERT INTO embeddings_json (kind, ref_id, vec, created_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(kind, ref_id) DO UPDATE SET
              vec = excluded.vec, created_at = excluded.created_at
            """,
            (kind, ref_id, json.dumps(vec), now),
        )
        conn.commit()


def search_similar(kind: str, query_vec: list[float], top_k: int = 5,
                   min_score: float = 0.0) -> list[tuple[str, float]]:
    """Top-k most similar by cosine. Scores in [-1, 1], higher = closer."""
    conn = _get_conn()
    if _use_pgvector() and kind in _PG_TABLES:
        table, key = _PG_TABLES[kind]
        vec_lit = "[" + ",".join(f"{v:.7g}" for v in query_vec) + "]"
        rows = conn.execute(
            # <=> is cosine DISTANCE in pgvector: similarity = 1 - distance
            f"SELECT {key}, 1 - (embedding <=> ?) AS score FROM {table} "
            f"ORDER BY embedding <=> ? LIMIT ?",
            (vec_lit, vec_lit, top_k),
        ).fetchall()
        return [(r[0], float(r[1])) for r in rows if float(r[1]) >= min_score]

    rows = conn.execute(
        "SELECT ref_id, vec FROM embeddings_json WHERE kind = ?", (kind,)
    ).fetchall()
    scored: list[tuple[str, float]] = []
    for r in rows:
        try:
            v = json.loads(r[1])
            s = _cosine(query_vec, v)
            if s >= min_score:
                scored.append((r[0], s))
        except Exception:
            continue
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


def delete_embedding(kind: str, ref_id: str) -> None:
    conn = _get_conn()
    with _lock:
        if _use_pgvector() and kind in _PG_TABLES:
            table, key = _PG_TABLES[kind]
            conn.execute(f"DELETE FROM {table} WHERE {key} = ?", (ref_id,))
        conn.execute(
            "DELETE FROM embeddings_json WHERE kind = ? AND ref_id = ?", (kind, ref_id)
        )
        conn.commit()


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)
