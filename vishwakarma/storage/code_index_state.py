"""Incremental code-index bookkeeping — file content hashes per repo."""
import time

from vishwakarma.storage.db import _get_conn, _lock


def unchanged(repo: str, path: str, content_md5: str) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT content_md5 FROM code_index_state WHERE repo=? AND path=?",
        (repo, path)).fetchone()
    return bool(row) and row[0] == content_md5


def mark(repo: str, path: str, content_md5: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO code_index_state (repo, path, content_md5, indexed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(repo, path) DO UPDATE SET
              content_md5 = excluded.content_md5, indexed_at = excluded.indexed_at
            """,
            (repo, path, content_md5, time.time()))
        conn.commit()
