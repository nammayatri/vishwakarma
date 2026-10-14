"""Evidence-snapshot outcome tracking — mark_evidence_correct/wrong must persist
even when fast-RCA (the only caller of store_evidence) never created a row."""
import sys
import tempfile

import pytest


def _reset():
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]


@pytest.fixture()
def db():
    _reset()
    from vishwakarma.storage import db as dbmod
    dbmod.init_db(db_path=tempfile.mktemp(suffix=".db"))
    from vishwakarma.storage.evidence import init_evidence
    init_evidence()
    return dbmod


def test_mark_correct_creates_a_row_when_none_exists(db):
    from vishwakarma.storage.evidence import mark_evidence_correct
    from vishwakarma.storage.db import _get_conn
    mark_evidence_correct("inc-1", alert_name="AllocatorJobPickupDelayHigh")
    row = _get_conn().execute(
        "SELECT outcome, alert_name FROM evidence_snapshots WHERE incident_id = ?",
        ("inc-1",)).fetchone()
    assert row is not None
    assert dict(row)["outcome"] == "correct"
    assert dict(row)["alert_name"] == "AllocatorJobPickupDelayHigh"


def test_mark_wrong_creates_a_row_when_none_exists(db):
    from vishwakarma.storage.evidence import mark_evidence_wrong
    from vishwakarma.storage.db import _get_conn
    mark_evidence_wrong("inc-2", alert_name="SomeAlert")
    row = _get_conn().execute(
        "SELECT outcome FROM evidence_snapshots WHERE incident_id = ?",
        ("inc-2",)).fetchone()
    assert dict(row)["outcome"] == "wrong"


def test_mark_correct_updates_existing_row_instead_of_duplicating(db):
    from vishwakarma.storage.evidence import store_evidence, mark_evidence_correct
    from vishwakarma.storage.db import _get_conn
    store_evidence("ev-1", "SomeAlert", {}, incident_id="inc-3", outcome="pending")
    mark_evidence_correct("inc-3", alert_name="SomeAlert")
    rows = _get_conn().execute(
        "SELECT outcome FROM evidence_snapshots WHERE incident_id = ?",
        ("inc-3",)).fetchall()
    assert len(rows) == 1
    assert dict(rows[0])["outcome"] == "correct"
