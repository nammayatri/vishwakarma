"""❌ feedback → proposed amendment (fakes for incident/runbook/llm)."""
from vishwakarma.core.amendment_proposals import propose_amendments


def test_propose_amendment_uses_fast_model():
    saved = {}
    calls = []
    propose_amendments(
        incident_id="i1", runbook_ids=["rb1"], corrected=False,
        fetch_incident=lambda i: {"title": "RDS", "analysis": "actual cause: pgbouncer"},
        get_runbook=lambda r: {"title": "T", "content_md": "old steps"},
        summarize=lambda prompt: calls.append(prompt) or "amended steps",
        save_proposal=lambda **kw: saved.update(kw) or "p1",
    )
    assert saved["runbook_id"] == "rb1" and saved["incident_id"] == "i1"
    assert "pgbouncer" in calls[0] and "old steps" in calls[0]


def test_correct_feedback_proposes_nothing():
    assert propose_amendments("i", ["rb"], corrected=True,
                              fetch_incident=None, get_runbook=None,
                              summarize=None, save_proposal=None) == []
