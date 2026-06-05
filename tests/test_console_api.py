"""
Console API tests — REST endpoints, RBAC, feedback wiring, event bus.

Uses FastAPI TestClient on a router mounted standalone (no full server
startup, no Slack/LLM). SQLite backend.

Run:  pytest tests/test_console_api.py -v
"""
import sys
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _reset():
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]


class Cfg:
    ui_auth_disabled = True
    ui_admin_tokens: list = []
    ui_reader_tokens: list = []


class CfgSecured(Cfg):
    ui_auth_disabled = False
    ui_admin_tokens = ["admin-tok"]
    ui_reader_tokens = ["reader-tok"]


def make_client(cfg) -> TestClient:
    from vishwakarma.ui.console_api import create_console_router
    app = FastAPI()
    app.include_router(create_console_router(cfg, {}))
    return TestClient(app)


@pytest.fixture()
def db():
    _reset()
    from vishwakarma.storage import db as dbmod
    dbmod.init_db(db_path=tempfile.mktemp(suffix=".db"))
    return dbmod


@pytest.fixture()
def seeded(db):
    from vishwakarma.storage import queries as q
    from vishwakarma.storage import runbooks as rb
    from vishwakarma.storage import investigations as inv
    q.save_incident(incident_id="inc-1", title="RDS CPU High", question="q",
                    analysis="missing index on driver_offers seqscan",
                    source="alertmanager", severity="critical")
    rb.save_runbook("rds-cpu", "RDS CPU", "## steps", cloud_type="aws",
                    keywords=["rds", "cpu"])
    inv.create_investigation("inv-1", alert_key="rdscpu", cloud="aws")
    inv.claim_investigation("inv-1", "exec-1")
    inv.checkpoint_investigation("inv-1", messages=[{"role": "user", "content": "x"}],
                                 step=2, phase="recon")
    return db


# ── Reads ─────────────────────────────────────────────────────────────────────

def test_overview(seeded):
    c = make_client(Cfg())
    r = c.get("/api/console/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["incident_stats"]["total"] == 1
    assert any(i["id"] == "inv-1" for i in body["active_investigations"])
    # transcripts not in cards
    assert all("messages" not in i for i in body["active_investigations"])


def test_investigations_list_and_detail(seeded):
    c = make_client(Cfg())
    r = c.get("/api/console/investigations")
    assert r.status_code == 200
    rows = r.json()
    assert rows and rows[0]["id"] == "inv-1"
    assert "messages" not in rows[0]            # list stays light

    r = c.get("/api/console/investigations/inv-1")
    assert r.status_code == 200
    assert r.json()["messages"] == [{"role": "user", "content": "x"}]  # detail has it
    assert r.json()["step"] == 2

    assert c.get("/api/console/investigations/nope").status_code == 404


def test_incidents_search_and_detail(seeded):
    c = make_client(Cfg())
    r = c.get("/api/console/incidents", params={"q": "seqscan"})
    assert r.status_code == 200 and len(r.json()) == 1
    r = c.get("/api/console/incidents/inc-1")
    assert r.status_code == 200 and r.json()["title"] == "RDS CPU High"
    assert c.get("/api/console/incidents", params={"q": "zzz"}).json() == []


# ── Runbook studio ────────────────────────────────────────────────────────────

def test_runbook_crud_mapping_dryrun(seeded):
    c = make_client(Cfg())
    # list + get
    assert any(r["id"] == "rds-cpu" for r in c.get("/api/console/runbooks").json())
    assert c.get("/api/console/runbooks/rds-cpu").json()["title"] == "RDS CPU"

    # save (update) bumps version
    r = c.put("/api/console/runbooks/rds-cpu", json={
        "title": "RDS CPU v2", "content_md": "## new", "cloud_type": "aws",
        "keywords": ["rds", "cpu", "database"]})
    assert r.status_code == 200 and r.json()["version"] == 2

    # mapping
    r = c.post("/api/console/runbooks/rds-cpu/mappings",
               json={"alert_name": "DatabaseCpuSpike"})
    assert r.status_code == 200 and r.json()["mapped"] == "databasecpuspike"

    # dry-run finds it via the map + keywords
    r = c.post("/api/console/runbooks/dry-run",
               json={"alert_text": "DatabaseCpuSpike", "cloud": "aws"})
    assert r.status_code == 200
    assert r.json() and r.json()[0]["id"] == "rds-cpu"

    # create new + delete
    r = c.put("/api/console/runbooks/new-rb", json={
        "title": "New", "content_md": "x"})
    assert r.status_code == 200
    assert c.delete("/api/console/runbooks/new-rb").json()["deleted"] == "new-rb"
    assert c.get("/api/console/runbooks/new-rb").status_code == 404


# ── Feedback ──────────────────────────────────────────────────────────────────

def test_feedback_updates_runbook_counters(seeded):
    c = make_client(Cfg())
    r = c.post("/api/console/incidents/inc-1/feedback", json={
        "correct": True, "runbook_ids": ["rds-cpu"], "alert_name": "RDSCpuHigh"})
    assert r.status_code == 200
    from vishwakarma.storage import runbooks as rb
    got = rb.get_runbook("rds-cpu")
    assert got["hit_count"] == 1
    # confirmed hit self-populated the alert map
    assert rb.mapped_runbook_ids("RDSCpuHigh") == ["rds-cpu"]

    c.post("/api/console/incidents/inc-1/feedback", json={
        "correct": False, "runbook_ids": ["rds-cpu"]})
    assert rb.get_runbook("rds-cpu")["miss_count"] == 1


# ── Fleet + fixes ─────────────────────────────────────────────────────────────

def test_fleet_snapshot_without_redis(seeded):
    c = make_client(Cfg())
    r = c.get("/api/console/fleet")
    assert r.status_code == 200
    body = r.json()
    assert body["queues"] == {}                  # no Redis in this test
    assert any(e["worker_id"] == "exec-1" for e in body["executors"])


def test_fixes_lists_awaiting_review(seeded):
    from vishwakarma.storage import investigations as inv
    inv.create_investigation("inv-fix", alert_key="x")
    inv.finish_investigation("inv-fix", "awaiting_fix_review")
    c = make_client(Cfg())
    rows = c.get("/api/console/fixes").json()
    assert any(r["id"] == "inv-fix" for r in rows)


# ── RBAC ──────────────────────────────────────────────────────────────────────

def test_rbac_blocks_unauthenticated(seeded):
    c = make_client(CfgSecured())
    assert c.get("/api/console/overview").status_code == 401


def test_rbac_reader_reads_but_cannot_write(seeded):
    c = make_client(CfgSecured())
    h = {"X-VK-Token": "reader-tok"}
    assert c.get("/api/console/overview", headers=h).status_code == 200
    r = c.put("/api/console/runbooks/x", headers=h,
              json={"title": "T", "content_md": "c"})
    assert r.status_code == 403


def test_rbac_admin_writes(seeded):
    c = make_client(CfgSecured())
    h = {"X-VK-Token": "admin-tok"}
    r = c.put("/api/console/runbooks/x", headers=h,
              json={"title": "T", "content_md": "c"})
    assert r.status_code == 200


def test_rbac_bad_token_rejected(seeded):
    c = make_client(CfgSecured())
    assert c.get("/api/console/overview",
                 headers={"X-VK-Token": "wrong"}).status_code == 401


# ── Event bus ─────────────────────────────────────────────────────────────────

def test_eventbus_inprocess_roundtrip():
    from vishwakarma.core import eventbus
    eventbus.init_eventbus("")          # in-process mode
    q = eventbus.subscribe()
    try:
        eventbus.publish("inc-9", {"type": "tool_call_start", "tool": "bash"})
        evt = q.get(timeout=1)
        assert evt["incident_id"] == "inc-9" and evt["tool"] == "bash"
    finally:
        eventbus.unsubscribe(q)
    # after unsubscribe, publishes don't reach the queue
    eventbus.publish("inc-9", {"type": "done"})
    assert q.empty()
