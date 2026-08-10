"""
Fast triage tests — deterministic Istio → Release Monitoring → pod-resource
pre-check (core/fast_triage.py).

Run:  pytest tests/test_fast_triage.py -v
"""
import uuid

import pytest

from vishwakarma.core.fast_triage import run_fast_triage
from vishwakarma.core.issue import Issue


def issue(title="HighErrorRate", **labels) -> Issue:
    return Issue(id=str(uuid.uuid4()), title=title, source="alertmanager", labels=labels)


class FakePrometheusToolset:
    """Dispatches on a substring match against the PromQL text and returns
    Prometheus's real `/api/v1/query` JSON shape — good enough to exercise
    fast_triage's query construction/parsing without a real backend."""
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
        return "Service X is failing route Y with 5xx due to Z."


def _metric_result(**metric) -> list[dict]:
    value = metric.pop("_value")
    return [{"metric": metric, "value": [0, value]}]


def _std_responses(service="beckn-driver-offer-bpp-production") -> list[tuple[str, list[dict]]]:
    return [
        ("sum by (pod, response_code, response_flags)", _metric_result(
            pod=f"{service}-abc123", response_code="503", response_flags="UF", _value="42",
        )),
        ("http_request_duration_seconds_count", _metric_result(
            method="POST", handler="/rideBooking", status_code="503", _value="30",
        )),
        ("error_counter", _metric_result(
            HttpCode="E500", ErrorCode="INTERNAL_ERROR", _value="12",
        )),
        ("container_cpu_usage_seconds_total", _metric_result(_value="87.5")),
        ("container_memory_working_set_bytes", _metric_result(_value="91.2")),
        ("kube_pod_container_status_restarts_total", _metric_result(_value="3")),
    ]


def test_known_service_label_skips_discovery():
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    result = run_fast_triage(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), timeout_seconds=10,
    )
    assert result is not None
    assert result["service"] == "beckn-driver-offer-bpp-production"
    assert result["route"] == "/rideBooking"
    assert result["cpu_pct"] == 87.5
    assert result["mem_pct"] == 91.2
    assert not any(q.startswith("topk(1,") for q in prom.calls)


def test_unknown_service_discovers_top_offender():
    responses = [
        ("topk(1,", _metric_result(
            destination_service_name="beckn-app-backend-production-pilot", _value="323",
        )),
    ] + _std_responses(service="beckn-app-backend-production-pilot")
    prom = FakePrometheusToolset(responses)
    tm = FakeToolsetManager(prom)
    result = run_fast_triage(issue(), tm, FakeLLM(), timeout_seconds=10)
    assert result is not None
    assert result["service"] == "beckn-app-backend-production-pilot"
    assert any(q.startswith("topk(1,") for q in prom.calls)


def test_no_prometheus_toolset_returns_none():
    tm = FakeToolsetManager(prom=None)
    assert run_fast_triage(issue(service="svc"), tm, FakeLLM()) is None


def test_no_toolset_manager_returns_none():
    assert run_fast_triage(issue(service="svc"), None, FakeLLM()) is None


def test_no_data_anywhere_identifies_nothing_returns_none():
    prom = FakePrometheusToolset([])  # every query → empty result
    tm = FakeToolsetManager(prom)
    assert run_fast_triage(issue(), tm, FakeLLM(), timeout_seconds=10) is None


def test_exception_in_step_fails_open(monkeypatch):
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)

    def _boom(*a, **kw):
        raise RuntimeError("network blip")

    monkeypatch.setattr("vishwakarma.core.fast_triage._release_monitoring", _boom)
    result = run_fast_triage(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), timeout_seconds=10,
    )
    assert result is None


def test_timeout_fails_open(monkeypatch):
    import time as _time

    def _slow(*a, **kw):
        _time.sleep(2)
        return "beckn-driver-offer-bpp-production", []

    monkeypatch.setattr("vishwakarma.core.fast_triage._identify_service", _slow)
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    result = run_fast_triage(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), timeout_seconds=0.2,
    )
    assert result is None


def test_summary_uses_fast_model_when_available():
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    result = run_fast_triage(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLMWithFastModel(), timeout_seconds=10,
    )
    assert result is not None
    assert "Service X is failing route Y" in result["summary_text"]
    assert "Quick Triage" in result["summary_text"]


def test_summary_falls_back_to_raw_findings_without_fast_model():
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    result = run_fast_triage(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), timeout_seconds=10,
    )
    assert result is not None
    assert "Service: beckn-driver-offer-bpp-production" in result["summary_text"]


# ── Regression coverage for the code-review fixes ──────────────────────────

def test_inf_resource_usage_is_not_silently_dropped():
    """A pod with no CPU request set makes the PromQL ratio evaluate to +Inf —
    that's the most alarming signal fast_triage can surface, so it must
    survive parsing rather than being silently dropped."""
    responses = [
        (substr, result) for substr, result in _std_responses()
        if substr != "container_cpu_usage_seconds_total"
    ]
    responses.append(("container_cpu_usage_seconds_total", _metric_result(_value="+Inf")))
    prom = FakePrometheusToolset(responses)
    tm = FakeToolsetManager(prom)
    result = run_fast_triage(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), timeout_seconds=10,
    )
    assert result is not None
    assert result["cpu_pct"] == float("inf")


def test_namespace_exclude_not_applied_when_service_already_known():
    prom = FakePrometheusToolset(_std_responses())
    tm = FakeToolsetManager(prom)
    result = run_fast_triage(
        issue(service="beckn-driver-offer-bpp-production"), tm, FakeLLM(), timeout_seconds=10,
        namespace_exclude="app-monitor",
    )
    assert result is not None
    pod_calls = [q for q in prom.calls if q.startswith("sum by (pod")]
    assert pod_calls and "destination_workload_namespace" not in pod_calls[0]


def test_namespace_exclude_applied_only_to_cluster_wide_discovery():
    responses = [
        ("topk(1,", _metric_result(
            destination_service_name="beckn-app-backend-production-pilot", _value="323",
        )),
    ] + _std_responses(service="beckn-app-backend-production-pilot")
    prom = FakePrometheusToolset(responses)
    tm = FakeToolsetManager(prom)
    result = run_fast_triage(issue(), tm, FakeLLM(), timeout_seconds=10, namespace_exclude="app-monitor")
    assert result is not None
    discover_calls = [q for q in prom.calls if q.startswith("topk(1,")]
    pod_calls = [q for q in prom.calls if q.startswith("sum by (pod")]
    assert discover_calls and 'destination_workload_namespace!="app-monitor"' in discover_calls[0]
    assert pod_calls and "destination_workload_namespace" not in pod_calls[0]
