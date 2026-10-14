"""
GCP Cloud Monitoring webhook — parse an incident notification POST body into
Issue objects.

Payload shape (Cloud Monitoring webhook schema v1.2):
  {"incident": {...}, "version": "1.2"}
Key incident fields: incident_id, state ("open"|"closed"), started_at (unix
epoch seconds), summary, policy_name, condition_name, url,
resource.type/.labels, metric.type/.labels, policy_user_labels.

Mirrors plugins/channels/alertmanager/plugin.py's parse_alertmanager_webhook
pattern so the two sources feed the exact same downstream Issue shape.
"""
import logging
from datetime import datetime, timezone

from vishwakarma.core.issue import Issue, IssueStatus

log = logging.getLogger(__name__)


def parse_gcp_cloud_monitoring_webhook(payload: dict) -> list[Issue]:
    """
    Parse a Cloud Monitoring webhook POST body into Issue objects.
    Used by the /api/gcp-cloud-monitoring/webhook endpoint.
    """
    incident = payload.get("incident") or {}
    if not incident:
        return []

    # Only investigate newly-opened incidents — a "closed" notification means
    # the alert cleared, handled separately (auto-resolve) by the endpoint.
    if incident.get("state") != "open":
        return []

    policy_name = incident.get("policy_name", "UnknownPolicy")
    condition_name = incident.get("condition_name", "")
    severity = (incident.get("severity") or "warning").lower()
    summary = incident.get("summary", "")

    resource = incident.get("resource") or {}
    metric = incident.get("metric") or {}
    resource_labels = resource.get("labels") or {}
    metric_labels = metric.get("labels") or {}
    policy_user_labels = incident.get("policy_user_labels") or {}

    # Flatten every label source into one dict, same role AlertManager's
    # `labels` plays downstream (fast_triage's service/namespace lookups
    # already degrade gracefully to cluster-wide discovery when these GCP
    # native resources — e.g. https_lb_rule, redis_instance — don't carry
    # k8s namespace/service labels).
    labels: dict = {}
    labels.update(policy_user_labels)
    labels.update(resource_labels)
    labels.update(metric_labels)
    labels["alertname"] = policy_name
    labels["cloud"] = "gcp"  # this source is inherently GCP-only

    title = f"[{severity.upper()}] {policy_name}"
    if condition_name:
        title += f" — {condition_name}"

    started_at = None
    raw_started_at = incident.get("started_at")
    if raw_started_at:
        try:
            started_at = datetime.fromtimestamp(float(raw_started_at), tz=timezone.utc)
        except (TypeError, ValueError):
            pass

    incident_id = incident.get("incident_id") or f"{policy_name}:{condition_name}"

    issue = Issue(
        id=f"gcp_cloud_monitoring:{incident_id}",
        title=title,
        description=summary,
        source="gcp_cloud_monitoring",
        source_url=incident.get("url"),
        labels=labels,
        annotations={},
        severity=severity,
        status=IssueStatus.OPEN,
        started_at=started_at,
        raw=payload,
    )
    return [issue]
