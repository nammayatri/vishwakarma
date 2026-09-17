"""Fast-triage log-signature classification and pod-health querying."""
from vishwakarma.core.fast_triage import _find_infra_or_route_match, _pod_health_lines, _route_for_alert


def test_gcp_elb_5xx_route_includes_release_monitoring():
    # A 5xx spike alert must check which route/API is actually throwing the
    # 500s, not just mesh-level symptoms and generic connectivity signatures.
    assert "Release Monitoring" in _route_for_alert("[CRITICAL] GCP ELB 5xx Alert — foo")


def test_no_healthy_upstream_does_not_assert_crashlooping():
    # Envoy emits this exact text for connection resets, rollouts/scale-downs,
    # and slow readiness probes too — not only an actual crash loop, so the
    # label must not claim "crashlooping" as an established fact.
    lines = [
        'httpVersion = HTTP/1.1, responseBody = "upstream connect error or '
        'disconnect/reset before headers. reset reason: connection termination"'
    ]
    hit = _find_infra_or_route_match(lines, [])
    assert hit is not None
    kind, label, matched_line, _pattern = hit
    assert kind == "infra"
    assert "crashlooping" not in label.lower()
    assert "not necessarily" in label.lower()
    assert matched_line == lines[0]


def test_oom_signature_still_asserts_cause_directly_stated_in_log():
    lines = ["container was OOMKilled at 2026-09-15T04:12:00Z"]
    hit = _find_infra_or_route_match(lines, [])
    assert hit is not None
    kind, label, _line, _pattern = hit
    assert kind == "infra" and "OOM" in label


def test_no_match_returns_none():
    assert _find_infra_or_route_match(["everything is fine, 200 OK"], []) is None


def test_a_stale_infra_match_does_not_suppress_a_more_recent_route_failure():
    # Regression: a single old "no healthy upstream" blip anywhere in the
    # window used to win unconditionally over a genuinely current, dominant
    # API failure — even when that failure sits on a much more recent line.
    lines = [
        'upstream connect error or disconnect/reset before headers',  # oldest
        'method=POST handler=/config/v2 status_code=500',             # newest
    ]
    hit = _find_infra_or_route_match(lines, [r"/config/v2"])
    assert hit is not None
    kind, _label, matched_line, _pattern = hit
    assert kind == "route"
    assert matched_line == lines[1]


def test_infra_match_still_wins_when_it_is_the_more_recent_line():
    lines = [
        'method=POST handler=/config/v2 status_code=500',             # oldest
        'upstream connect error or disconnect/reset before headers',  # newest
    ]
    hit = _find_infra_or_route_match(lines, [r"/config/v2"])
    assert hit is not None
    kind, _label, matched_line, _pattern = hit
    assert kind == "infra"
    assert matched_line == lines[1]


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
