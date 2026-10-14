"""Per-thread conversation_id continuity for ny-infra-gpt."""
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


def test_no_conversation_yet_returns_none(db):
    from vishwakarma.storage.infra_gpt import get_conversation_id
    assert get_conversation_id("C1", "111.222") is None


def test_save_then_get_round_trips(db):
    from vishwakarma.storage.infra_gpt import save_conversation_id, get_conversation_id
    save_conversation_id("C1", "111.222", 45)
    assert get_conversation_id("C1", "111.222") == 45


def test_different_threads_stay_independent(db):
    from vishwakarma.storage.infra_gpt import save_conversation_id, get_conversation_id
    save_conversation_id("C1", "111.222", 45)
    save_conversation_id("C1", "333.444", 99)
    assert get_conversation_id("C1", "111.222") == 45
    assert get_conversation_id("C1", "333.444") == 99


def test_save_again_updates_not_duplicates(db):
    from vishwakarma.storage.infra_gpt import save_conversation_id, get_conversation_id
    from vishwakarma.storage.db import _get_conn
    save_conversation_id("C1", "111.222", 45)
    save_conversation_id("C1", "111.222", 46)
    assert get_conversation_id("C1", "111.222") == 46
    rows = _get_conn().execute("SELECT * FROM infra_gpt_conversations").fetchall()
    assert len(rows) == 1
