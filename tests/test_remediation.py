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


ALICE, BOB = "alice@x.in", "bob@x.in"


def _settings(**kw):
    kw.setdefault("approver_emails", [ALICE, BOB])
    kw.setdefault("drainer_redis_host", "redis.internal")
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
    out = rem.decide(_settings(), a["id"], approved=True, approver="Alice", approver_email=ALICE, via="slack", runner=r)
    assert out["status"] == "executed"
    assert r.calls[-1] == ["kubectl", "delete", "pod", "beckn-offer-abc12-xyz", "-n", "atlas"]
    row = store.get_action(a["id"])
    assert row["is_approved"] == 1 and row["approved_via"] == "slack"
    assert row["approved_by"] == f"Alice <{ALICE}>"
    again = rem.decide(_settings(), a["id"], approved=True, approver="Bob", approver_email=BOB, via="slack", runner=r)
    assert again["status"] == "executed"
    assert sum(1 for c in r.calls if c[1] == "delete") == 1


def test_reject_never_runs():
    a, r = _propose(), Runner()
    out = rem.decide(_settings(), a["id"], approved=False, approver="Alice", approver_email=ALICE, via="xyne", runner=r)
    assert out["status"] == "rejected" and r.calls == []
    assert store.get_action(a["id"])["is_approved"] == 0


def test_expired_never_runs():
    a, r = _propose(), Runner()
    out = rem.decide(_settings(ttl_seconds=-1), a["id"], approved=True, approver="Alice", approver_email=ALICE,
                     via="slack", runner=r)
    assert out["status"] == "expired" and r.calls == []


def test_unauthorized_never_runs_and_stays_pending():
    a, r = _propose(), Runner()
    s = _settings(approver_emails=[ALICE])
    out = rem.decide(s, a["id"], approved=True, approver="Mallory", approver_email="mallory@x.in", via="slack", runner=r)
    assert out["status"] == "unauthorized" and r.calls == []
    assert "don't have access" in out["text"] and "mallory@x.in" in out["text"]
    assert store.get_action(a["id"])["status"] == "pending"
    ok = rem.decide(s, a["id"], approved=True, approver="Alice", approver_email=ALICE.upper(), via="slack", runner=r)
    assert ok["status"] == "executed"


def test_no_email_or_empty_allowlist_means_no_access():
    a, r = _propose(), Runner()
    for settings, email in [(_settings(), ""), (_settings(approver_emails=[]), ALICE), (_settings(approver_emails=[""]), "")]:
        out = rem.decide(settings, a["id"], approved=True, approver="X", approver_email=email, via="slack", runner=r)
        assert out["status"] == "unauthorized"
    assert r.calls == [] and store.get_action(a["id"])["status"] == "pending"


def test_unauthorized_reject_is_also_refused():
    a, r = _propose(), Runner()
    out = rem.decide(_settings(), a["id"], approved=False, approver="Mallory", approver_email="m@x.in", via="slack", runner=r)
    assert out["status"] == "unauthorized" and store.get_action(a["id"])["status"] == "pending"


def test_denied_notice_keeps_buttons():
    a = _propose()
    blocks = rem.proposal_blocks(a, rem.denied_notice("Mallory", "m@x.in"))
    kinds = [b["type"] for b in blocks]
    assert kinds == ["section", "context", "actions"]
    ids = [e["action_id"] for e in blocks[-1]["elements"]]
    assert ids == [rem.APPROVE_ACTION_ID, rem.REJECT_ACTION_ID]
    assert "don't have access" in blocks[1]["elements"][0]["text"]


def test_pod_already_gone_does_not_delete():
    a, r = _propose(), Runner(get_ok=False)
    out = rem.decide(_settings(), a["id"], approved=True, approver="Alice", approver_email=ALICE, via="slack", runner=r)
    assert out["status"] == "failed" and all(c[1] == "get" for c in r.calls)


def test_disabled_blocks_decision():
    a, r = _propose(), Runner()
    out = rem.decide(rem.RemediationSettings(enabled=False, approver_emails=[ALICE]), a["id"], approved=True,
                     approver="A", approver_email=ALICE, via="slack", runner=r)
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
    p = {"actions": [{"action_id": rem.APPROVE_ACTION_ID, "value": "abc"}],
         "user": {"id": "U1", "name": "al", "profile": {"email": "al@x.in"}}}
    assert rem.extract_click(p) == (rem.APPROVE_ACTION_ID, "abc", "U1", "al", "al@x.in")
    assert rem.extract_click({"payload": p}) == (rem.APPROVE_ACTION_ID, "abc", "U1", "al", "al@x.in")
    no_email = {"actions": [{"action_id": "x", "value": "v"}], "user": {"id": "U1"}}
    assert rem.extract_click(no_email)[4] == ""
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
    monkeypatch.setenv("VK_REMEDIATION_APPROVER_EMAILS", "a@b.in, C@D.in")
    monkeypatch.setenv("VK_REMEDIATION_DRAINER_REDIS_HOST", "10.1.2.3")
    monkeypatch.setenv("VK_REMEDIATION_DRAINER_REDIS_PORT", "6380")
    monkeypatch.setenv("VK_REMEDIATION_DRAINER_REDIS_CLUSTER", "true")
    monkeypatch.setenv("VK_REMEDIATION_ENABLED", "false")
    c = VishwakarmaConfig({"remediation": {"min_requests": 80}})
    assert (c.remediation_min_requests, c.remediation_outlier_factor) == (120.0, 20.0)
    assert c.remediation_allowed_namespaces == ["atlas", "prod2"]
    assert c.remediation_approver_emails == ["a@b.in", "C@D.in"]
    assert c.remediation_enabled is False
    s = rem.settings_from_config(c)
    assert s.min_requests == 120.0 and s.outlier_factor == 20.0 and s.enabled is False
    assert (s.drainer_redis_host, s.drainer_redis_port, s.drainer_redis_cluster) == ("10.1.2.3", 6380, True)
    assert s.approver_emails == ["a@b.in", "C@D.in"]

    for k in list(__import__("os").environ):
        if k.startswith("VK_REMEDIATION_"):
            monkeypatch.delenv(k)
    c = VishwakarmaConfig({})
    assert c.remediation_approver_emails == [] and c.remediation_drainer_redis_host == ""


def _drainer(side="driver", settings=None, **kw):
    args = dict(incident_id="inc2", alert_title="NoDriverDrainerRunning", platform="slack", channel="C1",
                thread_ts="1.1", side=side, evidence="stopped", queries_ran=["max(driver_drainer_stop_status)"])
    args.update(kw)
    return rem.propose_drainer(settings or _settings(), **args)


class RedisSim:
    def __init__(self, stop="true", force=None, fail_on=None):
        self.kv = {}
        if stop is not None:
            self.kv["DRIVER_DRAINER_STOP"] = stop
        if force is not None:
            self.kv["DRIVER_FORCE_DRAIN"] = force
        self.calls, self.fail_on = [], fail_on

    def __call__(self, argv):
        self.calls.append(argv)
        cmd, *rest = argv[argv.index("--no-auth-warning") + 1:]
        if self.fail_on == cmd:
            return 1, "ERR boom"
        if cmd == "GET":
            return 0, self.kv.get(rest[0], "")
        if cmd == "SET":
            self.kv[rest[0]] = rest[1]
            return 0, "OK"
        if cmd == "DEL":
            return 0, str(int(self.kv.pop(rest[0], None) is not None))
        return 1, "unknown"


def test_drainer_commands_use_the_real_backend_keys():
    assert rem.build_drainer_command("driver") == "redis-cli SET DRIVER_FORCE_DRAIN true\nredis-cli DEL DRIVER_DRAINER_STOP"
    assert rem.build_drainer_command("rider") == "redis-cli SET FORCE_DRAIN true\nredis-cli DEL RIDER_DRAINER_STOP"
    assert rem.parse_drainer_command(rem.build_drainer_command("rider")) == "rider"
    for bad in ["redis-cli FLUSHALL", "redis-cli DEL SOMETHING_ELSE", "redis-cli SET DRIVER_FORCE_DRAIN false\nredis-cli DEL DRIVER_DRAINER_STOP"]:
        assert rem.parse_drainer_command(bad) is None


def test_propose_drainer_gates_and_row():
    assert _drainer(settings=rem.RemediationSettings(enabled=False)) is None
    assert rem.propose_drainer(_settings(drainer_redis_host=""), incident_id="i", alert_title="t", platform="slack",
                               channel="C", thread_ts="1", side="driver", evidence="e", queries_ran=[]) is None
    assert rem.propose_drainer(_settings(), incident_id="i", alert_title="t", platform="slack", channel="C",
                               thread_ts="1", side="bogus", evidence="e", queries_ran=[]) is None
    a = _drainer()
    assert rem.kind_of(a) == "drainer_resume" and a["status"] == "pending"
    assert (a["service"], a["pod"], a["namespace"]) == ("driver-drainer", "driver", rem.DRAINER_NAMESPACE)
    assert "SET DRIVER_FORCE_DRAIN true" in rem.proposal_text(a) and "DEL DRIVER_DRAINER_STOP" in rem.proposal_text(a)


def test_drainer_approve_sets_force_then_deletes_stop_and_verifies():
    a, r = _drainer(), RedisSim()
    out = rem.decide(_settings(drainer_redis_port=6380, drainer_redis_cluster=True), a["id"], approved=True,
                     approver="Alice", approver_email=ALICE, via="slack", runner=r)
    assert out["status"] == "executed", out["text"]
    verbs = [c[c.index("--no-auth-warning") + 1:] for c in r.calls]
    assert verbs == [["GET", "DRIVER_DRAINER_STOP"], ["SET", "DRIVER_FORCE_DRAIN", "true"],
                     ["DEL", "DRIVER_DRAINER_STOP"], ["GET", "DRIVER_DRAINER_STOP"], ["GET", "DRIVER_FORCE_DRAIN"]]
    assert r.calls[0][:6] == ["redis-cli", "-h", "redis.internal", "-p", "6380", "-c"]
    assert r.kv == {"DRIVER_FORCE_DRAIN": "true"}
    assert store.get_action(a["id"])["approved_by"] == f"Alice <{ALICE}>"


def test_drainer_already_resumed_is_a_noop():
    a, r = _drainer(), RedisSim(stop=None)
    out = rem.decide(_settings(), a["id"], approved=True, approver="Alice", approver_email=ALICE, via="slack", runner=r)
    assert out["status"] == "executed" and "no-op" in store.get_action(a["id"])["output"]
    assert all(c[c.index("--no-auth-warning") + 1] == "GET" for c in r.calls) and r.kv == {}


def test_drainer_failure_midway_is_reported_not_hidden():
    a, r = _drainer(), RedisSim(fail_on="DEL")
    out = rem.decide(_settings(), a["id"], approved=True, approver="Alice", approver_email=ALICE, via="slack", runner=r)
    assert out["status"] == "failed" and "DEL DRIVER_DRAINER_STOP failed" in store.get_action(a["id"])["output"]


def test_drainer_moved_redirect_is_a_failure_not_a_noop():
    a = _drainer()
    calls = []

    def moved(argv):
        calls.append(argv)
        return 0, "MOVED 13362 10.60.96.3:11120"

    out = rem.decide(_settings(), a["id"], approved=True, approver="Alice", approver_email=ALICE, via="slack", runner=moved)
    row = store.get_action(a["id"])
    assert out["status"] == "failed" and "no-op" not in row["output"]
    assert "VK_REMEDIATION_DRAINER_REDIS_CLUSTER=true" in row["output"] and len(calls) == 1


def test_drainer_redis_error_replies_and_redirect_noise():
    a, r = _drainer(), RedisSim()
    real = r.__call__

    def noisy(argv):
        code, out = real(argv)
        return code, f"-> Redirected to slot [13362] located at 10.60.96.3:11120\n{out}" if out else (code, out)[1]

    out = rem.decide(_settings(), a["id"], approved=True, approver="Alice", approver_email=ALICE, via="slack", runner=noisy)
    assert out["status"] == "executed" and r.kv == {"DRIVER_FORCE_DRAIN": "true"}

    b = _drainer("rider")

    def set_refused(argv):
        verb = argv[argv.index("--no-auth-warning") + 1]
        return (0, "true") if verb == "GET" else (0, "READONLY You can't write against a read only replica.")

    out = rem.decide(_settings(), b["id"], approved=True, approver="Alice", approver_email=ALICE, via="slack", runner=set_refused)
    assert out["status"] == "failed" and "READONLY" in store.get_action(b["id"])["output"]


def test_drainer_unauthorized_touches_nothing():
    a, r = _drainer(), RedisSim()
    out = rem.decide(_settings(), a["id"], approved=True, approver="Mallory", approver_email="m@x.in", via="slack", runner=r)
    assert out["status"] == "unauthorized" and r.calls == [] and r.kv == {"DRIVER_DRAINER_STOP": "true"}


def test_drainer_routes_and_side_detection():
    from vishwakarma.core.fast_triage import _drainer_sides_from_title
    assert "Drainer Remediation" in _route_for_alert("[CRITICAL] NoRiderDrainerRunning in GCP")
    assert "Drainer Remediation" in _route_for_alert("[CRITICAL] DriverDrainerNotProcessing")
    assert _drainer_sides_from_title("NoDriverDrainerRunning") == ["driver"]
    assert _drainer_sides_from_title("NoRiderDrainerRunning") == ["rider"]
    assert _drainer_sides_from_title("CustomerDrainerNotProcessing") == ["rider"]
    assert _drainer_sides_from_title("SomethingDrainerStopped") == []


class StopProm:
    def __init__(self, driver, rider):
        self.v = {"max(driver_drainer_stop_status)": driver, "max(drainer_stop_status)": rider}

    def _get(self, path, params):
        v = self.v[params["query"]]
        return {"data": {"result": [] if v is None else [{"metric": {}, "value": [0, str(v)]}]}}


def _drain_stage(prom, title, settings=None, redis=("set", "true")):
    from vishwakarma.core.fast_triage import _stage_drainer_remediation
    proposed = []
    ctx = {"_remediation": settings or _settings(), "alert_title": title,
           "_propose_drainer": lambda **kw: proposed.append(kw) or kw}
    if redis is not None:
        ctx["_redis_reader"] = lambda s, side: redis
    text, _ = _stage_drainer_remediation(prom, ctx, 5)
    return text, proposed


def test_read_drainer_stop_key_states_and_cluster_flag():
    r = RedisSim(stop="true")
    assert rem.read_drainer_stop_key(_settings(drainer_redis_cluster=True), "driver", runner=r) == ("set", "true")
    assert r.calls[0][:6] == ["redis-cli", "-h", "redis.internal", "-p", "6379", "-c"]
    assert rem.read_drainer_stop_key(_settings(), "driver", runner=RedisSim(stop=None)) == ("absent", "")
    assert rem.read_drainer_stop_key(_settings(), "driver", runner=RedisSim(stop="false")) == ("absent", "false")
    state, detail = rem.read_drainer_stop_key(_settings(), "driver", runner=lambda argv: (0, "MOVED 1 10.0.0.1:1"))
    assert state == "error" and "CLUSTER=true" in detail
    assert rem.read_drainer_stop_key(_settings(drainer_redis_host=""), "driver")[0] == "error"
    r2 = RedisSim(stop="true")
    rem.read_drainer_stop_key(_settings(), "rider", runner=r2)
    assert r2.calls[0][-1] == "RIDER_DRAINER_STOP" and r2.calls[0][-2] == "GET"


def test_stage_redis_is_the_source_of_truth_key_absent_means_message_only_no_buttons():
    text, proposed = _drain_stage(StopProm(1, 0), "NoDriverDrainerRunning", redis=("absent", ""))
    assert proposed == []
    assert "`DRIVER_DRAINER_STOP` not found in Redis" in text and "nothing to resume" in text
    assert "stop_status=1" in text


def test_stage_key_present_with_non_true_value_is_not_a_stop():
    text, proposed = _drain_stage(StopProm(0, 0), "NoRiderDrainerRunning", redis=("absent", "false"))
    assert proposed == [] and "found with value `false`, not `true`" in text


def test_stage_redis_unreadable_is_reported_and_nothing_proposed():
    text, proposed = _drain_stage(StopProm(1, 0), "NoDriverDrainerRunning", redis=("error", "MOVED 1 x:1"))
    assert proposed == [] and "could not read `DRIVER_DRAINER_STOP` from Redis" in text


def test_stage_key_set_proposes_even_when_metric_lags():
    text, proposed = _drain_stage(StopProm(0, 0), "NoDriverDrainerRunning", redis=("set", "true"))
    assert [p["side"] for p in proposed] == ["driver"] and "`DRIVER_DRAINER_STOP`=true in Redis" in text
    assert proposed[0]["queries_ran"] == ["max(driver_drainer_stop_status)", "redis GET DRIVER_DRAINER_STOP"]


def test_stage_driver_alert_only_resumes_the_driver_drainer_even_if_rider_also_stopped():
    text, proposed = _drain_stage(StopProm(1, 1), "[CRITICAL] NoDriverDrainerRunning")
    assert [p["side"] for p in proposed] == ["driver"] and "resume suggested" in text


def test_stage_rider_alert_resumes_rider_with_the_rider_keys():
    text, proposed = _drain_stage(StopProm(0, 1), "NoRiderDrainerRunning")
    assert [p["side"] for p in proposed] == ["rider"]
    assert "RIDER_DRAINER_STOP" in proposed[0]["evidence"] and "`FORCE_DRAIN`" in proposed[0]["evidence"]


def test_stage_unknown_side_checks_both_and_missing_redis_config_is_explained():
    text, proposed = _drain_stage(StopProm(1, 1), "SomeDrainerAlert")
    assert sorted(p["side"] for p in proposed) == ["driver", "rider"]
    text, proposed = _drain_stage(StopProm(1, 0), "NoDriverDrainerRunning", _settings(drainer_redis_host=""), redis=None)
    assert proposed == [] and "drainer_redis.host is not configured" in text
    assert _drain_stage(StopProm(1, 0), "NoDriverDrainerRunning", rem.RemediationSettings(enabled=False)) == ("", [])
