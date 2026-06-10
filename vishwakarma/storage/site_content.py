"""
DB-backed site content — the knowledge base + learnings.

Both used to live on the PVC (/data/knowledge.md, /data/learnings/*.md). They now
live in Postgres so every pod (GCP + AWS) shares the same content; the PVC keeps
only the repo cache. Authored via the console or these helpers.
"""
import time

from vishwakarma.storage.db import _get_conn, _lock


# ── Knowledge base ──────────────────────────────────────────────────────────

def get_knowledge(cloud: str = "") -> str:
    """Knowledge for a cloud, falling back to the 'default' ('') row. '' if none."""
    conn = _get_conn()
    for key in ([cloud, ""] if cloud else [""]):
        row = conn.execute("SELECT content_md FROM site_knowledge WHERE cloud=?", (key,)).fetchone()
        if row and dict(row).get("content_md"):
            return dict(row)["content_md"]
    return ""


def set_knowledge(content_md: str, cloud: str = "") -> None:
    conn = _get_conn()
    now = time.time()
    with _lock:
        conn.execute(
            """INSERT INTO site_knowledge (cloud, content_md, updated_at) VALUES (?,?,?)
               ON CONFLICT(cloud) DO UPDATE SET content_md=excluded.content_md,
                                                updated_at=excluded.updated_at""",
            (cloud, content_md, now),
        )
        conn.commit()


def has_knowledge() -> bool:
    return bool(_get_conn().execute("SELECT 1 FROM site_knowledge LIMIT 1").fetchone())


# ── Learnings ───────────────────────────────────────────────────────────────

def get_learning(category: str) -> str | None:
    row = _get_conn().execute(
        "SELECT content_md FROM learnings WHERE category=?", (category,)).fetchone()
    return dict(row)["content_md"] if row else None


def set_learning(category: str, content_md: str) -> None:
    conn = _get_conn()
    now = time.time()
    with _lock:
        conn.execute(
            """INSERT INTO learnings (category, content_md, updated_at) VALUES (?,?,?)
               ON CONFLICT(category) DO UPDATE SET content_md=excluded.content_md,
                                                   updated_at=excluded.updated_at""",
            (category, content_md, now),
        )
        conn.commit()


def list_learnings() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT category, content_md, updated_at FROM learnings ORDER BY category").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        body = d.get("content_md") or ""
        out.append({
            "category": d["category"],
            "fact_count": sum(1 for ln in body.splitlines() if ln.strip().startswith("- ")),
            "size_bytes": len(body.encode("utf-8")),
            "last_modified": d.get("updated_at"),
        })
    return out


def has_learnings() -> bool:
    return bool(_get_conn().execute("SELECT 1 FROM learnings LIMIT 1").fetchone())
