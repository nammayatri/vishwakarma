"""
Tests for the gap-closure modules: key pool, fix scorer, per-cloud knowledge,
audit log, metrics, and Argus image-payload shape.

Run:  pytest tests/test_gap_closure.py -v
"""
import sys
import tempfile
from pathlib import Path

import pytest


def _reset():
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]


# ── Key pool ──────────────────────────────────────────────────────────────────

def test_keypool_round_robin_and_bench():
    from vishwakarma.core.keypool import KeyPool
    p = KeyPool(["a", "b", "c"])
    assert [p.get() for _ in range(4)] == ["a", "b", "c", "a"]
    p.penalize("b", seconds=60)
    got = [p.get() for _ in range(4)]
    assert "b" not in got
    assert set(got) == {"a", "c"}


def test_keypool_single_and_empty():
    from vishwakarma.core.keypool import KeyPool
    assert KeyPool([]).get() is None
    assert KeyPool(["only"]).get() == "only"


def test_keypool_all_benched_returns_something():
    from vishwakarma.core.keypool import KeyPool
    p = KeyPool(["a", "b"])
    p.penalize("a"); p.penalize("b")
    assert p.get() in ("a", "b")   # better than None


# ── Fix scorer ────────────────────────────────────────────────────────────────

def test_fix_scorer_draft_pr_path():
    from vishwakarma.core.fix_scorer import score_fix
    d = score_fix("HIGH", exact_line_found=True, pattern_matched=True,
                  diff_files=1, diff_lines=10, tests_passed=True)
    assert d.action == "draft_pr" and d.confidence == "HIGH"


def test_fix_scorer_never_pr_unvalidated():
    from vishwakarma.core.fix_scorer import score_fix
    # high score but tests not run → must not PR
    d = score_fix("HIGH", exact_line_found=True, pattern_matched=True,
                  diff_files=1, diff_lines=10, tests_passed=None)
    assert d.action == "propose_only"
    # tests failed → must not PR
    d2 = score_fix("HIGH", exact_line_found=True, diff_files=1, diff_lines=10,
                   tests_passed=False)
    assert d2.action == "propose_only"


def test_fix_scorer_broad_diff_blocked():
    from vishwakarma.core.fix_scorer import score_fix
    d = score_fix("HIGH", exact_line_found=True, pattern_matched=True,
                  diff_files=30, diff_lines=2000, tests_passed=True)
    assert d.action == "propose_only"


def test_fix_scorer_low_confidence():
    from vishwakarma.core.fix_scorer import score_fix
    d = score_fix("LOW", diff_files=1, diff_lines=5)
    assert d.confidence == "LOW" and d.action == "propose_only"


# ── Per-cloud knowledge ───────────────────────────────────────────────────────

def test_per_cloud_knowledge(tmp_path):
    from vishwakarma.config import _load_knowledge
    base = tmp_path / "knowledge.md"
    base.write_text("GENERIC")
    (tmp_path / "knowledge-aws.md").write_text("AWS-SPECIFIC")
    assert _load_knowledge(str(base), cloud="aws") == "AWS-SPECIFIC"
    assert _load_knowledge(str(base), cloud="gcp") == "GENERIC"   # fallback
    assert _load_knowledge(str(base)) == "GENERIC"
    assert _load_knowledge(str(tmp_path / "nope.md"), cloud="aws") == ""


# ── Metrics ───────────────────────────────────────────────────────────────────

def test_metrics_exposition():
    from vishwakarma.core import metrics
    metrics.inc("vk_investigations_started_total", labels={"cloud": "aws"})
    metrics.inc("vk_investigations_started_total", labels={"cloud": "aws"})
    metrics.set_gauge("vk_queue_depth", 3, {"cloud": "gcp"})
    out = metrics.render()
    assert 'vk_investigations_started_total{cloud="aws"} 2.0' in out
    assert 'vk_queue_depth{cloud="gcp"} 3' in out
    assert "# TYPE vk_investigations_started_total counter" in out


# ── Audit log (both backends) ─────────────────────────────────────────────────

@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_audit_log(backend):
    if backend == "postgres":
        try:
            import psycopg2
            psycopg2.connect("postgresql://postgres@localhost:5432/postgres").close()
        except Exception:
            pytest.skip("local Postgres not available")
        import subprocess
        subprocess.run(["psql", "-h", "localhost", "-U", "postgres",
                        "-c", "DROP DATABASE IF EXISTS vk_test;",
                        "-c", "CREATE DATABASE vk_test;"], capture_output=True)
    _reset()
    from vishwakarma.storage import db as dbmod
    if backend == "sqlite":
        dbmod.init_db(db_path=tempfile.mktemp(suffix=".db"))
    else:
        dbmod.init_db(dsn="postgresql://postgres@localhost:5432/vk_test")
    from vishwakarma.storage.audit import audit, list_audit
    audit("admin", "runbook.save", "rds-cpu", {"title": "X"})
    audit("reader", "feedback", "inc-1", {"correct": True})
    rows = list_audit()
    assert len(rows) == 2
    assert rows[0]["action"] == "feedback"            # newest first
    assert rows[0]["detail"]["correct"] is True
    assert rows[1]["actor"] == "admin"


# ── Curated tool subset ───────────────────────────────────────────────────────

def test_tool_selection_by_domain():
    from vishwakarma.core.tool_selection import select_toolset_names, CORE_TOOLSETS
    avail = {"bash", "todo", "runbooks", "learnings", "code_analyst",
             "code_session", "prometheus", "grafana", "database", "mongodb",
             "elasticsearch", "kafka", "aws", "http", "internet"}

    # core always present
    rds = select_toolset_names("RDSCpuHigh rds cpu connection", avail)
    assert CORE_TOOLSETS <= rds
    assert "database" in rds and "aws" in rds and "prometheus" in rds
    assert "kafka" not in rds and "elasticsearch" not in rds

    # streaming keywords
    assert "kafka" in select_toolset_names("drainer consumer lag", avail)

    # logs keywords
    assert "elasticsearch" in select_toolset_names("exception stacktrace 500", avail)


def test_tool_selection_vague_returns_all():
    from vishwakarma.core.tool_selection import select_toolset_names
    avail = {"bash", "todo", "prometheus", "kafka"}
    assert select_toolset_names("MysteryAlert", avail) == avail


def test_tool_selection_intersects_available():
    from vishwakarma.core.tool_selection import select_toolset_names
    # database domain matched but database toolset not enabled → not included
    avail = {"bash", "todo", "runbooks", "learnings", "code_analyst",
             "code_session", "prometheus"}
    sel = select_toolset_names("postgres query connection", avail)
    assert "database" not in sel and "prometheus" not in sel  # prom not a db tool
    assert sel <= avail


def test_filter_openai_tools():
    from vishwakarma.core.tool_selection import filter_openai_tools
    from vishwakarma.core.tools import ToolExecutor
    from vishwakarma.plugins.toolsets.code_analyst.code_analyst import CodeAnalystToolset

    ts = CodeAnalystToolset({"repo_dir": "/tmp/x",
                             "repos": [{"name": "r", "url": "https://x/y.git"}]})
    ex = ToolExecutor(toolsets=[ts])
    specs = filter_openai_tools(ex, {"code_analyst"})
    names = {s["function"]["name"] for s in specs}
    assert "git_blame" in names and "repo_sync" in names
    # a toolset not in the subset yields nothing
    assert filter_openai_tools(ex, {"prometheus"}) == []


# ── Argus image payload ───────────────────────────────────────────────────────

def test_argus_payload_carries_image_urls():
    from vishwakarma.bot.argus import ArgusBot
    captured = {}
    bot = ArgusBot(mre_group_id="S1", bot_user_id="U1",
                   dispatch=lambda p: captured.update(p) or "inc",
                   classify=lambda t: True)
    bot.handle_message({
        "text": "<!subteam^S1> OA screen bug", "ts": "1.0", "channel": "C1",
        "user": "U2",
        "files": [{"mimetype": "image/png", "url_private": "https://files.slack/a.png"}],
    })
    assert captured["raw"]["image_urls"] == ["https://files.slack/a.png"]
