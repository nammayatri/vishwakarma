"""Amendment proposal storage — ❌ feedback → proposed runbook edits, human-gated."""
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
    return dbmod


def test_proposal_roundtrip(db):
    from vishwakarma.storage import runbooks as rb
    rb.save_runbook("rb1", "T", "orig")
    pid = rb.save_proposal("rb1", "inc-1", "amended body", "RCA disagreed")
    rows = rb.list_proposals()
    assert len(rows) == 1 and rows[0]["runbook_id"] == "rb1"
    rb.delete_proposal(pid)
    assert rb.list_proposals() == []
