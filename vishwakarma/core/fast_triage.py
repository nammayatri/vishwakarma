"""
Fast triage — staged, per-dashboard Slack narration posted ahead of the deep RCA.

Mirrors the on-call's manual first response to an alert, stage by stage, each
stage's real Grafana panel queries run directly against the same
Prometheus/VictoriaMetrics backend the `prometheus` toolset already talks to:

  1. Istio mesh       — "Istio Mesh Dashboard": 5xx/4xx by service + pod-wise detail
  2. Release Monitoring — "Release Monitoring": failing route + app error codes
  3. DB/Redis          — "KV Metrics": SQL errors, Redis call-limit breaches, P99 latency
  4. Pod CPU/Mem       — "Pods / CPU New": CPU/mem %, throttling, restarts

Each stage is: PromQL fetch -> short fast-model interpretation -> a callback
(`on_stage_ready`) that the caller uses to post one Slack message per stage, so
the on-call sees the investigation narrated live instead of one message at the
end. No service/namespace name is ever hardcoded — everything is discovered
from the alert's own labels or ranked live from the metrics themselves.

Runs alongside (never blocking) the pre-enrichment + agentic investigation.
Fails open at both the per-stage level (one bad stage doesn't kill the rest)
and the whole-pipeline level (a hard total time budget, after which whatever
already posted stands and the rest is silently skipped).
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Callable

log = logging.getLogger(__name__)

_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.\-]")


def _sanitize(value: str) -> str:
    """Alert labels are untrusted input — restrict to a safe PromQL label charset."""
    return _SAFE_LABEL.sub("", value or "")[:200]


def _query(prom, promql: str) -> list[tuple[dict, float]]:
    """
    Query Prometheus's stable `/api/v1/query` JSON contract directly, via the
    toolset's own `_get()` transport (same base URL/auth/timeout it already
    uses). Bypasses `PrometheusToolset._format_instant`'s display-text
    formatting — that format exists for LLM readability and can change
    independently; raw JSON is Prometheus's own external API and won't.
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


def _topk_rows(rows: list[tuple[dict, float]], n: int) -> list[tuple[dict, float]]:
    """Rank full rows by value descending, keep the top n — never collapses
    multiple concurrent issues down to a single max()/top-1."""
    return sorted(rows, key=lambda r: r[1], reverse=True)[:n]


def _topk_names(rows: list[tuple[dict, float]], label_key: str, n: int) -> list[tuple[str, float]]:
    """Rank rows by value descending, dedupe by one label, keep the top n names."""
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    for labels, val in sorted(rows, key=lambda r: r[1], reverse=True):
        name = labels.get(label_key)
        if name and name not in seen:
            seen.add(name)
            out.append((name, val))
        if len(out) >= n:
            break
    return out


# ── Stage 1 — Istio mesh ────────────────────────────────────────────────────

def _stage_istio(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels: '5xx - Service Wise' + '4xx - Service Wise' (service
    ranking) and '5xx - Pod Wise' (already broad — excludes only 2xx/3xx/4xx,
    so it also catches response_code "0" + response_flags like DC/UF/UC,
    i.e. connection-level failures with no HTTP status at all)."""
    common = 'destination_service_name!="istio-telemetry", reporter="destination"'
    known_service = ctx.get("known_service", "")

    if known_service:
        scope = f'{common}, destination_service_name="{known_service}"'
    else:
        namespace_exclude = _sanitize(ctx.get("namespace_exclude", ""))
        scope = f'{common}, destination_workload_namespace!="{namespace_exclude}"'

    rows_5xx = _query(prom, (
        f'sum(increase(istio_requests_total{{{scope}, response_code!~"(2..|3..|4..)"}}[5m])) '
        f'by (destination_service_name, response_code)'
    ))
    rows_4xx = _query(prom, (
        f'sum(increase(istio_requests_total{{{scope}, response_code!~"(2..|3..|5..)"}}[5m])) '
        f'by (destination_service_name, response_code)'
    ))
    top_5xx = _topk_names(rows_5xx, "destination_service_name", top_n)
    top_4xx = _topk_names(rows_4xx, "destination_service_name", top_n)

    if known_service:
        services = [known_service]
    else:
        services, seen = [], set()
        for name, _val in top_5xx + top_4xx:
            if name not in seen:
                seen.add(name)
                services.append(name)
        services = services[:top_n]

    pod_rows: list[tuple[dict, float]] = []
    if services:
        svc_match = "|".join(re.escape(s) for s in services)
        pod_q = (
            f'sum by (destination_service_name, pod, response_code, response_flags) '
            f'(increase(istio_requests_total{{{common}, destination_service_name=~"{svc_match}", '
            f'response_code!~"(2..|3..|4..)", pod!=""}}[5m]))'
        )
        pod_rows = _topk_rows(_query(prom, pod_q), top_n)

    lines = []
    if top_5xx:
        lines.append("5xx by service: " + ", ".join(f"{n} ({int(v)}x)" for n, v in top_5xx))
    if top_4xx:
        lines.append("4xx by service: " + ", ".join(f"{n} ({int(v)}x)" for n, v in top_4xx))
    if pod_rows:
        pod_lines = [
            f"{labels.get('pod', '?')} [{labels.get('response_code', '?')}/{labels.get('response_flags') or '-'}] {int(v)}x"
            for labels, v in pod_rows
        ]
        lines.append("Worst pods: " + "; ".join(pod_lines))
    findings = "\n".join(lines) if lines else "No 5xx/4xx traffic found."

    return findings, {"services": services}


# ── Stage 2 — Release Monitoring ────────────────────────────────────────────

def _stage_release_monitoring(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels: 'Request Count / Route 5xx' + 'Request Count / Route
    non-200 non-5xx' (together = all non-200 traffic, matching the dashboard's
    own two panels) + '5xx Error codes' (app-level)."""
    services = ctx.get("services") or []
    if not services:
        return "No service identified from the mesh check — skipped.", {}
    svc_match = "|".join(re.escape(s) for s in services)

    rows_5xx = _query(prom, (
        f'sum(increase(http_request_duration_seconds_count{{service=~"{svc_match}", '
        f'status_code=~"5[0-9]{{2}}"}}[5m])) by (method, handler, status_code)'
    ))
    rows_other = _query(prom, (
        f'sum(increase(http_request_duration_seconds_count{{service=~"{svc_match}", '
        f'status_code!="200", status_code!~"5.."}}[5m])) by (method, handler, status_code)'
    ))
    rows_err = _query(prom, (
        f'sum(increase(error_counter{{job=~"{svc_match}", HttpCode=~"E[45][0-9]{{2}}", '
        f'ErrorContext="DEFAULT_ERROR"}}[5m])) by (HttpCode, ErrorCode)'
    ))

    top_5xx = _topk_rows(rows_5xx, top_n)
    top_other = _topk_rows(rows_other, top_n)
    top_err = _topk_rows(rows_err, top_n)

    lines = []
    if top_5xx:
        lines.append("5xx routes: " + ", ".join(
            f"{labels.get('method', '?')} {labels.get('handler', '?')} ({int(v)}x)" for labels, v in top_5xx
        ))
    if top_other:
        lines.append("Other non-200 routes: " + ", ".join(
            f"{labels.get('method', '?')} {labels.get('handler', '?')} [{labels.get('status_code', '?')}] ({int(v)}x)"
            for labels, v in top_other
        ))
    if top_err:
        lines.append("App error codes: " + ", ".join(
            f"{labels.get('ErrorCode', '?')} ({int(v)}x)" for labels, v in top_err
        ))
    findings = "\n".join(lines) if lines else "No route-level errors found for the affected service(s)."
    return findings, {}


# ── Stage 3 — DB/Redis choking ──────────────────────────────────────────────

def _stage_db_redis(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels from the 'KV Metrics' dashboard (Prometheus-native app-level
    KVConnector instrumentation) — shared infra, not scoped to a specific
    service: SQL error rate, Redis soft/hard call-limit breaches (literal
    choking), P99 handler latency, all ranked by worst DB table."""
    sql_err = _topk_rows(_query(prom, f'sum by (model) (increase(kv_sql_error_counter[5m]))'), top_n)
    hard_limit = _topk_rows(_query(prom, f'sum by (model) (increase(kvRedis_hard_db_limit_exceeded[5m]))'), top_n)
    soft_limit = _topk_rows(_query(prom, f'sum by (model) (increase(kvRedis_soft_db_limit_exceeded[5m]))'), top_n)
    p99 = _topk_rows(_query(prom, (
        'sort_desc(histogram_quantile(0.99, sum by (model, le) (rate(kv_handler_latency_bucket[5m]))))'
    )), top_n)

    lines = []
    if sql_err:
        lines.append("SQL errors by table: " + ", ".join(f"{l.get('model', '?')} ({int(v)}x)" for l, v in sql_err))
    if hard_limit:
        lines.append("Redis HARD limit breaches: " + ", ".join(f"{l.get('model', '?')} ({int(v)}x)" for l, v in hard_limit))
    if soft_limit:
        lines.append("Redis soft limit breaches: " + ", ".join(f"{l.get('model', '?')} ({int(v)}x)" for l, v in soft_limit))
    if p99:
        lines.append("Worst P99 handler latency: " + ", ".join(f"{l.get('model', '?')} ({v:.2f}s)" for l, v in p99))
    findings = "\n".join(lines) if lines else "No DB/Redis error or latency pressure detected."
    return findings, {}


# ── Stage 4 — Pod CPU/Mem ───────────────────────────────────────────────────

def _stage_pod_resources(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels from 'Pods / CPU New': CPU%/mem% of requested, plus 'Pods
    throttled' (container_cpu_cfs_throttled_periods_total — a genuine
    choking signal missing from v1) and 'Pods Restarts'. Grouped per-pod
    (rather than summed across all matched pods, as the dashboard panel
    does) so the top offenders can be ranked instead of collapsed to one
    aggregate ratio."""
    services = ctx.get("services") or []
    if not services:
        return "No service identified from the mesh check — skipped.", {}
    svc_match = "|".join(re.escape(s) for s in services)
    namespace = ctx.get("namespace")
    ns_clause = f', namespace="{namespace}"' if namespace else ""

    cpu = _topk_rows(_query(prom, (
        f'topk({top_n}, sum by (pod) (rate(container_cpu_usage_seconds_total{{pod=~"({svc_match}).*"{ns_clause}, '
        f'image!="", container!="POD"}}[1m])) / sum by (pod) (kube_pod_container_resource_requests{{'
        f'resource="cpu", pod=~"({svc_match}).*"{ns_clause}, container!="POD"}}) * 100)'
    )), top_n)
    mem = _topk_rows(_query(prom, (
        f'topk({top_n}, sum by (pod) (container_memory_working_set_bytes{{pod=~"({svc_match}).*"{ns_clause}, '
        f'image!="", container!="POD"}}) / sum by (pod) (kube_pod_container_resource_requests{{'
        f'resource="memory", pod=~"({svc_match}).*"{ns_clause}, container!="POD"}}) * 100)'
    )), top_n)
    throttled = _topk_rows(_query(prom, (
        f'topk({top_n}, sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{pod=~"({svc_match}).*"{ns_clause}, '
        f'image!="", container!="POD"}}[1m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{{'
        f'pod=~"({svc_match}).*"{ns_clause}, container!="POD"}}[1m])) * 100)'
    )), top_n)
    restarts = _topk_rows(_query(prom, (
        f'topk({top_n}, sum by (pod) (increase(kube_pod_container_status_restarts_total{{'
        f'pod=~"({svc_match}).*"{ns_clause}}}[15m])))'
    )), top_n)

    lines = []
    if cpu:
        lines.append("CPU % of requested: " + ", ".join(f"{l.get('pod', '?')} ({v:.0f}%)" for l, v in cpu))
    if mem:
        lines.append("Mem % of requested: " + ", ".join(f"{l.get('pod', '?')} ({v:.0f}%)" for l, v in mem))
    if throttled:
        lines.append("CPU throttled %: " + ", ".join(f"{l.get('pod', '?')} ({v:.0f}%)" for l, v in throttled))
    if restarts:
        lines.append("Restarts (15m): " + ", ".join(f"{l.get('pod', '?')} ({int(v)}x)" for l, v in restarts))
    findings = "\n".join(lines) if lines else "No CPU/mem/restart pressure detected."
    return findings, {}


# ── AI interpretation + driver ──────────────────────────────────────────────

def _ai_interpret_stage(llm, stage_name: str, alert_title: str, findings: str) -> str:
    header = f":mag: *Quick Triage — {stage_name}*"
    if not findings.strip():
        return f"{header}\n(no data)"
    if not llm or not getattr(llm.cfg, "fast_model", None):
        return f"{header}\n```\n{findings}\n```"
    prompt = (
        f"You just ran the '{stage_name}' check as one step of a staged first-look "
        "triage on a firing alert (more checks follow after this one). Given these "
        "raw findings, write 2-3 terse plain-English sentences: what's failing or "
        "degraded and how bad. No markdown headers, no preamble.\n\n"
        f"Alert: {alert_title}\n\n{findings}"
    )
    try:
        text = (llm.summarize(prompt) or "").strip()
    except Exception as e:
        log.warning(f"Fast triage stage interpretation failed ({stage_name}): {e}")
        text = ""
    return f"{header}\n{text or findings}"


_STAGES: list[tuple[str, Callable]] = [
    ("Istio mesh", _stage_istio),
    ("Release Monitoring", _stage_release_monitoring),
    ("DB/Redis", _stage_db_redis),
    ("Pod CPU/Mem", _stage_pod_resources),
]


def _run_stage(name: str, fn: Callable, prom, ctx: dict, top_n: int,
                llm, alert_title: str, on_stage_ready: Callable[[str, str], None]) -> str:
    try:
        findings, ctx_update = fn(prom, ctx, top_n)
    except Exception as e:
        log.warning(f"Fast triage stage {name!r} failed: {e}")
        text = f":warning: *Quick Triage — {name}*\nskipped: {e}"
        on_stage_ready(name, text)
        return text
    ctx.update(ctx_update)
    text = _ai_interpret_stage(llm, name, alert_title, findings)
    on_stage_ready(name, text)
    return text


def _run_triage_stages(prom, llm, issue, on_stage_ready: Callable[[str, str], None],
                        top_n: int, namespace_exclude: str) -> str:
    labels = issue.labels or {}
    ctx = {
        "known_service": _sanitize(labels.get("service") or labels.get("job") or ""),
        "namespace": _sanitize(labels.get("namespace") or labels.get("exported_namespace") or "") or None,
        "namespace_exclude": namespace_exclude,
        "services": [],
    }
    summaries = [
        _run_stage(name, fn, prom, ctx, top_n, llm, issue.title, on_stage_ready)
        for name, fn in _STAGES
    ]
    return "\n\n".join(summaries)


def run_fast_triage_staged(
    issue,
    toolset_manager,
    llm,
    on_stage_ready: Callable[[str, str], None],
    timeout_seconds: int = 240,
    top_n: int = 5,
    namespace_exclude: str = "app-monitor",
) -> str:
    """
    Runs the 4-stage triage (Istio mesh -> Release Monitoring -> DB/Redis ->
    Pod CPU/Mem) sequentially — later stages use the service(s) identified by
    the Istio stage. Calls `on_stage_ready(stage_name, summary_text)` after
    each stage so the caller can post one Slack message per stage as the
    investigation narrates live, instead of one message at the end.

    Also returns the concatenated per-stage summary text once all stages
    finish (or the total budget is exhausted) — callers that only care about
    the Slack narration can ignore the return value; callers that want to
    seed a system prompt with the findings (e.g. as pre-investigation evidence
    for a matched runbook) can use it.

    Fails open at two levels: a single stage erroring doesn't stop the rest
    (`on_stage_ready` still gets called, noting it was skipped), and the whole
    pipeline is capped at `timeout_seconds` total — past that, whatever
    already posted stands, the return value is `""`, and anything left is
    silently dropped. Never raises.
    """
    if toolset_manager is None:
        return ""
    prom = toolset_manager.get("prometheus")
    if prom is None:
        return ""

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_run_triage_stages, prom, llm, issue, on_stage_ready, top_n, namespace_exclude)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        log.warning(
            f"Fast triage exceeded its {timeout_seconds}s total budget for alert "
            f"{issue.title!r} — stopping; stages already posted stand."
        )
        return ""
    except Exception as e:
        log.warning(f"Fast triage failed (non-fatal): {e}")
        return ""
    finally:
        pool.shutdown(wait=False)
