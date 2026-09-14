"""Resolution-source tracking — auto-resolve records who closed the incident."""
import json
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


def test_resolution_source_recorded(db):
    from vishwakarma.storage.queries import save_incident, resolve_incidents_by_labels, get_incident
    save_incident("i1", "RDS CPU", question="q", analysis="a", source="alertmanager",
                  labels={"alertname": "RDS-CPU", "namespace": "db"})
    n = resolve_incidents_by_labels({"alertname": "RDS-CPU", "namespace": "db"},
                                    source="alertmanager")
    assert n == 1
    meta = get_incident("i1")["meta"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta["resolution_source"] == "alertmanager"
