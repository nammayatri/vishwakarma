import tempfile
import time

import pytest

from vishwakarma.core import remediation as rem
from vishwakarma.core.fast_triage import _route_for_alert, _stage_zero_dc_remediation
from vishwakarma.storage import db as dbmod
from vishwakarma.storage import remediation as store


@pytest.fixture(autouse=True)
def _db():
    dbmod._conn = None
    dbmod.init_db(db_path=tempfile.mktemp(suffix=".db"))
    yield


def _settings(**kw):
    return rem.RemediationSettings(enabled=True, **kw)


def _propose(settings=None, **kw):
    args = dict(incident_id="inc1", alert_title="DriverApp0DC", platform="slack", channel="C1",
                thread_ts="1.1", namespace="atlas", service="beckn-offer",
                pod="beckn-offer-abc12-xyz", evidence="e", queries_ran=["q1", "q2"])
    args.update(kw)
    return rem.propose(settings or _settings(), **args)


class Runner:
    def __init__(self, get_ok=True, delete_ok=True):
        self.calls = []
        self.get_ok, self.delete_ok = get_ok, delete_ok

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[1] == "get":
            return (0, "pod/x") if self.get_ok else (1, "NotFound")
        return (0, 'pod "x" deleted') if self.delete_ok else (1, "forbidden")


def test_parse_command_accepts_only_exact_shape():
    assert rem.parse_command("kubectl delete pod beckn-offer-abc -n atlas") == ("atlas", "beckn-offer-abc")
    for bad in ["kubectl delete pod a -n atlas; rm -rf /", "kubectl delete pod a b -n atlas",
                "kubectl delete pods a -n atlas", "kubectl delete pod A_B -n atlas",
                "kubectl delete pod a -n atlas --all", "kubectl delete ns atlas", "rm -rf /"]:
        assert rem.parse_command(bad) is None


def test_propose_gates():
    assert _propose(rem.RemediationSettings(enabled=False)) is None
    assert _propose(namespace="kube-system") is None
    assert _propose(pod="other-service-abc") is None
    assert _propose(pod="beckn-offer-abc; rm -rf") is None
    a = _propose()
    assert a["status"] == "pending" and a["command"] == "kubectl delete pod beckn-offer-abc12-xyz -n atlas"
    assert a["queries_ran"] == ["q1", "q2"] and a["is_approved"] is None


def test_approve_executes_exact_command_once():
    a, r = _propose(), Runner()
    out = rem.decide(_settings(), a["id"], approved=True, approver="Alice", via="slack", runner=r)
    assert out["status"] == "executed"
    assert r.calls[-1] == ["kubectl", "delete", "pod", "beckn-offer-abc12-xyz", "-n", "atlas"]
    row = store.get_action(a["id"])
    assert row["is_approved"] == 1 and row["approved_by"] == "Alice" and row["approved_via"] == "slack"
    again = rem.decide(_settings(), a["id"], approved=True, approver="Bob", via="slack", runner=r)
    assert again["status"] == "executed"
    assert sum(1 for c in r.calls if c[1] == "delete") == 1


def test_reject_never_runs():
    a, r = _propose(), Runner()
    out = rem.decide(_settings(), a["id"], approved=False, approver="Alice", via="xyne", runner=r)
    assert out["status"] == "rejected" and r.calls == []
    assert store.get_action(a["id"])["is_approved"] == 0


def test_expired_never_runs():
    a, r = _propose(), Runner()
    out = rem.decide(_settings(ttl_seconds=-1), a["id"], approved=True, approver="Alice", via="slack", runner=r)
    assert out["status"] == "expired" and r.calls == []


def test_unauthorized_never_runs_and_stays_pending():
    a, r = _propose(), Runner()
    s = _settings(approvers=["alice@x.in"])
    out = rem.decide(s, a["id"], approved=True, approver="Mallory", approver_ids=["U9"], via="slack", runner=r)
    assert out["status"] == "unauthorized" and r.calls == []
    assert store.get_action(a["id"])["status"] == "pending"
    ok = rem.decide(s, a["id"], approved=True, approver="Alice", approver_ids=["alice@x.in"], via="slack", runner=r)
    assert ok["status"] == "executed"


def test_pod_already_gone_does_not_delete():
    a, r = _propose(), Runner(get_ok=False)
    out = rem.decide(_settings(), a["id"], approved=True, approver="Alice", via="slack", runner=r)
    assert out["status"] == "failed" and all(c[1] == "get" for c in r.calls)


def test_disabled_blocks_decision():
    a, r = _propose(), Runner()
    out = rem.decide(rem.RemediationSettings(enabled=False), a["id"], approved=True, approver="A", via="slack", runner=r)
    assert out["status"] == "disabled" and r.calls == []


def test_yes_no_parsing():
    assert rem.is_yes("yes") and rem.is_yes("Approve!") and not rem.is_yes("yes but check logs first")
    assert rem.is_no("no") and not rem.is_no("not sure")


def test_xyne_pending_lookup_requires_unambiguous():
    a = _propose()
    assert rem.find_pending_for_xyne("whatever", 900)["id"] == a["id"]
    _propose(pod="beckn-offer-def34-uvw")
    assert rem.find_pending_for_xyne("whatever", 900) is None


def test_extract_click_slack_shaped():
    p = {"actions": [{"action_id": rem.APPROVE_ACTION_ID, "value": "abc"}], "user": {"id": "U1", "name": "al"}}
    assert rem.extract_click(p) == (rem.APPROVE_ACTION_ID, "abc", "U1", "al")
    assert rem.extract_click({"payload": p}) == (rem.APPROVE_ACTION_ID, "abc", "U1", "al")
    assert rem.extract_click({"foo": 1}) is None


def test_zero_dc_alerts_route_through_remediation_stage():
    assert "0DC Remediation" in _route_for_alert("[CRITICAL] DriverApp0DCOr5xx in GCP")
    assert "0DC Remediation" not in _route_for_alert("[CRITICAL] NoRiderDrainerRunning")


class FakeProm:
    def __init__(self, dc, allpods):
        self.dc, self.allpods = dc, allpods

    def _get(self, path, params):
        q = params["query"]
        rows = self.dc if 'response_code="0"' in q else self.allpods
        return {"data": {"result": [{"metric": m, "value": [0, str(v)]} for m, v in rows]}}


def _m(svc, pod):
    return {"destination_service_name": svc, "destination_workload_namespace": "atlas", "pod": pod}


def _run_stage(prom, settings=None):
    proposed = []
    s = settings or _settings()
    ctx = {"_remediation": s, "known_service": "", "namespace_exclude": "app-monitor",
           "_propose": lambda **kw: proposed.append(kw) or kw}
    text, _ = _stage_zero_dc_remediation(prom, ctx, 5)
    return text, proposed


def test_stage_one_bad_pod_of_four_proposes_delete():
    allp = [(_m("beckn-offer", f"beckn-offer-p{i}"), 100) for i in range(4)]
    text, proposed = _run_stage(FakeProm([(_m("beckn-offer", "beckn-offer-p0"), 400)], allp))
    assert len(proposed) == 1 and proposed[0]["pod"] == "beckn-offer-p0" and proposed[0]["namespace"] == "atlas"
    assert "1/4 pods" in text and len(proposed[0]["queries_ran"]) == 2


def test_stage_real_incident_ambient_noise_elsewhere_still_proposes_the_one_outlier():
    offer = [f"beckn-driver-offer-bpp-production-c9e91bv1-566d45f4db-p{i}" for i in range(24)]
    lts = [f"beckn-location-tracking-service-production-3a96c8-p{i}" for i in range(8)]
    allp = [(_m("beckn-driver-offer-bpp-production", p), 3000) for p in offer] + \
           [(_m("beckn-location-tracking-service-production", p), 3000) for p in lts]
    dc = [(_m("beckn-driver-offer-bpp-production", offer[0]), 2371)] + \
         [(_m("beckn-driver-offer-bpp-production", p), 8) for p in offer[1:]] + \
         [(_m("beckn-location-tracking-service-production", p), 8) for p in lts]
    text, proposed = _run_stage(FakeProm(dc, allp))
    assert len(proposed) == 1
    assert proposed[0]["pod"] == offer[0] and proposed[0]["service"] == "beckn-driver-offer-bpp-production"
    assert "1/24 pods" in text and "not isolated" not in text


def test_stage_ambient_noise_only_is_silent():
    pods = [f"a-{i}" for i in range(10)]
    allp = [(_m("a", p), 100) for p in pods]
    assert _run_stage(FakeProm([(_m("a", p), 8) for p in pods], allp)) == ("", [])


def test_stage_two_services_with_outliers_no_proposal():
    allp = [(_m(s, f"{s}-{i}"), 100) for s in ("a", "b") for i in range(4)]
    dc = [(_m("a", "a-0"), 600), (_m("b", "b-0"), 600)]
    text, proposed = _run_stage(FakeProm(dc, allp))
    assert proposed == [] and "not isolated" in text


def test_stage_fleet_wide_spread_is_not_an_outlier():
    pods = [f"a-{i}" for i in range(6)]
    allp = [(_m("a", p), 1000) for p in pods]
    text, proposed = _run_stage(FakeProm([(_m("a", p), 500) for p in pods], allp))
    assert proposed == [] and "spread evenly" in text


def test_stage_all_pods_bad_no_proposal():
    allp = [(_m("a", "a-0"), 100)]
    text, proposed = _run_stage(FakeProm([(_m("a", "a-0"), 400)], allp))
    assert proposed == [] and "every pod" in text


def test_stage_too_many_bad_pods_no_proposal():
    allp = [(_m("a", f"a-{i}"), 100) for i in range(12)]
    dc = [(_m("a", f"a-{i}"), 400) for i in range(4)]
    text, proposed = _run_stage(FakeProm(dc, allp), _settings(max_pods=3))
    assert proposed == [] and "too many" in text


def test_stage_below_floor_and_disabled_are_silent():
    allp = [(_m("a", f"a-{i}"), 100) for i in range(4)]
    assert _run_stage(FakeProm([(_m("a", "a-0"), 20)], allp)) == ("", [])
    assert _run_stage(FakeProm([(_m("a", "a-0"), 400)], allp), rem.RemediationSettings(enabled=False)) == ("", [])


def test_config_defaults_yaml_and_env_override(monkeypatch):
    pytest.importorskip("litellm")
    from vishwakarma.config import VishwakarmaConfig
    for k in list(__import__("os").environ):
        if k.startswith("VK_REMEDIATION_"):
            monkeypatch.delenv(k)
    c = VishwakarmaConfig({})
    assert (c.remediation_enabled, c.remediation_min_requests, c.remediation_outlier_factor) == (True, 50.0, 10.0)
    assert c.remediation_allowed_namespaces == ["atlas"] and c.remediation_max_pods == 3

    c = VishwakarmaConfig({"remediation": {"min_requests": 80, "outlier_factor": 5, "allowed_namespaces": ["atlas", "x"]}})
    assert (c.remediation_min_requests, c.remediation_outlier_factor) == (80.0, 5.0)
    assert c.remediation_allowed_namespaces == ["atlas", "x"]

    monkeypatch.setenv("VK_REMEDIATION_MIN_REQUESTS", "120")
    monkeypatch.setenv("VK_REMEDIATION_OUTLIER_FACTOR", "20")
    monkeypatch.setenv("VK_REMEDIATION_ALLOWED_NAMESPACES", "atlas, prod2")
    monkeypatch.setenv("VK_REMEDIATION_APPROVERS", "U1,a@b.in")
    monkeypatch.setenv("VK_REMEDIATION_ENABLED", "false")
    c = VishwakarmaConfig({"remediation": {"min_requests": 80}})
    assert (c.remediation_min_requests, c.remediation_outlier_factor) == (120.0, 20.0)
    assert c.remediation_allowed_namespaces == ["atlas", "prod2"] and c.remediation_approvers == ["U1", "a@b.in"]
    assert c.remediation_enabled is False
    s = rem.settings_from_config(c)
    assert s.min_requests == 120.0 and s.outlier_factor == 20.0 and s.enabled is False
