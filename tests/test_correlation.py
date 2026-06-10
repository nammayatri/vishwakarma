"""
Incident correlation tests — key derivation, link/find/unlink, grouped-alert
recording, and the conservative "don't group when vague" rule.

Run:  pytest tests/test_correlation.py -v
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
    from vishwakarma.core import correlation
    correlation.init_correlation("")   # in-memory
    # clear any cross-test in-memory state
    correlation._local.clear()
    return dbmod


def test_correlation_key():
    from vishwakarma.core.correlation import correlation_key as k
    assert k({"service": "DriverOffer", "namespace": "atlas"}) == "svc:driveroffer/atlas"
    assert k({"app": "drainer"}) == "svc:drainer"
    assert k({"cluster": "eks-cluster"}) == "cluster:eks-cluster"
    assert k({"severity": "critical"}) == ""        # too vague → never correlate


def test_link_find_unlink(db):
    from vishwakarma.core import correlation as c
    key = "svc:driveroffer/atlas"
    assert c.find_correlated(key) is None
    c.link(key, "inc-1")
    assert c.find_correlated(key) == "inc-1"
    c.unlink(key)
    assert c.find_correlated(key) is None


def test_empty_key_never_correlates(db):
    from vishwakarma.core import correlation as c
    c.link("", "inc-x")
    assert c.find_correlated("") is None


def test_alertname_stem_fallback_groups_label_less_alerts():
    """Real case: drainer alerts with only alertname (no svc/ns) must group."""
    from vishwakarma.core.correlation import correlation_key as k
    a = k({"alertname": "GCPDriverDrainerNotProcessing", "severity": "critical"})
    b = k({"alertname": "GCPNoDriverDrainerRunning", "severity": "critical"})
    assert a == b == "alert:drainer+driver"      # grouped via stem
    assert k({"alertname": "RedisHighCPU"}) != k({"alertname": "RedisHighMemory"})
    assert k({"alertname": "Down"}) == ""        # single generic token → no group
    assert k({"service": "driver", "namespace": "atlas",
              "alertname": "Whatever"}) == "svc:driver/atlas"   # labels win


def test_record_and_list_correlated(db):
    from vishwakarma.core import correlation as c
    c.record_correlated_alert("inc-1", "High latency", {"service": "driver"})
    c.record_correlated_alert("inc-1", "DB connections high", {"service": "driver"})
    c.record_correlated_alert("inc-2", "unrelated", {})
    rows = c.list_correlated("inc-1")
    assert [r["alert_title"] for r in rows] == ["High latency", "DB connections high"]
    assert rows[0]["alert_labels"]["service"] == "driver"
    assert len(c.list_correlated("inc-2")) == 1


def test_storm_scenario(db):
    """
    First alert starts an investigation + links the entity; subsequent
    DIFFERENT alerts on the same entity correlate into it.
    """
    from vishwakarma.core import correlation as c
    labels = {"service": "driver-offer", "namespace": "atlas"}
    key = c.correlation_key(labels)

    # alert 1 — no active investigation → would start one
    assert c.find_correlated(key) is None
    c.link(key, "inc-storm")           # investigation started

    # alerts 2-4 (different alertnames, same entity) → correlate
    for title in ("5xx spike", "latency high", "pod OOMKilled"):
        parent = c.find_correlated(key)
        assert parent == "inc-storm"
        c.record_correlated_alert(parent, title, labels)

    assert len(c.list_correlated("inc-storm")) == 3

    # investigation done → window closed → next alert starts fresh
    c.unlink(key)
    assert c.find_correlated(key) is None


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("redis"),
    reason="redis lib not installed",
)
def test_redis_backend_if_available(db):
    import redis as redis_lib
    try:
        redis_lib.Redis.from_url("redis://localhost:6379/13",
                                 socket_connect_timeout=1).ping()
    except Exception:
        pytest.skip("local Redis not available")
    from vishwakarma.core import correlation as c
    redis_lib.Redis.from_url("redis://localhost:6379/13").flushdb()
    c.init_correlation("redis://localhost:6379/13")
    c.link("svc:x/y", "inc-r", ttl=60)
    assert c.find_correlated("svc:x/y") == "inc-r"
    # visible to another client (cross-pod)
    other = redis_lib.Redis.from_url("redis://localhost:6379/13", decode_responses=True)
    assert other.get("vk:corr:svc:x/y") == "inc-r"
    c.unlink("svc:x/y")
    assert c.find_correlated("svc:x/y") is None
    c.init_correlation("")   # reset for other tests
