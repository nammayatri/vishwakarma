"""Fast-triage log-signature classification and pod-health querying."""
from vishwakarma.core.fast_triage import _find_infra_match, _pod_health_lines


def test_no_healthy_upstream_does_not_assert_crashlooping():
    # Envoy emits this exact text for connection resets, rollouts/scale-downs,
    # and slow readiness probes too — not only an actual crash loop, so the
    # label must not claim "crashlooping" as an established fact.
    lines = [
        'httpVersion = HTTP/1.1, responseBody = "upstream connect error or '
        'disconnect/reset before headers. reset reason: connection termination"'
    ]
    hit = _find_infra_match(lines)
    assert hit is not None
    label, matched_line, _pattern = hit
    assert "crashlooping" not in label.lower()
    assert "not necessarily" in label.lower()
    assert matched_line == lines[0]


def test_oom_signature_still_asserts_cause_directly_stated_in_log():
    lines = ["container was OOMKilled at 2026-09-15T04:12:00Z"]
    hit = _find_infra_match(lines)
    assert hit is not None
    label, _line, _pattern = hit
    assert "OOM" in label


def test_no_match_returns_none():
    assert _find_infra_match(["everything is fine, 200 OK"]) is None


class _FakeProm:
    """Records every PromQL string sent; returns [] for everything (the
    join structure is what's under test, not real evaluation)."""
    def __init__(self):
        self.queries = []

    def _get(self, path, params):
        self.queries.append(params["query"])
        return {"data": {"result": []}}


def test_pod_health_queries_scope_to_currently_live_pods():
    # A pod deleted minutes ago (routine scale-down/rollout) still has
    # samples inside a 15m increase() window — without joining against
    # kube_pod_info (only exists for pods that currently exist), its old
    # restart count gets misreported as describing a pod still running now.
    prom = _FakeProm()
    _pod_health_lines(prom, [("svc", "ns")], top_n=5)
    assert prom.queries
    assert all("and on(pod)" in q and "kube_pod_info" in q for q in prom.queries)
