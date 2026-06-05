"""
Cloud router — classify an alert/issue to the cloud whose executors can
actually reach its data plane.

Each cloud's DB/Redis/cluster is VPC-internal, so an investigation MUST run
on an executor inside that cloud. Routing reads the issue's labels and
source; cross-cloud incidents fan out to 'both' and each side investigates
its own half.

Order of precedence:
  1. explicit `cloud` label                       (operator override)
  2. unambiguous single-cloud signals             (account id, region,
     cluster name, source system)
  3. signals from BOTH clouds                     → 'both'
  4. default_cloud                                (config; '' = 'aws')
"""
import logging

log = logging.getLogger(__name__)

AWS = "aws"
GCP = "gcp"
BOTH = "both"

# Substring signals checked against a small set of label values + source.
_AWS_SIGNALS = (
    "ap-south-1", "eks-", "aws", "cloudwatch", "amazonaws", "rds", "ec2",
)
_GCP_SIGNALS = (
    "asia-south1", "gke-", "gcp", "google", "alloydb", "cloudsql", "prod-project",
)

# Labels worth inspecting for cloud hints.
_HINT_LABELS = (
    "cloud", "region", "aws_account", "aws_region", "project_id", "cluster",
    "cluster_name", "source", "aws_namespace", "zone",
)


def route_issue(issue, default_cloud: str = AWS) -> str:
    """Return 'aws' | 'gcp' | 'both' for an Issue."""
    labels = dict(issue.labels or {})

    # 1. Explicit override
    explicit = str(labels.get("cloud", "")).lower().strip()
    if explicit in (AWS, GCP, BOTH):
        return explicit

    # 2/3. Collect signals from labels + source + title
    hay = [str(issue.source or "").lower()]
    for k in _HINT_LABELS:
        v = labels.get(k)
        if v:
            hay.append(str(v).lower())
    # GCP Monitoring alerts carry project_id; CloudWatch carries aws_account —
    # those labels' mere presence is a strong signal even with odd values.
    if labels.get("aws_account") or labels.get("aws_region"):
        hay.append("aws")
    if labels.get("project_id"):
        hay.append("gcp")
    text = " ".join(hay)

    aws_hit = any(s in text for s in _AWS_SIGNALS)
    gcp_hit = any(s in text for s in _GCP_SIGNALS)

    if aws_hit and gcp_hit:
        return BOTH
    if aws_hit:
        return AWS
    if gcp_hit:
        return GCP

    # 4. No signal — default
    cloud = (default_cloud or AWS).lower()
    log.info(f"No cloud signal for '{issue.title[:60]}' — defaulting to {cloud}")
    return cloud if cloud in (AWS, GCP, BOTH) else AWS
