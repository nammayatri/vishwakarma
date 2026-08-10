"""
Fast triage tests — staged Istio -> Release Monitoring -> DB/Redis -> Pod
CPU/Mem narration (core/fast_triage.py).

Run:  pytest tests/test_fast_triage.py -v
"""
import uuid

import pytest

from vishwakarma.core.fast_triage import run_fast_triage_staged
from vishwakarma.core.issue import Issue

STAGE_NAMES = ["Istio mesh", "Release Monitoring", "DB/Redis", "Pod CPU/Mem"]


def issue(title="HighErrorRate", **labels) -> Issue:
    return Issue(id=str(uuid.uuid4()), title=title, source="alertmanager", labels=labels)


class FakePrometheusToolset:
    """Dispatches on a substring match against the PromQL text and returns
    Prometheus's real `/api/v1/query` JSON shape."""
    name = "prometheus"

    def __init__(self, responses: list[tuple[str, list[dict]]]):
        self.responses = responses
        self.calls: list[str] = []

    def _get(self, path: str, params: dict) -> dict:
        query = params.get("query", "")
        self.calls.append(query)
        for substr, result in self.responses:
            if substr in query:
                return {"status": "success", "data": {"resultType": "vector", "result": result}}
        return {"status": "success", "data": {"resultType": "vector", "result": []}}


class FakeToolsetManager:
    def __init__(self, prom=None):
        self._prom = prom

    def get(self, name: str):
        return self._prom if name == "prometheus" else None


class FakeLLM:
    """No fast_model configured — exercises the no-LLM fallback path."""
    class _Cfg:
        fast_model = None
    cfg = _Cfg()


class FakeLLMWithFastModel:
    class _Cfg:
        fast_model = "fast-model"
    cfg = _Cfg()

    def summarize(self, prompt: str) -> str:
        return "Something is degraded."


class StageCollector:
    """Test double for `on_stage_ready` — records (stage_name, text) in order."""
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, stage_name: str, summary_text: str) -> None:
        self.calls.append((stage_name, summary_text))


def _metric_result(**metric) -> list[dict]:
    value = metric.pop("_value")
    return [{"metric": metric, "value": [0, value]}]


def _multi_result(*rows: dict) -> list[dict]:
    out = []
    for row in rows:
        row = dict(row)
        value = row.pop("_value")
        out.append({"metric": row, "value": [0, value]})
    return out


_5XX_SVC_SUBSTR = '(2..|3..|4..)"}[5m])) by (destination_service_name, response_code)'
_4XX_SVC_SUBSTR = '(2..|3..|5..)"}[5m])) by (destination_service_name, response_code)'
_POD_WISE_SUBSTR = "sum by (destination_service_name, pod, response_code, response_flags)"
_RM_5XX_SUBSTR = 'status_code=~"5[0-9]{2}"'
_RM_OTHER_SUBSTR = 'status_code!="200"'


def _std_responses(service="beckn-driver-offer-bpp-production") -> list[tuple[str, list[dict]]]:
    return [
        # order matters: FakePrometheusToolset returns the FIRST substring
        # match, so more specific patterns must come before shorter/looser ones
        (_POD_WISE_SUBSTR, _metric_result(
            destination_service_name=service, pod=f"{service}-abc123",
            response_code="503", response_flags="UF", _value="42",
        )),
        (_5XX_SVC_SUBSTR, _metric_result(
            destination_service_name=service, response_code="503", _value="42",
        )),
        (_4XX_SVC_SUBSTR, _metric_result(
            destination_service_name=service, response_code="404", _value="5",
        )),
        (_RM_5XX_SUBSTR, _metric_result(
            method="POST", handler="/rideBooking", status_code="503", _value="30",
        )),
        (_RM_OTHER_SUBSTR, _metric_result(
            method="POST", handler="/rideStatus", status_code="429", _value="8",
        )),
        ("error_counter", _metric_result(
            HttpCode="E500", ErrorCode="INTERNAL_ERROR", _value="12",
        )),
        ("kv_sql_error_counter", _metric_result(model="RideTable", _value="4")),
        ("kvRedis_hard_db_limit_exceeded", []),
        ("kvRedis_soft_db_limit_exceeded", _metric_result(model="RideTable", _value="2")),
        ("kv_handler_latency_bucket", _metric_result(model="RideTable", _value="0.85")),
        ("container_cpu_usage_seconds_total", _metric_result(pod=f"{service}-abc123", _value="87.5")),
        ("container_memory_working_set_bytes", _metric_result(pod=f"{service}-abc123", _value="91.2")),
        ("container_cpu_cfs_throttled_periods_total", _metric_result(pod=f"{service}-abc123", _value="15")),
        ("kube_pod_container_status_restarts_total", _metric_result(pod=f"{service}-abc123", _value="3")),
    ]


def test_four_stages_run_in_order():
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    run_fast_triage_staged(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), collector, timeout_seconds=10,
    )
    assert [name for name, _ in collector.calls] == STAGE_NAMES


def test_return_value_carries_all_stage_summaries():
    """Callers that want to seed pre-investigation evidence (not just the
    Slack narration via on_stage_ready) use the return value."""
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    result = run_fast_triage_staged(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), collector, timeout_seconds=10,
    )
    assert isinstance(result, str) and result
    for stage_text in dict(collector.calls).values():
        assert stage_text in result


def test_return_value_empty_when_disabled_or_unavailable():
    assert run_fast_triage_staged(issue(service="svc"), None, FakeLLM(), StageCollector()) == ""
    assert run_fast_triage_staged(
        issue(service="svc"), FakeToolsetManager(prom=None), FakeLLM(), StageCollector(),
    ) == ""


def test_known_service_skips_cluster_wide_discovery():
    """When the alert already names a service, Istio queries scope directly to
    it and never apply the discovery-only namespace-exclude filter."""
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    run_fast_triage_staged(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), collector, timeout_seconds=10,
    )
    istio_calls = [q for q in prom.calls if "istio_requests_total" in q]
    assert istio_calls
    assert not any("destination_workload_namespace!=" in q for q in istio_calls)
    # rows_5xx/rows_4xx use an exact match; pod_q uses a regex match (it
    # supports multiple identified services, so the name is re.escape()'d)
    # — accept either form.
    import re as _re
    escaped = _re.escape("beckn-driver-offer-bpp-production")
    assert all(
        'destination_service_name="beckn-driver-offer-bpp-production"' in q
        or f'destination_service_name=~"{escaped}"' in q
        for q in istio_calls
    )
    istio_text = dict(collector.calls)["Istio mesh"]
    assert "beckn-driver-offer-bpp-production" in istio_text


def test_unknown_service_discovers_top_offenders():
    service = "beckn-app-backend-production-pilot"
    responses = [
        (_5XX_SVC_SUBSTR, _multi_result(
            {"destination_service_name": service, "response_code": "503", "_value": "323"},
            {"destination_service_name": "beckn-gateway-production", "response_code": "503", "_value": "40"},
        )),
    ] + _std_responses(service=service)
    prom = FakePrometheusToolset(responses)
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    run_fast_triage_staged(issue(), tm, FakeLLM(), collector, timeout_seconds=10)
    istio_text = dict(collector.calls)["Istio mesh"]
    assert service in istio_text


def test_multiple_offenders_not_collapsed_to_one():
    """topk(5) semantics — more than one service must survive per stage, not max()."""
    prom = FakePrometheusToolset([
        (_5XX_SVC_SUBSTR, _multi_result(
            {"destination_service_name": "svc-a", "response_code": "503", "_value": "50"},
            {"destination_service_name": "svc-b", "response_code": "500", "_value": "30"},
        )),
    ])
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    run_fast_triage_staged(issue(), tm, FakeLLM(), collector, timeout_seconds=10, top_n=5)
    istio_text = dict(collector.calls)["Istio mesh"]
    assert "svc-a" in istio_text and "svc-b" in istio_text


def test_db_redis_stage_reports_choking_signals():
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    run_fast_triage_staged(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), collector, timeout_seconds=10,
    )
    db_text = dict(collector.calls)["DB/Redis"]
    assert "RideTable" in db_text
    assert "SQL errors" in db_text or "limit breaches" in db_text.lower() or "latency" in db_text.lower()


def test_no_prometheus_toolset_does_not_call_back():
    tm = FakeToolsetManager(prom=None)
    collector = StageCollector()
    run_fast_triage_staged(issue(service="svc"), tm, FakeLLM(), collector)
    assert collector.calls == []


def test_no_toolset_manager_does_not_call_back():
    collector = StageCollector()
    run_fast_triage_staged(issue(service="svc"), None, FakeLLM(), collector)
    assert collector.calls == []


def test_one_bad_stage_does_not_abort_the_rest(monkeypatch):
    import vishwakarma.core.fast_triage as ft

    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    collector = StageCollector()

    def _boom(*a, **kw):
        raise RuntimeError("network blip")

    # _STAGES holds direct function references captured at import time, so
    # monkeypatching the module attribute alone wouldn't affect it — patch
    # the list itself instead.
    patched = [(name, _boom if name == "Release Monitoring" else fn) for name, fn in ft._STAGES]
    monkeypatch.setattr(ft, "_STAGES", patched)

    run_fast_triage_staged(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), collector, timeout_seconds=10,
    )
    assert [name for name, _ in collector.calls] == STAGE_NAMES
    rm_text = dict(collector.calls)["Release Monitoring"]
    assert "skipped" in rm_text.lower()


def test_total_timeout_fails_open(monkeypatch):
    import time as _time
    import vishwakarma.core.fast_triage as ft

    def _slow(*a, **kw):
        _time.sleep(2)
        return "no data", {}

    patched = [(name, _slow if name == "Istio mesh" else fn) for name, fn in ft._STAGES]
    monkeypatch.setattr(ft, "_STAGES", patched)

    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    # Should return (not raise/hang) roughly at the timeout, regardless of the
    # stage still running in the background thread.
    result = run_fast_triage_staged(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), collector, timeout_seconds=0.2,
    )
    assert collector.calls == []
    assert result == ""


def test_summary_uses_fast_model_when_available():
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    run_fast_triage_staged(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLMWithFastModel(), collector, timeout_seconds=10,
    )
    istio_text = dict(collector.calls)["Istio mesh"]
    assert "Something is degraded." in istio_text


def test_no_namespace_label_means_no_namespace_filter_emitted():
    """Regression: v1 defaulted an unknown namespace to a hardcoded 'atlas' —
    v2 must skip the namespace filter entirely instead of guessing one."""
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    run_fast_triage_staged(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), collector, timeout_seconds=10,
    )
    pod_resource_calls = [q for q in prom.calls if "container_cpu_usage_seconds_total" in q]
    assert pod_resource_calls and 'namespace="atlas"' not in pod_resource_calls[0]
    assert pod_resource_calls and "namespace=" not in pod_resource_calls[0]


def test_inf_resource_usage_is_not_silently_dropped():
    responses = [
        (substr, result) for substr, result in _std_responses()
        if substr != "container_cpu_usage_seconds_total"
    ]
    responses.append(("container_cpu_usage_seconds_total", _metric_result(
        pod="beckn-driver-offer-bpp-production-abc123", _value="+Inf",
    )))
    prom = FakePrometheusToolset(responses)
    tm = FakeToolsetManager(prom)
    collector = StageCollector()
    run_fast_triage_staged(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), collector, timeout_seconds=10,
    )
    pod_text = dict(collector.calls)["Pod CPU/Mem"]
    assert "inf" in pod_text.lower()
