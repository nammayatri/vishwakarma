"""
Code RAG — chunking, incremental indexing, and semantic search.

Chunking/state run with a stub embedder; one real semantic test runs only when
fastembed is installed.

Run:  pytest tests/test_code_rag.py -v
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

HAVE_FASTEMBED = importlib.util.find_spec("fastembed") is not None


def _reset():
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]


@pytest.fixture()
def db():
    _reset()
    from vishwakarma.storage import db as dbmod
    dbmod.init_db(db_path=tempfile.mktemp(suffix=".db"))
    return dbmod


def test_chunking_by_symbol():
    from vishwakarma.core.code_index import _chunk_file
    src = (
        "def accept_order(o):\n    return o.accept()\n\n"
        "def drain_tickets(q):\n    return q.drain()\n\n"
        "class Foo:\n    pass\n"
    )
    chunks = _chunk_file(src)
    syms = [c[0] for c in chunks]
    assert "accept_order" in syms and "drain_tickets" in syms and "Foo" in syms
    body = dict(chunks)["drain_tickets"]
    assert "q.drain()" in body


def test_chunking_haskell_symbols():
    from vishwakarma.core.code_index import _chunk_file
    src = "forwardOnConfirm :: Context -> IO ()\nforwardOnConfirm ctx = ...\n"
    chunks = _chunk_file(src)
    assert chunks and chunks[0][0] == "forwardOnConfirm"


def test_index_state_incremental(db):
    from vishwakarma.storage import code_index_state as st
    assert not st.unchanged("repo", "a.py", "h1")
    st.mark("repo", "a.py", "h1")
    assert st.unchanged("repo", "a.py", "h1")
    assert not st.unchanged("repo", "a.py", "h2")   # content changed
    st.mark("repo", "a.py", "h2")
    assert st.unchanged("repo", "a.py", "h2")


def test_index_repo_unconfigured_embeddings(db):
    from vishwakarma.core import embeddings
    embeddings.init_embeddings()   # unconfigured
    from vishwakarma.core.code_index import index_repo
    res = index_repo("r", "/tmp")
    assert res.get("error")


@pytest.mark.skipif(not HAVE_FASTEMBED, reason="fastembed not installed")
def test_index_and_search_real(db, tmp_path):
    from vishwakarma.core import embeddings
    embeddings.init_embeddings(local_model="BAAI/bge-small-en-v1.5",
                               provider="local", dim=384)

    repo = tmp_path / "backend"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "orders.py").write_text(
        "def accept_order(order):\n"
        "    # auto-accept and drain the ticket queue\n"
        "    return drain_ticket_queue(order)\n\n"
        "def drain_ticket_queue(q):\n"
        "    return q.process_all()\n")
    (repo / "src" / "billing.py").write_text(
        "def charge_customer(amount):\n"
        "    return payment_gateway.charge(amount)\n")

    from vishwakarma.core.code_index import index_repo, search_code
    res = index_repo("backend", str(repo))
    assert res["indexed"] == 2 and res["chunks"] >= 3

    # natural-language query → the draining code, not billing
    hits = search_code("where are tickets drained from the queue", top_k=3)
    assert hits
    top = hits[0]
    assert "orders.py" in top["path"]
    assert top["symbol"] in ("drain_ticket_queue", "accept_order")

    # incremental: re-index skips unchanged files
    res2 = index_repo("backend", str(repo))
    assert res2["indexed"] == 0 and res2["skipped"] == 2


@pytest.mark.skipif(not HAVE_FASTEMBED, reason="fastembed not installed")
def test_toolset_code_semantic_search(db, tmp_path):
    from vishwakarma.core import embeddings
    embeddings.init_embeddings(local_model="BAAI/bge-small-en-v1.5",
                               provider="local", dim=384)
    repo = tmp_path / "backend"
    repo.mkdir()
    (repo / "h.py").write_text("def forward_callback(ctx):\n    return route_by_url(ctx)\n")

    from vishwakarma.core.code_index import index_repo
    index_repo("backend", str(repo))

    from vishwakarma.core.models import ToolStatus
    from vishwakarma.plugins.toolsets.code_analyst.code_analyst import CodeAnalystToolset
    ts = CodeAnalystToolset({"repo_dir": str(tmp_path),
                             "repos": [{"name": "backend", "url": "u"}]})
    out = ts.execute("code_semantic_search", {"query": "forwarding a callback by url"})
    assert out.status == ToolStatus.SUCCESS and "forward_callback" in str(out.output)
