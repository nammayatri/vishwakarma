"""
Fast triage — deterministic first-look pre-check, posted before the deep RCA.

Mirrors the on-call's manual first response to an alert: check the Istio mesh
for which service/route is throwing errors, then pod CPU/memory for resource
pressure. All three PromQL steps hit the same Prometheus/VictoriaMetrics
backend the `prometheus` toolset already talks to (real panel queries pulled
from the "Istio Mesh Dashboard", "Release Monitoring", and "Pods / CPU New"
Grafana dashboards) — no new integration.

Runs alongside (never blocking) the existing pre-enrichment + agentic
investigation. Always fails open: any error or timeout returns None and the
deep investigation proceeds exactly as it does today.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

log = logging.getLogger(__name__)

_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.\-]")


def _sanitize(value: str) -> str:
    """Alert labels are untrusted input — restrict to a safe PromQL label charset."""
    return _SAFE_LABEL.sub("", value or "")[:200]


def _query(prom, promql: str) -> list[tuple[dict, float]]:
    """
    Query Prometheus's stable `/api/v1/query` JSON contract directly, via the
    toolset's own `_get()` transport (same base URL/auth/timeout it already
    uses). Deliberately bypasses `PrometheusToolset._format_instant`'s
    display-text formatting — that format exists for LLM readability and can
    change independently; the raw JSON `metric`/`value` shape is Prometheus's
    own external API and won't. `float()` parses "+Inf"/"-Inf"/"NaN" directly.
    """
    try:
        data = prom._get("/api/v1/query", {"query": promql})
    except Exception as e:
        log.debug(f"Fast triage query failed: {promql[:120]} — {e}")
        return []
    rows = []
    for r in data.get("data", {}).get("result", []):
        value = r.get("value")
        if not value or len(value) < 2:
            continue
        try:
            val = float(value[1])
        except (TypeError, ValueError):
            continue
        rows.append((r.get("metric", {}), val))
    return rows


def _top(rows: list[tuple[dict, float]], label_key: str) -> tuple[str, float] | None:
    if not rows:
        return None
    labels, val = max(rows, key=lambda r: r[1])
    name = labels.get(label_key)
    return (name, val) if name else None


def _identify_service(prom, known_service: str, namespace_exclude: str) -> tuple[str, list]:
    """Step 1 — Istio 5xx. Uses the alert's own service if known, otherwise
    discovers the top offending service cluster-wide — same as checking the
    Istio Mesh Dashboard first when the alert doesn't already name a service."""
    common_filter = (
        'response_code!~"(2..|3..|4..)", destination_service_name!="istio-telemetry", '
        'reporter="destination"'
    )
    if known_service:
        service = known_service
    else:
        # namespace_exclude only applies to cluster-wide discovery — it keeps
        # Argus's own monitoring namespace out of "which service is failing"
        # ranking. Once a service is known/scoped it's dropped: the alert may
        # legitimately name a service that lives in that namespace.
        discover_filter = f'{common_filter}, destination_workload_namespace!="{_sanitize(namespace_exclude)}"'
        discover_q = (
            f"topk(1, sum(increase(istio_requests_total{{{discover_filter}}}[5m])) "
            f"by (destination_service_name))"
        )
        top = _top(_query(prom, discover_q), "destination_service_name")
        if not top:
            return "", []
        service = top[0]

    pod_q = (
        f"sum by (pod, response_code, response_flags) (increase(istio_requests_total{{"
        f'{common_filter}, destination_service_name="{service}", pod!=""}}[5m]))'
    )
    return service, _query(prom, pod_q)


def _release_monitoring(prom, service: str) -> dict:
    """Step 2 — which exact API/route is failing on the identified service."""
    route_q = (
        f'topk(3, sum(increase(http_request_duration_seconds_count{{service=~"{service}", '
        f'status_code=~"5[0-9]{{2}}"}}[5m])) by (method, handler, status_code))'
    )
    err_q = (
        f'topk(3, sum(increase(error_counter{{job=~"{service}", HttpCode=~"E5[0-9]{{2}}", '
        f'ErrorContext="DEFAULT_ERROR"}}[5m])) by (HttpCode, ErrorCode))'
    )
    route_rows = _query(prom, route_q)
    err_rows = _query(prom, err_q)
    top_route_labels = max(route_rows, key=lambda r: r[1])[0] if route_rows else {}
    top_route = _top(route_rows, "handler")
    top_err = _top(err_rows, "ErrorCode")
    return {
        "route": top_route[0] if top_route else None,
        "route_count": top_route[1] if top_route else None,
        "method": top_route_labels.get("method"),
        "status_code": top_route_labels.get("status_code"),
        "error_code": top_err[0] if top_err else None,
        "error_count": top_err[1] if top_err else None,
    }


def _pod_resources(prom, service: str, namespace: str) -> dict:
    """Step 3 — is this resource pressure? CPU/mem % of requests, recent restarts."""
    cpu_q = (
        f'sum(rate(container_cpu_usage_seconds_total{{pod=~"{service}.*", namespace="{namespace}", '
        f'image!="", container!="POD"}}[1m])) / sum(kube_pod_container_resource_requests{{'
        f'resource="cpu", pod=~"{service}.*", namespace="{namespace}", container!="POD"}}) * 100'
    )
    mem_q = (
        f'(sum(container_memory_working_set_bytes{{namespace="{namespace}", image!="", '
        f'container!="POD", pod=~"{service}.*"}}) / sum(kube_pod_container_resource_requests{{'
        f'resource="memory", namespace="{namespace}", container!="POD", pod=~"{service}.*"}})) * 100'
    )
    restarts_q = (
        f'sum(increase(kube_pod_container_status_restarts_total{{namespace="{namespace}", '
        f'pod=~"{service}.*"}}[15m]))'
    )

    def _single(promql: str) -> float | None:
        rows = _query(prom, promql)
        return rows[0][1] if rows else None

    return {
        "cpu_pct": _single(cpu_q),
        "mem_pct": _single(mem_q),
        "restarts": _single(restarts_q),
    }


def _compose_findings(service: str, pod_rows: list, route_info: dict, resource_info: dict) -> str:
    lines = [f"Service: {service}"]
    if pod_rows:
        labels, count = max(pod_rows, key=lambda r: r[1])
        pod_name = labels.get("pod", "?")
        code = labels.get("response_code", "?")
        flags = labels.get("response_flags", "")
        suffix = f" (flags={flags})" if flags else ""
        lines.append(f"Top failing pod: {pod_name} — {int(count)}x {code} errors in last 5m{suffix}")
    if route_info.get("route"):
        lines.append(
            f"Failing route: {route_info.get('method') or '?'} {route_info['route']} "
            f"({route_info.get('status_code') or '5xx'}) — {int(route_info.get('route_count') or 0)}x in 5m"
        )
    if route_info.get("error_code"):
        lines.append(f"App error code: {route_info['error_code']} — {int(route_info.get('error_count') or 0)}x in 5m")
    if resource_info.get("cpu_pct") is not None:
        lines.append(f"Pod CPU usage: {resource_info['cpu_pct']:.0f}% of requested")
    if resource_info.get("mem_pct") is not None:
        lines.append(f"Pod memory usage: {resource_info['mem_pct']:.0f}% of requested")
    if resource_info.get("restarts"):
        lines.append(f"Pod restarts (last 15m): {int(resource_info['restarts'])}")
    return "\n".join(lines)


def _interpret(llm, alert_title: str, findings: str) -> str:
    header = ":mag: *Quick Triage* _(automated pre-check — deep investigation continues below)_"
    if not llm or not getattr(llm.cfg, "fast_model", None):
        return f"{header}\n```\n{findings}\n```"
    prompt = (
        "You are an SRE doing the first 60-second look at a firing alert, before the deep "
        "investigation starts. Given this automated pre-check data (mesh error rate, failing "
        "route, pod resource usage), write a 3-5 line interpretation: which service/route is "
        "failing, how bad, and whether it looks like resource pressure or something else. Be "
        "terse, plain sentences, no markdown headers, no preamble.\n\n"
        f"Alert: {alert_title}\n\n{findings}"
    )
    try:
        text = (llm.summarize(prompt) or "").strip()
    except Exception as e:
        log.warning(f"Fast triage interpretation failed: {e}")
        text = ""
    return f"{header}\n{text or findings}"


def _run_triage_steps(prom, llm, issue, namespace_exclude: str) -> dict | None:
    labels = issue.labels or {}
    known_service = _sanitize(labels.get("service") or labels.get("job") or "")
    namespace = _sanitize(
        labels.get("namespace") or labels.get("exported_namespace") or "atlas"
    )

    service, pod_rows = _identify_service(prom, known_service, namespace_exclude)
    if not service:
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        route_future = pool.submit(_release_monitoring, prom, service)
        resource_future = pool.submit(_pod_resources, prom, service, namespace)
        route_info = route_future.result()
        resource_info = resource_future.result()

    findings = _compose_findings(service, pod_rows, route_info, resource_info)
    summary_text = _interpret(llm, issue.title, findings)

    return {
        "service": service,
        "route": route_info.get("route"),
        "cpu_pct": resource_info.get("cpu_pct"),
        "mem_pct": resource_info.get("mem_pct"),
        "summary_text": summary_text,
    }


def run_fast_triage(
    issue,
    toolset_manager,
    llm,
    timeout_seconds: int = 45,
    namespace_exclude: str = "app-monitor",
) -> dict | None:
    """
    Deterministic pre-flight triage: Istio 5xx → Release Monitoring route →
    pod CPU/mem, the same order an on-call checks manually. Returns a dict
    with a ready-to-post `summary_text`, or None if unavailable/failed/timed
    out. Never raises and never blocks past `timeout_seconds` — the deep
    investigation must proceed unaffected either way.
    """
    if toolset_manager is None:
        return None
    prom = toolset_manager.get("prometheus")
    if prom is None:
        return None

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_run_triage_steps, prom, llm, issue, namespace_exclude)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        log.warning(f"Fast triage timed out after {timeout_seconds}s for alert {issue.title!r}")
        return None
    except Exception as e:
        log.warning(f"Fast triage failed (non-fatal): {e}")
        return None
    finally:
        pool.shutdown(wait=False)
