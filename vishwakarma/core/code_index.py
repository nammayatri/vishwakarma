"""
Code RAG — semantic search over repo source.

Chunks source files by top-level symbol (function / class / data type), embeds
each chunk with the configured embeddings provider (local fastembed by
default), and stores vectors keyed by repo:path:symbol. code_semantic_search
then finds the relevant code for a natural-language query — "where is offer
draining handled?" — that grep can't answer.

Incremental: re-index only files whose content hash changed since last index.
Best-effort throughout; degrades to no-op when embeddings are unconfigured
(the agent still has grep/ast-grep/LSP).
"""
import hashlib
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Files worth indexing (source), by extension.
_SOURCE_EXT = {".hs", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java",
               ".rb", ".rs", ".kt", ".scala", ".swift", ".purs"}

# Top-level symbol starts (language-agnostic enough for chunk boundaries).
_SYMBOL_RE = re.compile(
    r"^(?:"
    r"def\s+(\w+)"                       # python
    r"|class\s+(\w+)"                    # python/js/java
    r"|(?:async\s+)?function\s+(\w+)"    # js/ts
    r"|(?:export\s+)?const\s+(\w+)\s*="  # js/ts arrow fns
    r"|(\w+)\s*::"                       # haskell type sig
    r"|data\s+(\w+)"                     # haskell data
    r"|newtype\s+(\w+)"                  # haskell newtype
    r"|func\s+(\w+)"                     # go
    r")",
    re.MULTILINE,
)

MAX_CHUNK_LINES = 120
MAX_FILE_BYTES = 400_000   # skip generated/huge files


def _chunk_file(text: str) -> list[tuple[str, str]]:
    """Return [(symbol, chunk_text)] split at top-level symbol boundaries."""
    lines = text.splitlines()
    # find symbol-start line indices
    starts: list[tuple[int, str]] = []
    for m in _SYMBOL_RE.finditer(text):
        sym = next((g for g in m.groups() if g), "")
        line_no = text[: m.start()].count("\n")
        starts.append((line_no, sym))
    if not starts:
        # no symbols — one chunk (capped)
        return [("(module)", "\n".join(lines[:MAX_CHUNK_LINES]))] if lines else []

    chunks = []
    for i, (ln, sym) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        end = min(end, ln + MAX_CHUNK_LINES)
        body = "\n".join(lines[ln:end]).strip()
        if body:
            chunks.append((sym or f"sym{ln}", body))
    return chunks


def _iter_source_files(repo_path: Path, sub_paths: list[str] | None):
    roots = [repo_path / p for p in sub_paths] if sub_paths else [repo_path]
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if (f.is_file() and f.suffix in _SOURCE_EXT
                    and ".git" not in f.parts
                    and f.stat().st_size <= MAX_FILE_BYTES):
                yield f


def index_repo(repo_name: str, repo_path: str, sub_paths: list[str] | None = None,
               max_files: int = 50_000, on_progress=None) -> dict:
    """
    Index a repo's source into code_embeddings. Incremental by content hash —
    only files whose content changed since the last run are re-embedded.

    Intended to run as a BACKGROUND/scheduled job (vk index-code), NOT inline
    during an investigation: a first full index of a large monorepo embeds tens
    of thousands of chunks. Scope with sub_paths (e.g. ["Backend"]) to index
    only the modules that matter. Returns {indexed, skipped, chunks}.
    """
    from vishwakarma.core.embeddings import get_client
    from vishwakarma.storage.vectors import upsert_embedding
    from vishwakarma.storage import code_index_state as state

    emb = get_client()
    if not emb.configured:
        return {"error": "embeddings not configured — code RAG disabled"}

    root = Path(repo_path)
    indexed = skipped = chunks_total = 0
    for f in _iter_source_files(root, sub_paths):
        if indexed >= max_files:
            break
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        rel = str(f.relative_to(root))
        h = hashlib.md5(text.encode()).hexdigest()
        if state.unchanged(repo_name, rel, h):
            skipped += 1
            continue

        chunks = _chunk_file(text)
        if not chunks:
            continue
        # embed all chunks of this file in one batch
        vecs = emb.embed([f"{rel}\n{sym}\n{body}" for sym, body in chunks])
        if not vecs:
            continue
        for (sym, _body), vec in zip(chunks, vecs):
            cid = f"{repo_name}:{rel}:{sym}"
            upsert_embedding("code", cid, vec)
        state.mark(repo_name, rel, h)
        indexed += 1
        chunks_total += len(chunks)
        if on_progress and indexed % 200 == 0:
            on_progress(indexed, chunks_total)

    log.info(f"Code index {repo_name}: {indexed} files ({chunks_total} chunks), {skipped} unchanged")
    return {"indexed": indexed, "skipped": skipped, "chunks": chunks_total}


def search_code(query: str, top_k: int = 8) -> list[dict]:
    """Semantic search over indexed code. Returns [{repo, path, symbol, score}]."""
    from vishwakarma.core.embeddings import get_client
    from vishwakarma.storage.vectors import search_similar
    from vishwakarma.storage.db import get_backend, vector_available
    emb = get_client()
    if not emb.configured:
        return []
    # Scale guard: brute-force cosine over the JSON fallback is O(n). Fine for
    # dev/small; at scale prod must use pgvector (indexed ANN).
    if not (get_backend() == "postgres" and vector_available()):
        try:
            from vishwakarma.storage.db import _get_conn
            n = _get_conn().execute(
                "SELECT COUNT(*) FROM embeddings_json WHERE kind='code'").fetchone()[0]
            if n > 20_000:
                log.warning(f"code RAG: {n} chunks on the JSON store — search will be "
                            "slow; use Postgres+pgvector in production")
        except Exception:
            pass
    qvec = emb.embed_one(query)
    if not qvec:
        return []
    out = []
    for cid, score in search_similar("code", qvec, top_k=top_k, min_score=0.3):
        parts = cid.split(":", 2)
        if len(parts) == 3:
            out.append({"repo": parts[0], "path": parts[1], "symbol": parts[2],
                        "score": round(score, 3)})
    return out
