"""Runbook mining — clustering + draft orchestration with fakes."""
from vishwakarma.core.runbook_mine import (
    cosine, cluster_rcas, mine, build_draft_prompt, CONFIRMED_SQL)


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


def test_cli_dry_run_writes_nothing(capsys):
    import typer.testing
    from vishwakarma.cli import app
    import vishwakarma.cli as cli_mod
    cli_mod._mine_fetch = lambda cfg, days, limit: []
    r = typer.testing.CliRunner().invoke(app, ["mine", "--since-days", "30"])
    assert r.exit_code == 0
