"""
Learned tool routing — effectiveness recording + selection bias.

Run:  pytest tests/test_tool_effectiveness.py -v
"""
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


def test_record_and_top(db):
    from vishwakarma.storage import tool_effectiveness as te
    # database helped this alert class 3 times, kafka once
    for _ in range(3):
        te.record_effective("rdscpu", {"database", "bash"})
    te.record_effective("rdscpu", {"kafka"})
    top = te.top_toolsets("rdscpu", min_hits=2)
    assert "database" in top and "bash" in top
    assert "kafka" not in top          # only 1 hit < min_hits=2


def test_top_empty_for_unknown(db):
    from vishwakarma.storage import tool_effectiveness as te
    assert te.top_toolsets("never-seen") == set()
    assert te.top_toolsets("") == set()


def test_tools_to_toolsets():
    from vishwakarma.storage.tool_effectiveness import tools_to_toolsets
    from vishwakarma.plugins.toolsets.code_analyst.code_analyst import CodeAnalystToolset
    from vishwakarma.plugins.toolsets.todo import TodoToolset
    ca = CodeAnalystToolset({"repo_dir": "/tmp", "repos": [{"name": "r", "url": "u"}]})
    todo = TodoToolset({})
    used = {"git_blame", "todo_write"}
    assert tools_to_toolsets(used, [ca, todo]) == {"code_analyst", "todo"}


def test_selection_includes_learned(db):
    from vishwakarma.core.tool_selection import select_toolset_names
    avail = {"bash", "todo", "runbooks", "learnings", "code_analyst",
             "code_session", "verify", "database", "prometheus", "kafka"}
    # vague alert that wouldn't match a domain, but learned says 'database'
    sel = select_toolset_names("MysteryAlertXYZ", avail, learned={"database"})
    assert "database" in sel
    # learned acts as a match signal → trimmed (not all), core present
    assert "kafka" not in sel and "bash" in sel


def test_selection_learned_filtered_by_available(db):
    from vishwakarma.core.tool_selection import select_toolset_names
    avail = {"bash", "todo", "runbooks", "learnings", "code_analyst",
             "code_session", "verify"}
    # learned 'database' but it's not enabled here → ignored, vague → all
    sel = select_toolset_names("MysteryAlert", avail, learned={"database"})
    assert sel == avail
