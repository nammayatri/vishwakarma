"""
Phase 2 RAG tests — vector store, incident RAG, runbook storage + hybrid matching.

Embeddings use a deterministic fake (hash-bucket vectors) so tests run with
no provider. Postgres legs are covered by the Phase-0 parity suite; these
run on SQLite (the JSON/cosine fallback path, which is also what local PG14
without pgvector uses).

Run:  pytest tests/test_rag_phase2.py -v
"""
import sys
import tempfile

import pytest

from vishwakarma.core.models import ToolStatus


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


def fake_vec(text: str, dim: int = 32) -> list[float]:
    """Deterministic pseudo-embedding: similar words → overlapping buckets."""
    v = [0.0] * dim
    for word in text.lower().split():
        v[hash(word) % dim] += 1.0
    return v


# ── Vector store ───────────────────────────────────────────────────────────────

def test_vector_roundtrip_and_ranking(db):
    from vishwakarma.storage import vectors

    docs = {
        "inc-rds": "rds cpu high missing index seqscan",
        "inc-redis": "redis memory evictions maxmemory",
        "inc-alb": "alb 5xx errors upstream timeout",
    }
    for rid, text in docs.items():
        vectors.upsert_embedding("incident", rid, fake_vec(text))

    # query about database CPU should rank the RDS incident first
    hits = vectors.search_similar("incident", fake_vec("cpu high rds index"), top_k=3)
    assert hits and hits[0][0] == "inc-rds"
    assert hits[0][1] > hits[-1][1]

    # upsert overwrites (same id, new vector)
    vectors.upsert_embedding("incident", "inc-rds", fake_vec("totally different topic now"))
    hits2 = vectors.search_similar("incident", fake_vec("cpu high rds index"), top_k=1)
    assert hits2[0][0] != "inc-rds" or hits2[0][1] < hits[0][1]

    vectors.delete_embedding("incident", "inc-redis")
    remaining = vectors.search_similar("incident", fake_vec("redis evictions"), top_k=5)
    assert all(rid != "inc-redis" for rid, _ in remaining)


def test_vector_kinds_are_isolated(db):
    from vishwakarma.storage import vectors
    vectors.upsert_embedding("incident", "x1", fake_vec("alpha beta"))
    vectors.upsert_embedding("runbook", "r1", fake_vec("alpha beta"))
    assert [h[0] for h in vectors.search_similar("runbook", fake_vec("alpha beta"))] == ["r1"]


# ── Embeddings client degradation ─────────────────────────────────────────────

def test_embeddings_unconfigured_returns_none():
    from vishwakarma.core.embeddings import EmbeddingClient
    c = EmbeddingClient()
    assert not c.configured
    assert c.embed(["x"]) is None
    assert c.embed_one("x") is None


# ── Runbook storage ────────────────────────────────────────────────────────────

def test_runbook_crud_and_normalization(db):
    from vishwakarma.storage import runbooks as rb

    assert rb.normalize_alert_key("RDS-CPU-Production-High") == "rdscpu"
    assert rb.normalize_alert_key("RDSCpuHigh") == "rdscpuhigh"  # camelcase kept whole
    assert rb.normalize_alert_key("redis_evictions_WARNING") == "redisevictions"

    rb.save_runbook("rds-cpu", "RDS CPU investigation", "## Steps\n1. check pg_stat",
                    cloud_type="aws", keywords=["rds", "cpu"])
    rb.save_runbook("rds-cpu", "RDS CPU investigation v2", "## Steps\nupdated",
                    cloud_type="aws", keywords=["rds", "cpu"])
    got = rb.get_runbook("rds-cpu")
    assert got["version"] == 2 and got["title"].endswith("v2")

    rb.save_runbook("gcp-only", "AlloyDB thing", "x", cloud_type="gcp", keywords=["alloydb"])
    aws_list = rb.list_runbooks(cloud="aws")
    assert {r["id"] for r in aws_list} == {"rds-cpu"}  # gcp-only excluded


def test_runbook_map_and_feedback(db):
    from vishwakarma.storage import runbooks as rb

    rb.save_runbook("rb-1", "T", "C", keywords=["x"])
    rb.map_alert("RDS-CPU-High", "rb-1")
    assert rb.mapped_runbook_ids("RDSCPU") == ["rb-1"]          # normalized variants meet
    assert rb.mapped_runbook_ids("rds cpu production") == ["rb-1"]

    # hit self-populates the map for a new alert spelling
    rb.mark_runbook_hit("rb-1", alert_name="DatabaseCpuSpike")
    assert rb.mapped_runbook_ids("database cpu spike") == ["rb-1"]
    assert rb.get_runbook("rb-1")["hit_count"] == 1

    # misses demote once they dominate
    for _ in range(3):
        rb.mark_runbook_miss("rb-1")
    assert rb.get_runbook("rb-1")["status"] == "demoted"
    # demoted runbooks drop out of active listing
    assert all(r["id"] != "rb-1" for r in rb.list_runbooks(status="active"))


def test_seed_from_files_idempotent(db, tmp_path):
    import json as _json
    from vishwakarma.storage import runbooks as rb

    (tmp_path / "rb").mkdir()
    (tmp_path / "rb" / "a.md").write_text("# A runbook\ncheck things")
    agents = {"agents": [
        {"id": "alert-a", "description": "Investigate A", "keywords": ["aaa"],
         "runbook": "rb/a.md"},
        {"id": "missing", "description": "no file", "keywords": ["m"],
         "runbook": "rb/nope.md"},
    ]}
    p = tmp_path / "agents.json"
    p.write_text(_json.dumps(agents))

    assert rb.seed_from_files(p) == 1
    assert rb.seed_from_files(p) == 1  # idempotent
    got = rb.get_runbook("alert-a")
    assert got and "check things" in got["content_md"]
    assert got["keywords"] == ["aaa"]


# ── Hybrid matcher ─────────────────────────────────────────────────────────────

@pytest.fixture()
def seeded(db):
    from vishwakarma.storage import runbooks as rb
    rb.save_runbook("rds-cpu", "RDS CPU high", "rds steps", cloud_type="aws",
                    keywords=["rds", "cpu", "database"])
    rb.save_runbook("redis-evict", "Redis evictions", "redis steps", cloud_type="both",
                    keywords=["redis", "evictions", "memory"])
    rb.save_runbook("alb-5xx", "ALB 5xx", "alb steps", cloud_type="aws",
                    keywords=["alb", "5xx", "errors"])
    rb.save_runbook("gcp-lb", "GCP LB 5xx", "gcp steps", cloud_type="gcp",
                    keywords=["5xx", "load", "balancer"])
    return rb


def test_match_keyword_leg(seeded):
    from vishwakarma.core.runbook_match import match_runbooks
    got = match_runbooks("RDS-CPU-Production-High", cloud="aws")
    assert got and got[0]["id"] == "rds-cpu"


def test_match_cloud_filter(seeded):
    from vishwakarma.core.runbook_match import match_runbooks
    aws = match_runbooks("backend 5xx errors spike", cloud="aws")
    assert all(r["id"] != "gcp-lb" for r in aws)
    gcp = match_runbooks("load balancer 5xx", cloud="gcp")
    assert any(r["id"] == "gcp-lb" for r in gcp)


def test_match_exact_map_outranks_keywords(seeded):
    from vishwakarma.core.runbook_match import match_runbooks
    # keyword overlap alone would prefer redis-evict for this text; an explicit
    # map row for the alert must outrank it (RRF weight 2.0)
    seeded.map_alert("MemoryPressure", "rds-cpu")
    got = match_runbooks("memory pressure redis evictions", cloud="")
    ids = [r["id"] for r in got]
    assert "redis-evict" in ids  # keyword leg still contributes
    got_mapped = match_runbooks("MemoryPressure", cloud="")
    assert got_mapped and got_mapped[0]["id"] == "rds-cpu"


def test_match_vector_leg_with_fake_embeddings(seeded, monkeypatch):
    from vishwakarma.core import embeddings as emb_mod
    from vishwakarma.storage import vectors

    class FakeClient:
        configured = True
        def embed_one(self, text):
            return fake_vec(text)

    monkeypatch.setattr(emb_mod, "get_client", lambda: FakeClient())

    # index runbooks with content the keywords DON'T cover
    vectors.upsert_embedding("runbook", "alb-5xx", fake_vec("ingress gateway upstream errors"))
    from vishwakarma.core.runbook_match import match_runbooks
    got = match_runbooks("ingress gateway upstream errors", cloud="aws")
    assert any(r["id"] == "alb-5xx" for r in got)  # found via vector leg only


def test_match_no_candidates(db):
    from vishwakarma.core.runbook_match import match_runbooks
    assert match_runbooks("anything", cloud="aws") == []


# ── runbook_search tool ────────────────────────────────────────────────────────

def test_runbook_search_tool(seeded):
    from vishwakarma.plugins.toolsets.runbooks.runbooks import RunbookToolset
    ts = RunbookToolset({})
    out = ts.execute("runbook_search", {"query": "redis memory evictions climbing"})
    assert out.status == ToolStatus.SUCCESS, out.error
    assert "Redis evictions" in str(out.output)

    out = ts.execute("runbook_search", {"query": "zz unmatched topic qq"})
    assert out.status == ToolStatus.NO_DATA

    out = ts.execute("runbook_search", {"query": ""})
    assert out.status == ToolStatus.ERROR
