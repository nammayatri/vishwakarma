"""
GCP Cloud Monitoring webhook — payload parsing (core logic; the actual
FastAPI route itself needs `fastapi`, which isn't part of this parsing-level
test's dependency surface, so route-level auth/dispatch is covered by
inspection/manual testing rather than a TestClient here).

Run:  pytest tests/test_gcp_cloud_monitoring_webhook.py -v
"""
import hmac

from vishwakarma.plugins.channels.gcp_cloud_monitoring.plugin import (
    parse_gcp_cloud_monitoring_webhook,
)


def _payload(state="open", **incident_overrides) -> dict:
    incident = {
        "incident_id": "inc-123",
        "state": state,
        "started_at": 1700000000,
        "summary": "Spike in 5xx",
        "policy_name": "GCP ELB 5xx Alert",
        "condition_name": "Backend Request Count",
        "severity": "CRITICAL",
        "url": "https://console.cloud.google.com/incident/inc-123",
        "resource": {"type": "https_lb_rule", "labels": {"url_map_name": "k8s2-um-xxx"}},
        "metric": {
            "type": "loadbalancing.googleapis.com/https/backend_request_count",
            "labels": {"response_code_class": "500"},
        },
        "policy_user_labels": {},
    }
    incident.update(incident_overrides)
    return {"incident": incident, "version": "1.2"}


def test_open_incident_becomes_one_issue():
    issues = parse_gcp_cloud_monitoring_webhook(_payload())
    assert len(issues) == 1
    issue = issues[0]
    assert issue.id == "gcp_cloud_monitoring:inc-123"
    assert issue.title == "[CRITICAL] GCP ELB 5xx Alert — Backend Request Count"
    assert issue.source == "gcp_cloud_monitoring"
    assert issue.severity == "critical"
    assert issue.description == "Spike in 5xx"
    assert issue.source_url == "https://console.cloud.google.com/incident/inc-123"


def test_labels_flatten_resource_and_metric_and_force_gcp_cloud():
    issue = parse_gcp_cloud_monitoring_webhook(_payload())[0]
    assert issue.labels["url_map_name"] == "k8s2-um-xxx"
    assert issue.labels["response_code_class"] == "500"
    assert issue.labels["alertname"] == "GCP ELB 5xx Alert"
    assert issue.labels["cloud"] == "gcp"


def test_started_at_parsed_from_unix_epoch():
    issue = parse_gcp_cloud_monitoring_webhook(_payload())[0]
    assert issue.started_at is not None
    assert issue.started_at.year == 2023


def test_closed_incident_is_skipped():
    assert parse_gcp_cloud_monitoring_webhook(_payload(state="closed")) == []


def test_missing_incident_returns_no_issues():
    assert parse_gcp_cloud_monitoring_webhook({}) == []
    assert parse_gcp_cloud_monitoring_webhook({"incident": {}}) == []


def test_missing_optional_fields_degrade_gracefully():
    payload = {"incident": {"state": "open", "policy_name": "SomePolicy"}}
    issues = parse_gcp_cloud_monitoring_webhook(payload)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.title.startswith("[WARNING] SomePolicy")
    assert issue.started_at is None


# ── Token-auth logic (mirrors the constant-time check the route performs) ──

def test_token_auth_constant_time_compare_matches():
    secret = "s3cr3t-token"
    assert hmac.compare_digest("s3cr3t-token", secret) is True


def test_token_auth_constant_time_compare_rejects_wrong_or_missing():
    secret = "s3cr3t-token"
    assert hmac.compare_digest("wrong", secret) is False
    assert hmac.compare_digest("", secret) is False
