"""Runbook mining — clustering + draft orchestration with fakes."""
import sys
import tempfile

import pytest

from vishwakarma.core.runbook_mine import (
    cosine, cluster_rcas, mine, build_draft_prompt, CONFIRMED_SQL)


def _reset():
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]


class _FakeCfg:
    def __init__(self, db_path):
        self.db_path = db_path
        self.pg_dsn = ""


@pytest.fixture()
def db():
    _reset()
    from vishwakarma.storage import db as dbmod
    path = tempfile.mktemp(suffix=".db")
    dbmod.init_db(db_path=path)
    from vishwakarma.storage.evidence import init_evidence
    init_evidence()
    return path


def vecs(*rows):
    return [[float(x) for x in r] for r in rows]


def test_cosine():
    assert round(cosine([1, 0], [1, 0]), 3) == 1.0
    assert round(cosine([1, 0], [0, 1]), 3) == 0.0


def test_cluster_rcas_groups_similar():
    items = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    v = vecs((1, 0, 0), (0.99, 0.01, 0), (0, 1, 0))
    clusters = cluster_rcas(items, v, threshold=0.9)
    assert [[i["id"] for i in c] for c in clusters] == [["1", "2"], ["3"]]


def test_mine_saves_only_multi_incident_clusters():
    items = [{"id": "1", "title": "RDS CPU", "analysis": "autovacuum on toast"},
             {"id": "2", "title": "RDS CPU", "analysis": "autovacuum toast bloat"},
             {"id": "3", "title": "Redis", "analysis": "evictions"}]
    v = vecs((1, 0, 0), (0.99, 0.01, 0), (0, 1, 0))
    saved = []
    out = mine(items, embed_fn=lambda ts: v, min_cluster=2,
               draft_fn=lambda c: build_draft_prompt(c) and
               f"# RB\n> Provenance: mined from {len(c)} incidents (ids: "
               + ", ".join(i["id"] for i in c) + "), unverified",
               save_fn=lambda **kw: saved.append(kw))
    assert len(out) == 1 and len(saved) == 1
    assert "(ids: 1, 2)" in saved[0]["content_md"]


def test_confirmed_sql_constant():
    assert "evidence_snapshots" in CONFIRMED_SQL and "'correct'" in CONFIRMED_SQL


def test_cli_dry_run_writes_nothing(capsys, monkeypatch):
    import typer.testing
    from vishwakarma.cli import app
    import vishwakarma.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_mine_fetch", lambda cfg, days, limit: [])
    r = typer.testing.CliRunner().invoke(app, ["mine", "--since-days", "30"])
    assert r.exit_code == 0


def test_mine_fetch_finds_a_real_confirmed_incident_end_to_end(db):
    # Exercises the real path: save an incident, click ✅ (evidence.mark_evidence_correct),
    # then call the actual _mine_fetch (not a monkeypatch) — the same query `vk mine` runs.
    from vishwakarma.storage.queries import save_incident
    from vishwakarma.storage.evidence import mark_evidence_correct
    import vishwakarma.cli as cli_mod

    save_incident("inc-1", "AllocatorJobPickupDelayHigh", question="q",
                  analysis="deploy diff caused the delay spike", source="alertmanager")
    mark_evidence_correct("inc-1", alert_name="AllocatorJobPickupDelayHigh")

    rows = cli_mod._mine_fetch(_FakeCfg(db), since_days=90, limit=500)
    assert [r["id"] for r in rows] == ["inc-1"]
    assert rows[0]["analysis"] == "deploy diff caused the delay spike"


def test_mine_fetch_excludes_unconfirmed_incidents(db):
    from vishwakarma.storage.queries import save_incident
    import vishwakarma.cli as cli_mod

    save_incident("inc-2", "SomeAlert", question="q", analysis="no feedback yet", source="alertmanager")
    rows = cli_mod._mine_fetch(_FakeCfg(db), since_days=90, limit=500)
    assert rows == []
