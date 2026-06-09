"""
Phase 4a tests — cloud router, Redis Streams job transport, and the
orchestrator→executor dispatch semantics.

Redis tests use scratch db 14 (flushed per run); skipped when Redis is
absent.

Run:  pytest tests/test_phase4_dispatch.py -v
"""
import time
import uuid

import pytest

from vishwakarma.core.cloud_router import route_issue
from vishwakarma.core.issue import Issue

REDIS_URL = "redis://localhost:6379/14"


def _redis_available() -> bool:
    try:
        import redis
        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


needs_redis = pytest.mark.skipif(not _redis_available(), reason="local Redis not available")


def issue(title="T", source="alertmanager", **labels) -> Issue:
    return Issue(id=str(uuid.uuid4()), title=title, source=source, labels=labels)


# ── Cloud router ───────────────────────────────────────────────────────────────

def test_route_explicit_label_wins():
    assert route_issue(issue(cloud="gcp", region="ap-south-1")) == "gcp"
    assert route_issue(issue(cloud="both")) == "both"


def test_route_aws_signals():
    assert route_issue(issue(region="ap-south-1")) == "aws"
    assert route_issue(issue(aws_account="123456789012")) == "aws"
    assert route_issue(issue(cluster="eks-cluster")) == "aws"
    assert route_issue(issue(source="cloudwatch")) == "aws"


def test_route_gcp_signals():
    assert route_issue(issue(region="asia-south1")) == "gcp"
    assert route_issue(issue(project_id="prod-project")) == "gcp"
    assert route_issue(issue(cluster="gke-cluster")) == "gcp"


def test_route_cross_cloud_both():
    # signals from both clouds → fan out
    assert route_issue(issue(cluster="eks-cluster", project_id="prod-project")) == "both"


def test_route_default_when_no_signal():
    assert route_issue(issue(), default_cloud="aws") == "aws"
    assert route_issue(issue(), default_cloud="gcp") == "gcp"
    assert route_issue(issue(), default_cloud="bogus") == "aws"  # sanitized


# ── Job stream ─────────────────────────────────────────────────────────────────

@needs_redis
class TestJobStream:
    @pytest.fixture(autouse=True)
    def _stream(self):
        import redis as redis_lib
        redis_lib.Redis.from_url(REDIS_URL).flushdb()
        from vishwakarma.core import jobstream
        jobstream.init_jobstream(REDIS_URL)
        self.js = jobstream
        yield

    def test_enqueue_consume_ack(self):
        self.js.enqueue("aws", {"incident_id": "i-1", "x": 1})
        got = self.js.consume("aws", "c1", block_ms=500)
        assert got is not None
        msg_id, payload = got
        assert payload["incident_id"] == "i-1"
        assert self.js.pending_count("aws") == 1
        self.js.ack("aws", msg_id)
        assert self.js.pending_count("aws") == 0
        # nothing left
        assert self.js.consume("aws", "c1", block_ms=200) is None

    def test_cloud_isolation(self):
        self.js.enqueue("gcp", {"incident_id": "i-gcp"})
        assert self.js.consume("aws", "c1", block_ms=200) is None
        got = self.js.consume("gcp", "g1", block_ms=500)
        assert got is not None and got[1]["incident_id"] == "i-gcp"
        self.js.ack("gcp", got[0])

    def test_both_fans_out(self):
        ids = self.js.enqueue("both", {"incident_id": "i-both"})
        assert len(ids) == 2
        a = self.js.consume("aws", "c1", block_ms=500)
        g = self.js.consume("gcp", "g1", block_ms=500)
        assert a[1]["incident_id"] == "i-both" and g[1]["incident_id"] == "i-both"
        self.js.ack("aws", a[0]); self.js.ack("gcp", g[0])

    def test_claim_stale_recovers_dead_consumer(self):
        self.js.enqueue("aws", {"incident_id": "i-dead"})
        got = self.js.consume("aws", "dead-consumer", block_ms=500)
        assert got is not None
        # dead-consumer never acks. Another consumer reclaims after idle.
        time.sleep(0.05)
        reclaimed = self.js.claim_stale("aws", "rescuer", min_idle_ms=10)
        assert any(p["incident_id"] == "i-dead" for _, p in reclaimed)
        # rescuer acks; pending drains
        for msg_id, _ in reclaimed:
            self.js.ack("aws", msg_id)
        assert self.js.pending_count("aws") == 0

    def test_depth(self):
        assert self.js.depth("aws") == 0
        self.js.enqueue("aws", {"incident_id": "d1"})
        self.js.enqueue("aws", {"incident_id": "d2"})
        assert self.js.depth("aws") == 2


# ── Executor job handling (stubbed investigation) ─────────────────────────────

@needs_redis
def test_executor_runs_job_and_acks(monkeypatch, tmp_path):
    """End-to-end: enqueue → executor consumes → (stubbed) investigation → ack."""
    import sys
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]
    import redis as redis_lib
    redis_lib.Redis.from_url(REDIS_URL).flushdb()

    from vishwakarma.storage import db as dbmod
    dbmod.init_db(db_path=str(tmp_path / "t.db"))
    from vishwakarma.core import jobstream
    jobstream.init_jobstream(REDIS_URL)

    ran = {}

    async def fake_do_investigation(config, state, iss, incident_id, fingerprint,
                                    cross_cloud="", cross_cloud_base=""):
        ran["incident_id"] = incident_id
        ran["title"] = iss.title
        from vishwakarma.storage.investigations import (
            create_investigation, finish_investigation)
        create_investigation(incident_id)
        finish_investigation(incident_id, "done")

    import vishwakarma.server as server_mod
    monkeypatch.setattr(server_mod, "_do_investigation", fake_do_investigation)

    from vishwakarma.executor import Executor

    class Cfg:  # minimal config stub
        db_path = str(tmp_path / "t.db")
        pg_dsn = ""
        redis_url = REDIS_URL

    ex = Executor.__new__(Executor)  # skip full start() — drive _run_job directly
    ex.config = Cfg()
    ex.cloud = "aws"
    ex.consumer = "test-exec"
    ex._state = {}

    iss = issue(title="RDS CPU High", region="ap-south-1")
    import json as _json
    incident_id = str(uuid.uuid4())
    jobstream.enqueue("aws", {
        "incident_id": incident_id, "fingerprint": "fp-1", "cloud": "aws",
        "issue": _json.loads(iss.model_dump_json()),
    })

    msg_id, payload = jobstream.consume("aws", "test-exec", block_ms=500)
    ex._run_job(msg_id, payload)

    assert ran["incident_id"] == incident_id and ran["title"] == "RDS CPU High"
    assert jobstream.pending_count("aws") == 0  # acked

    # duplicate delivery of a done investigation: acked without re-running
    ran.clear()
    jobstream.enqueue("aws", payload)
    msg_id2, payload2 = jobstream.consume("aws", "test-exec", block_ms=500)
    ex._run_job(msg_id2, payload2)
    assert ran == {}  # not re-run
    assert jobstream.pending_count("aws") == 0


@needs_redis
def test_executor_both_job_uses_suffixed_id_and_cross_cloud(monkeypatch, tmp_path):
    """A `both`-tagged job → cloud-suffixed tracking id + cross_cloud set."""
    import sys
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]
    import redis as redis_lib
    redis_lib.Redis.from_url(REDIS_URL).flushdb()
    from vishwakarma.storage import db as dbmod
    dbmod.init_db(db_path=str(tmp_path / "t.db"))
    from vishwakarma.core import jobstream
    jobstream.init_jobstream(REDIS_URL)

    seen = {}

    async def fake_do_investigation(config, state, iss, incident_id, fingerprint,
                                    cross_cloud="", cross_cloud_base=""):
        seen["incident_id"] = incident_id
        seen["cross_cloud"] = cross_cloud
        seen["base"] = cross_cloud_base

    import vishwakarma.server as server_mod
    monkeypatch.setattr(server_mod, "_do_investigation", fake_do_investigation)

    from vishwakarma.executor import Executor

    class Cfg:
        db_path = str(tmp_path / "t.db"); pg_dsn = ""; redis_url = REDIS_URL

    ex = Executor.__new__(Executor)
    ex.config = Cfg(); ex.cloud = "gcp"; ex.consumer = "g1"; ex._state = {}

    jobstream.enqueue("gcp", {
        "incident_id": "inc-both", "fingerprint": "fp", "cloud": "both",
        "issue": {"id": "x", "title": "Drainer lag", "source": "alertmanager", "labels": {}},
    })
    msg_id, payload = jobstream.consume("gcp", "g1", block_ms=500)
    ex._run_job(msg_id, payload)

    assert seen["incident_id"] == "inc-both:gcp"   # suffixed so halves don't collide
    assert seen["cross_cloud"] == "gcp"
    assert seen["base"] == "inc-both"


@needs_redis
def test_executor_bad_payload_dropped(tmp_path):
    import redis as redis_lib
    redis_lib.Redis.from_url(REDIS_URL).flushdb()
    from vishwakarma.core import jobstream
    jobstream.init_jobstream(REDIS_URL)

    from vishwakarma.executor import Executor

    class Cfg:
        db_path = str(tmp_path / "t.db"); pg_dsn = ""; redis_url = REDIS_URL

    ex = Executor.__new__(Executor)
    ex.config = Cfg(); ex.cloud = "aws"; ex.consumer = "t"; ex._state = {}

    jobstream.enqueue("aws", {"incident_id": "x", "issue": {"not": "an issue"}})
    msg_id, payload = jobstream.consume("aws", "t", block_ms=500)
    ex._run_job(msg_id, payload)              # must not raise
    assert jobstream.pending_count("aws") == 0  # dropped + acked
