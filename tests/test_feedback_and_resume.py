import sys
import tempfile
import threading
from types import SimpleNamespace

import pytest

from vishwakarma.core.rca_text import clean_rca


def test_clean_rca_strips_narration_before_first_heading():
    text = "All evidence is in. Writing the final RCA.\n\n---\n\n## Alert Timeline\n- fired"
    assert clean_rca(text) == "## Alert Timeline\n- fired"


def test_clean_rca_leaves_text_starting_with_heading():
    assert clean_rca("## Root Cause\nx") == "## Root Cause\nx"


def test_clean_rca_leaves_text_without_headings():
    assert clean_rca("  plain answer  ") == "plain answer"


def test_clean_rca_keeps_long_preamble_that_may_be_real_content():
    text = ("word " * 400) + "\n## Root Cause\nx"
    assert clean_rca(text) == text.strip()


def test_clean_rca_keeps_text_when_preamble_has_its_own_heading():
    text = "# Title\nintro\n## Root Cause\nx"
    assert clean_rca(text) == text


def _plugin():
    from vishwakarma.plugins.relays.xyne import plugin
    return plugin


def _feedback_blocks(incident_id="inc-1"):
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Was this RCA accurate?*"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Correct"}, "style": "primary",
             "action_id": "vk_rca_correct", "value": incident_id},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ Wrong"}, "style": "danger",
             "action_id": "vk_rca_wrong", "value": incident_id},
        ]},
    ]


def test_feedback_buttons_become_a_xyne_card_with_incident_in_action_id():
    flow = _plugin().blocks_to_flow(_feedback_blocks("inc-9"))
    assert flow["screenId"] == "argus-feedback"
    row = [c for c in flow["components"] if c["type"] == "row"][0]
    ids = [b["props"]["action"]["actionId"] for b in row["children"]]
    assert ids == ["vk_rca_correct:inc-9", "vk_rca_wrong:inc-9"]
    assert [b["props"]["variant"] for b in row["children"]] == ["primary", "destructive"]


def test_remediation_card_keeps_its_own_screen():
    blocks = [{"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": "Approve"},
         "action_id": "vk_remediate_approve", "value": "r1"}]}]
    assert _plugin().blocks_to_flow(blocks)["screenId"] == "argus-remediation"


def _config():
    return SimpleNamespace(xyne_base_url="https://xyne.test", xyne_bot_token="tok")


def _click(action_id):
    return {"actionId": action_id, "type": "submit", "messageId": "m-1", "conversationId": "c-1",
            "context": {"userId": "u-1"}}


def test_flow_action_ignores_other_verbs():
    from vishwakarma.core import feedback
    assert feedback.handle_flow_action(_config(), _click("vk_remediate_approve:x"), lambda u: ("", "")) is None
    assert feedback.handle_flow_action(_config(), _click("vk_rca_correct:"), lambda u: ("", "")) is None


@pytest.mark.parametrize("verb,fn", [("vk_rca_correct", "record_correct"), ("vk_rca_wrong", "record_wrong")])
def test_flow_action_runs_feedback_and_updates_card(monkeypatch, verb, fn):
    from vishwakarma.core import feedback
    from vishwakarma.plugins.relays.xyne import plugin

    done = threading.Event()
    calls = []
    updates = []
    monkeypatch.setattr(feedback, fn, lambda config, incident_id, *a: (
        calls.append(incident_id), {"status": "saved", "category": "redis", "pattern_saved": False,
                                    "runbooks_penalized": 1})[1])

    def fake_update(self, channel, ts, text="", blocks=None, flow=None, **kw):
        updates.append((channel, ts, text))
        if "Saving" not in text:
            done.set()
        return {"ok": True}

    monkeypatch.setattr(plugin.XyneWebClient, "chat_update", fake_update)
    resp = feedback.handle_flow_action(_config(), _click(f"{verb}:inc-1"), lambda u: ("Asha", "a@x.com"))
    assert resp["type"] == "ack"
    assert done.wait(5)
    assert calls == ["inc-1"]
    assert updates[0][2].startswith("⏳")
    assert updates[-1][0] == "c-1" and updates[-1][1] == "flow:m-1"
    assert "Asha" in updates[-1][2]


def test_flow_action_ignores_double_click(monkeypatch):
    from vishwakarma.core import feedback
    from vishwakarma.plugins.relays.xyne import plugin

    release = threading.Event()
    started = threading.Event()

    def slow(config, incident_id):
        started.set()
        release.wait(5)
        return {"status": "saved", "category": "c", "pattern_saved": False}

    monkeypatch.setattr(feedback, "record_correct", slow)
    monkeypatch.setattr(plugin.XyneWebClient, "chat_update", lambda self, *a, **k: {"ok": True})
    first = feedback.handle_flow_action(_config(), _click("vk_rca_correct:inc-2"), lambda u: ("", ""))
    assert started.wait(5)
    second = feedback.handle_flow_action(_config(), _click("vk_rca_correct:inc-2"), lambda u: ("", ""))
    release.set()
    assert first["message"] == "Saving feedback…"
    assert second["message"] == "Already saving this feedback."


def test_flow_action_reports_failure_on_card(monkeypatch):
    from vishwakarma.core import feedback
    from vishwakarma.plugins.relays.xyne import plugin

    done = threading.Event()
    texts = []

    def boom(config, incident_id):
        raise RuntimeError("db down")

    def fake_update(self, channel, ts, text="", **kw):
        texts.append(text)
        if "failed" in text:
            done.set()
        return {"ok": True}

    monkeypatch.setattr(feedback, "record_correct", boom)
    monkeypatch.setattr(plugin.XyneWebClient, "chat_update", fake_update)
    feedback.handle_flow_action(_config(), _click("vk_rca_correct:inc-3"), lambda u: ("", ""))
    assert done.wait(5)


def test_record_wrong_without_cause_penalizes_runbooks_and_invalidates_patterns(monkeypatch):
    from vishwakarma.core import feedback
    from vishwakarma.storage import patterns, queries, runbooks

    invalidated, penalized = [], []
    monkeypatch.setattr(queries, "get_incident", lambda i: {
        "labels": {"alertname": "A"}, "meta": {"matched_runbook_ids": ["rb1", "rb2"]}, "analysis": "x"})
    monkeypatch.setattr(patterns, "invalidate_patterns_for_incident", lambda i: invalidated.append(i))
    monkeypatch.setattr(runbooks, "mark_runbook_miss", lambda r: penalized.append(r))
    result = feedback.record_wrong(SimpleNamespace(), "inc-1")
    assert result == {"status": "saved", "category": "", "runbooks_penalized": 2}
    assert invalidated == ["inc-1"] and penalized == ["rb1", "rb2"]


def test_record_correct_reports_missing_incident(monkeypatch):
    from vishwakarma.core import feedback
    from vishwakarma.storage import queries
    monkeypatch.setattr(queries, "get_incident", lambda i: None)
    assert feedback.record_correct(SimpleNamespace(), "nope") == {"status": "missing"}


def _outcome(verb, result):
    from vishwakarma.core import feedback
    return feedback._outcome_text(verb, result, "Asha")


def test_outcome_text_variants():
    assert "Learning saved to redis" in _outcome("vk_rca_correct", {"status": "saved", "category": "redis"})
    assert "already captured" in _outcome("vk_rca_correct", {"status": "duplicate"})
    assert "Marked wrong by Asha" in _outcome("vk_rca_wrong", {"status": "saved", "runbooks_penalized": 0})
    assert "Could not find" in _outcome("vk_rca_wrong", {"status": "missing"})


def _fresh_db():
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]
    from vishwakarma.storage import db
    db.init_db(db_path=tempfile.mktemp(suffix=".db"))
    return db


def test_delivery_round_trips_through_investigation_row():
    _fresh_db()
    from vishwakarma.storage.investigations import create_investigation, find_orphaned, set_delivery
    from vishwakarma.storage.investigations import claim_investigation, _get_conn
    create_investigation("inc-1", alert_key="A")
    claim_investigation("inc-1", worker_id="pod-a")
    set_delivery("inc-1", {"channel": "C1", "thread_ts": "1.2", "xyne": {"channel": "x", "ts": "9"}})
    _get_conn().execute("UPDATE investigations SET heartbeat_at = 1 WHERE id = 'inc-1'")
    _get_conn().commit()
    orphan = find_orphaned(stale_seconds=10)[0]
    assert orphan["delivery"]["thread_ts"] == "1.2"
    assert orphan["delivery"]["xyne"]["ts"] == "9"


def test_migration_adds_delivery_column_to_existing_table():
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]
    import sqlite3
    path = tempfile.mktemp(suffix=".db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE investigations (id TEXT PRIMARY KEY, alert_key TEXT, cloud TEXT, status TEXT, "
                "phase TEXT, step INTEGER, messages TEXT, findings TEXT, code_session TEXT, attempt INTEGER, "
                "worker_id TEXT, heartbeat_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    old.commit()
    old.close()
    from vishwakarma.storage import db
    db.init_db(db_path=path)
    cols = [r[1] for r in db._get_conn().execute("PRAGMA table_info(investigations)").fetchall()]
    assert "delivery" in cols
    db.init_db(db_path=path)


def test_resume_context_prefers_delivery_over_incident_row():
    from vishwakarma import server
    inv = {"alert_key": "FarePolicyNotFound", "delivery": {
        "title": "[CRITICAL] FarePolicyNotFound in GCP", "channel": "C9", "thread_ts": "5.5",
        "labels": {"alertname": "FarePolicyNotFound"}, "matched_runbook_ids": ["rb"], "severity": "critical"}}
    ctx = server._resume_context(inv, {"title": "Resumed RCA", "labels": {"x": "1"}})
    assert ctx["title"] == "[CRITICAL] FarePolicyNotFound in GCP"
    assert ctx["channel"] == "C9" and ctx["thread_ts"] == "5.5"
    assert ctx["labels"] == {"x": "1", "alertname": "FarePolicyNotFound"}
    assert ctx["runbook_ids"] == ["rb"] and ctx["severity"] == "critical"


def test_resume_context_falls_back_to_alert_key_when_nothing_saved():
    from vishwakarma import server
    ctx = server._resume_context({"alert_key": "FarePolicyNotFound"}, {})
    assert ctx["title"] == "FarePolicyNotFound"
    assert ctx["channel"] == "" and ctx["thread_ts"] is None


class _FakeDest:
    def __init__(self):
        self.posted = []
        self.updated = []
        self.client = SimpleNamespace(chat_update=lambda **kw: self.updated.append(kw))

    def _get_client(self):
        return self.client

    def post_investigation(self, **kw):
        self.posted.append(kw)
        return {"ts": "77.1"}


def test_resumed_rca_is_posted_to_original_thread_with_pdf(monkeypatch):
    from vishwakarma import server
    from vishwakarma.bot import pdf

    dest = _FakeDest()
    monkeypatch.setattr(server, "_make_destination", lambda config, labels: dest)
    monkeypatch.setattr(server, "_destination_configured", lambda config, labels: True)
    monkeypatch.setattr(pdf, "generate_pdf", lambda **kw: "/tmp/x.pdf")
    ctx = {"title": "T", "severity": "critical", "source": "alertmanager", "labels": {}, "channel": "C1",
           "thread_ts": "1.2", "runbook_ids": ["rb"], "xyne": {}}
    pdf_path, slack_ts = server._deliver_resumed_rca(_config(), ctx, "inc-1", "## Root Cause\nx")
    assert (pdf_path, slack_ts) == ("/tmp/x.pdf", "77.1")
    assert dest.posted[0]["thread_ts"] == "1.2" and dest.posted[0]["channel"] == "C1"
    assert dest.posted[0]["pdf_path"] == "/tmp/x.pdf" and dest.posted[0]["incident_id"] == "inc-1"
    assert dest.updated and dest.updated[0]["ts"] == "1.2"


def test_resumed_rca_without_target_is_not_posted(monkeypatch):
    from vishwakarma import server
    from vishwakarma.bot import pdf

    dest = _FakeDest()
    monkeypatch.setattr(server, "_make_destination", lambda config, labels: dest)
    monkeypatch.setattr(server, "_destination_configured", lambda config, labels: True)
    monkeypatch.setattr(pdf, "generate_pdf", lambda **kw: None)
    ctx = {"title": "T", "severity": "high", "source": "", "labels": {}, "channel": "",
           "thread_ts": None, "runbook_ids": [], "xyne": {}}
    assert server._deliver_resumed_rca(_config(), ctx, "inc-1", "x") == (None, None)
    assert dest.posted == []


def test_resumed_rca_mirrors_to_xyne_thread(monkeypatch):
    from vishwakarma import server
    from vishwakarma.bot import pdf
    from vishwakarma.plugins.relays.xyne import plugin

    dest = _FakeDest()
    mirrored = []

    class _Mirror:
        def post_investigation(self, primary_ts, **kw):
            mirrored.append((primary_ts, kw))

    monkeypatch.setattr(server, "_make_destination", lambda config, labels: dest)
    monkeypatch.setattr(server, "_destination_configured", lambda config, labels: True)
    monkeypatch.setattr(pdf, "generate_pdf", lambda **kw: "/tmp/x.pdf")
    monkeypatch.setattr(plugin, "restore_alert_mirror", lambda config, ch, pts, xts: _Mirror())
    ctx = {"title": "T", "severity": "high", "source": "", "labels": {}, "channel": "C1",
           "thread_ts": "1.2", "runbook_ids": [], "xyne": {"channel": "XC", "ts": "X9"}}
    server._deliver_resumed_rca(_config(), ctx, "inc-1", "x")
    assert mirrored and mirrored[0][0] == "1.2" and mirrored[0][1]["pdf_path"] == "/tmp/x.pdf"


def test_restore_alert_mirror_maps_primary_thread_to_xyne_thread():
    mirror = _plugin().restore_alert_mirror(_config(), "XC", "1.2", "X9")
    assert mirror.xyne_target("1.2") == ("XC", "X9")
    assert _plugin().restore_alert_mirror(_config(), "", "1.2", "X9") is None


def _xyne_app(monkeypatch):
    from fastapi.testclient import TestClient
    from vishwakarma import server
    from vishwakarma.config import VishwakarmaConfig

    config = VishwakarmaConfig({"xyne": {"webhook_token": "tok", "base_url": "https://xyne.test", "bot_token": "b"}})
    app = server.create_app(config)
    server._state["xyne_bot"] = object()
    return TestClient(app), server


def test_webhook_routes_feedback_click_to_feedback_handler(monkeypatch):
    from vishwakarma.core import feedback, remediation

    client, _ = _xyne_app(monkeypatch)
    seen = {}
    monkeypatch.setattr(feedback, "handle_flow_action",
                        lambda config, payload, lookup: seen.update(payload=payload) or {"type": "ack", "message": "Saving feedback…"})
    monkeypatch.setattr(remediation, "handle_flow_action", lambda *a, **k: pytest.fail("remediation handler used"))
    r = client.post("/api/xyne/events?auth_token=tok", json=_click("vk_rca_wrong:inc-5"))
    assert r.status_code == 200
    assert r.json() == {"type": "ack", "message": "Saving feedback…"}
    assert seen["payload"]["actionId"] == "vk_rca_wrong:inc-5"


def test_webhook_still_routes_remediation_click_to_remediation_handler(monkeypatch):
    from vishwakarma.core import feedback, remediation

    client, _ = _xyne_app(monkeypatch)
    monkeypatch.setattr(feedback, "handle_flow_action", lambda *a, **k: pytest.fail("feedback handler used"))
    monkeypatch.setattr(remediation, "handle_flow_action",
                        lambda config, payload, lookup: {"type": "ack", "message": "executed"})
    r = client.post("/api/xyne/events?auth_token=tok", json=_click("vk_remediate_approve:r1"))
    assert r.json() == {"type": "ack", "message": "executed"}


def test_webhook_rejects_bad_token(monkeypatch):
    client, _ = _xyne_app(monkeypatch)
    assert client.post("/api/xyne/events?auth_token=wrong", json=_click("vk_rca_correct:i")).status_code == 401
