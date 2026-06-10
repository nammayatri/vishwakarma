"""
Infra-mutation guardrail — the agent is read-only and must never run a
destructive kubectl/aws/gcloud/helm/... command even with prod creds.

Run:  pytest tests/test_infra_guard.py -v
"""
import pytest

from vishwakarma.plugins.toolsets.bash.bash import BashToolset


@pytest.fixture()
def bash():
    # allow the infra CLIs (so the guard, not the allow-list, is what blocks)
    return BashToolset({"safe_mode": False,
                        "allow": ["kubectl", "aws", "gcloud", "helm", "gsutil",
                                  "argocd", "terraform", "docker", "echo", "cat", "grep"]})


# ── Must be BLOCKED (mutating) ──────────────────────────────────────────────────

BLOCKED = [
    "kubectl delete deployment protocol-app -n atlas",
    "kubectl -n atlas scale deployment x --replicas=0",
    "kubectl rollout restart deployment/foo -n atlas",
    "kubectl apply -f deploy.yaml",
    "kubectl patch deployment x -p '{}'",
    "kubectl drain node-1",
    "kubectl exec -it pod -- sh",
    "aws rds delete-db-instance --db-instance-identifier prod",
    "aws ec2 terminate-instances --instance-ids i-123",
    "aws ec2 stop-instances --instance-ids i-123",
    "aws s3 rm s3://bucket/key",
    "aws rds modify-db-instance --db-instance-identifier prod",
    "gcloud sql instances delete prod",
    "gcloud compute instances stop vm-1",
    "gcloud container clusters resize c --num-nodes 0",
    "helm upgrade release chart",
    "helm uninstall release",
    "gsutil rm gs://bucket/obj",
    "argocd app delete myapp",
    "terraform destroy",
    "terraform apply",
    "docker push myreg/img:tag",
    "echo hi && kubectl delete pod x",                 # chained
    "bash -c 'kubectl delete ns atlas'",               # also caught by hardcoded bash -c
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_mutating_blocked(bash, cmd):
    allowed, reason = bash._is_allowed(cmd)
    assert not allowed, f"SHOULD BE BLOCKED: {cmd}"


def test_mutating_in_subshell_blocked(bash):
    allowed, _ = bash._is_allowed("echo $(kubectl delete pod x)")
    assert not allowed


# ── Must be ALLOWED (read-only) ─────────────────────────────────────────────────

ALLOWED = [
    "kubectl get pods -n atlas",
    "kubectl describe deployment protocol-app -n atlas",
    "kubectl logs -n atlas pod-xyz --tail=100",
    "kubectl get events -n atlas --sort-by=.lastTimestamp",
    "kubectl top pods -n atlas",
    "aws rds describe-db-instances --db-instance-identifier prod",
    "aws cloudwatch get-metric-statistics --namespace AWS/RDS",
    "aws ec2 describe-instances",
    "aws s3 ls s3://bucket/",
    "gcloud sql instances describe prod",
    "gcloud compute instances list",
    "gcloud logging read 'severity>=ERROR' --limit 50",
    "helm list -n atlas",
    "helm get values release",
    "kubectl get deployment -o yaml protocol-app -n atlas",   # 'get -o yaml' is read
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_read_only_allowed(bash, cmd):
    allowed, reason = bash._is_allowed(cmd)
    assert allowed, f"SHOULD BE ALLOWED (read-only): {cmd} — blocked by: {reason}"


def test_guard_not_overridable_by_allow():
    # even if 'kubectl' is explicitly allowed, delete is still blocked
    b = BashToolset({"safe_mode": False, "allow": ["kubectl delete"]})
    allowed, _ = b._is_allowed("kubectl delete pod x -n atlas")
    assert not allowed
