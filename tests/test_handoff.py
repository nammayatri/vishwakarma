"""Weekly handoff — pure builder + injectable run_once."""
from vishwakarma.scheduler.handoff import build_handoff, collect_week, run_once


ROWS = [
    {"id": "i1", "title": "RDS CPU prod", "status": "resolved",
     "analysis": "autovacuum", "created_at": 1 * 86400.0, "labels": "{}"},
    {"id": "i2", "title": "Redis evict", "status": "open",
     "analysis": "", "created_at": 8 * 86400.0, "labels": "{}"},
]


def test_collect_week_window():
    got = collect_week(ROWS, now=10 * 86400.0, window_s=7 * 86400)
    assert [r["id"] for r in got] == ["i2"]           # i1 older than 7d from now


def test_build_handoff_sections():
    md = build_handoff(ROWS)
    assert "## What happened" in md and "## What it means" in md and "## What to do" in md
    assert "RDS CPU prod" in md and "1" in md          # open-incident count rendered


def test_run_once_posts_via_injection():
    posted = []
    n = run_once(ROWS, summarize=lambda p: "SUMMARY", post_fn=posted.append)
    assert n is True and posted == ["SUMMARY"]
    assert run_once([], summarize=lambda p: "x", post_fn=posted.append) is False
