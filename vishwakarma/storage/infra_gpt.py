"""Per-Slack-thread continuity for the ny-infra-gpt conversation_id."""
import time

from vishwakarma.storage.db import _get_conn, _lock


def thread_key(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def get_conversation_id(channel: str, thread_ts: str) -> int | None:
    row = _get_conn().execute(
        "SELECT conversation_id FROM infra_gpt_conversations WHERE thread_key = ?",
        (thread_key(channel, thread_ts),),
    ).fetchone()
    return dict(row)["conversation_id"] if row else None


def save_conversation_id(channel: str, thread_ts: str, conversation_id: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO infra_gpt_conversations (thread_key, conversation_id, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(thread_key) DO UPDATE SET "
            "  conversation_id = excluded.conversation_id, updated_at = excluded.updated_at",
            (thread_key(channel, thread_ts), conversation_id, time.time()),
        )
        conn.commit()
