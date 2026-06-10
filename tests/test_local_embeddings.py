"""
Local + API embeddings provider tests.

The wiring (provider selection, degradation, vector-store roundtrip with a
non-1536 dim) is tested with a stub. A real fastembed test runs only when the
lib is installed.

Run:  pytest tests/test_local_embeddings.py -v
"""
import importlib.util
import sys
import tempfile

import pytest

HAVE_FASTEMBED = importlib.util.find_spec("fastembed") is not None


def _reset():
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]


def test_provider_inference():
    from vishwakarma.core.embeddings import EmbeddingClient
    assert EmbeddingClient(local_model="m").provider == "local"
    assert EmbeddingClient(api_base="http://x", model="m").provider == "api"
    assert EmbeddingClient().provider == ""
    # explicit wins
    assert EmbeddingClient(api_base="http://x", model="m", provider="local").provider == "local"


def test_unconfigured_degrades():
    from vishwakarma.core.embeddings import EmbeddingClient
    assert EmbeddingClient().embed(["x"]) is None
    assert EmbeddingClient(provider="local").embed(["x"]) is None   # no model


def test_local_missing_lib_degrades(monkeypatch):
    from vishwakarma.core.embeddings import EmbeddingClient
    c = EmbeddingClient(provider="local", local_model="some/model", dim=384)
    # simulate fastembed not installed
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name == "fastembed":
            raise ImportError("no fastembed")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert c.embed(["x"]) is None   # graceful, not a crash


def test_vector_store_roundtrip_384_dim():
    """The JSON vector store works for any dim (local models are 384)."""
    _reset()
    from vishwakarma.storage import db as dbmod
    dbmod.init_db(db_path=tempfile.mktemp(suffix=".db"))
    from vishwakarma.storage import vectors
    v384 = [0.1] * 384
    vectors.upsert_embedding("incident", "i1", v384)
    hits = vectors.search_similar("incident", v384, top_k=1)
    assert hits and hits[0][0] == "i1"


@pytest.mark.skipif(not HAVE_FASTEMBED, reason="fastembed not installed")
def test_real_local_embedding_semantic():
    from vishwakarma.core.embeddings import EmbeddingClient
    import math
    c = EmbeddingClient(provider="local", local_model="BAAI/bge-small-en-v1.5", dim=384)
    assert c.configured
    docs = c.embed(["RDS CPU high missing index seqscan",
                    "redis memory evictions maxmemory"])
    assert len(docs) == 2 and len(docs[0]) == 384

    q = c.embed_one("database cpu spike caused by a missing index")

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b)) / (
            math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

    # the DB query is semantically closer to the RDS doc than the redis doc
    assert cos(q, docs[0]) > cos(q, docs[1])
