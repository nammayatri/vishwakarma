"""
Fast triage — staged, per-dashboard Slack narration posted ahead of the deep RCA.

Mirrors the on-call's manual first response to an alert, stage by stage, each
stage's real Grafana panel queries run directly against the same
Prometheus/VictoriaMetrics backend the `prometheus` toolset already talks to:

  - Business Impact    — "Release Monitoring": rides/searches/ratio for every city, tagged
                         dropping/rising/steady vs the previous 15m — always runs, independent of routing
  - Istio mesh         — "Istio Mesh Dashboard": severity-ranked per-service 5xx/4xx/0DC codes —
                         baselines analysed behind the scenes, only the sorted service names posted
  - Release Monitoring — "Release Monitoring": failing route + app error codes
  - DB/Redis           — "KV Metrics": SQL errors, Redis call-limit breaches, P99 latency
  - Pod CPU/Mem        — "Pods / CPU New": CPU/mem %, throttling, restarts
  - Scheduler          — "Beckn - Scheduler Dashboard": job throughput/pickup delay
  - Drainer            — "Beckn Drainer Metrics": drainer stop-status/lag/pod-count/errors

Which stage(s) run, and in what order, is chosen per-alert by `_route_for_alert`
(alert-name pattern -> ordered stage list) — not every alert benefits from
every dashboard (e.g. `DriverAllocatorLooksDead` has nothing to do with Istio
5xx traffic; a node-exporter disk-full alert has no app-dashboard match at
all). Unmatched alerts fall back to the original 4-stage default. Business
Impact is the one exception — it always runs first, ahead of routing, since
sizing up ride/search impact across all cities is useful no matter which
category the alert falls into. Routing is
purely by alert title text — never by hardcoding a specific service/namespace
name into the routing table itself (dashboard *queries* do reuse a panel's own
literal filters verbatim where the real panel hardcodes one, e.g. Scheduler's
"Driver Producer Errors" — that's using the dashboard's real definition, not
inventing a new one).

Each stage is: PromQL fetch -> short fast-model interpretation -> a callback
(`on_stage_ready`) that the caller uses to post one Slack message per stage, so
the on-call sees the investigation narrated live instead of one message at the
end. No service/namespace name is ever invented — everything is discovered
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


def _query_promql_with_baseline(prom, promql: str, top_n: int, baseline_offset: str = "1h"
                                 ) -> list[tuple[dict, float, float]]:
    """Runs `promql` for now, and again with `offset <baseline_offset>`
    appended to every range-vector selector — joins each series against what
    was normal for that exact same series at that offset. This is what tells
    a service's routine baseline error rate (e.g. a steady stream of 429s
    from a chatty client) apart from a genuinely new spike, instead of a flat
    count that's noisy for high-traffic services and blind for low-traffic
    ones."""
    current = _query(prom, promql)
    baseline_promql = re.sub(r"(\[\d+[smhd]\])", rf"\1 offset {baseline_offset}", promql)
    baseline = _query(prom, baseline_promql)
    base_by_key = {tuple(sorted(l.items())): v for l, v in baseline}
    joined = [
        (labels, val, base_by_key.get(tuple(sorted(labels.items())), 0.0))
        for labels, val in current
    ]
    return sorted(joined, key=lambda r: r[1], reverse=True)[:top_n]


def _is_elevated(current: float, baseline: float, ratio_threshold: float = 3.0,
                  min_absolute: float = 20.0) -> bool:
    """Relative-to-baseline significance check: flags only when `current` is
    meaningfully above what's normal for this specific series. A series with
    a steady baseline (e.g. 300/5m of routine 429s) won't get flagged just
    for repeating that same 300; one jumping from ~5/5m to 300/5m will.
    `min_absolute` only guards the baseline==0 case (nothing to compare
    against yet) so a handful of first-ever occurrences isn't screamed about
    as "infinitely elevated"."""
    if baseline > 0:
        return current >= baseline * ratio_threshold
    return current >= min_absolute


def _sig_tag(current: float, baseline: float) -> str:
    baseline_str = f"{baseline:.2f}" if 0 < baseline < 1 else f"{int(baseline)}"
    if _is_elevated(current, baseline):
        return f"ELEVATED vs ~{baseline_str}/1h-ago" if baseline > 0 else "NEW (no 1h-ago baseline)"
    return f"baseline-normal (~{baseline_str}/1h-ago)"


def _label(labels: dict, *names: str) -> str:
    """Grafana's stackdriver DataFrame label keys aren't confirmed live (only
    read-only HTTP access was available while building this — no POST to
    /api/ds/query was possible) — try each plausible key spelling rather
    than assume one."""
    for n in names:
        if n in labels:
            return labels[n]
    return "?"


# ── Stage 0 — Business Impact (always runs, independent of alert routing) ──

_BUSINESS_IMPACT_TOP_CITIES = 15


def _trend_tag(current_ratio: float, prev_ratio: float) -> str:
    """Verdict for the current 15m ratio vs the previous 15m window: a >=10%
    relative fall is 'dropping', a >=10% relative rise is 'rising', anything
    in between is 'steady'. Relative (not absolute percentage points) so a
    2pp wobble at Bangalore's ~70% baseline doesn't scream, while Chennai's
    8% -> 5% does."""
    if prev_ratio <= 0:
        return "steady"
    change = current_ratio / prev_ratio
    if change <= 0.90:
        return "dropping"
    if change >= 1.10:
        return "rising"
    return "steady"


def _stage_business_impact(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Rides/searches/ratio per city — runs for every alert, regardless of
    which triage category matched (Istio, DB/Redis, GCP Redis/AlloyDB, or the
    unmatched default route), so the on-call can size up business impact
    before reading any of the category-specific dashboards. Same 'Rides
    Created Count' / 'Search Request Count' counters as the 'Release
    Monitoring' dashboard's own panels, just grouped across cities instead of
    one.

    Each line also compares the current 15m ratio against the previous 15m
    window ('[15m] offset 15m') and tags it dropping/rising/steady — a bare
    ratio alone can't tell you whether the city's traffic is actually sagging
    or just looking at you, and that trend is the first thing the on-call
    reads this panel for.

    `ctx["business_impact_cities"]` (from config: fast_triage.
    business_impact_cities, a merchantOperatingCityId -> display-name map) is
    the normal path — a city has one merchant_operating_city row per
    onboarded merchant, so several ids legitimately roll up to one name (e.g.
    5 ids -> "Bangalore"); those get summed together rather than shown as
    separate rows. Config missing/empty falls back to every city ranked by
    search volume under its raw id — still useful (nothing configured yet
    shouldn't mean nothing shown), just less readable."""
    ride_rows = _query(prom, 'sum by (merchantOperatingCityId) (increase(ride_created_count[15m]))')
    search_rows = _query(prom, 'sum by (merchantOperatingCityId) (increase(search_request_count[15m]))')
    prev_ride_rows = _query(prom, 'sum by (merchantOperatingCityId) (increase(ride_created_count[15m] offset 15m))')
    prev_search_rows = _query(prom, 'sum by (merchantOperatingCityId) (increase(search_request_count[15m] offset 15m))')
    ride_by_id = {l.get('merchantOperatingCityId', '?'): v for l, v in ride_rows}
    search_by_id = {l.get('merchantOperatingCityId', '?'): v for l, v in search_rows}
    prev_ride_by_id = {l.get('merchantOperatingCityId', '?'): v for l, v in prev_ride_rows}
    prev_search_by_id = {l.get('merchantOperatingCityId', '?'): v for l, v in prev_search_rows}

    city_names: dict[str, str] = ctx.get("business_impact_cities") or {}
    if city_names:
        by_name: dict[str, list[float]] = {}
        for city_id, name in city_names.items():
            agg = by_name.setdefault(name, [0.0, 0.0, 0.0, 0.0])
            agg[0] += ride_by_id.get(city_id, 0.0)
            agg[1] += search_by_id.get(city_id, 0.0)
            agg[2] += prev_ride_by_id.get(city_id, 0.0)
            agg[3] += prev_search_by_id.get(city_id, 0.0)
        rows = [(name, *vals) for name, vals in by_name.items()]
    else:
        city_ids = set(ride_by_id) | set(search_by_id)
        rows = [(f"City {cid}", ride_by_id.get(cid, 0.0), search_by_id.get(cid, 0.0),
                 prev_ride_by_id.get(cid, 0.0), prev_search_by_id.get(cid, 0.0))
                for cid in city_ids]
    rows.sort(key=lambda r: r[2], reverse=True)
    rows = rows[:max(top_n, _BUSINESS_IMPACT_TOP_CITIES)]

    lines = []
    for label, rides, searches, prev_rides, prev_searches in rows:
        if not searches:
            lines.append(f"{label} (15m): {int(rides)} rides / 0 searches — ratio n/a (0 searches)")
            continue
        ratio = rides / searches * 100
        ratio_str = f"{ratio:.1f}%"
        if prev_searches:
            prev_ratio = prev_rides / prev_searches * 100
            ratio_str += f" (prev 15m: {prev_ratio:.1f}% — {_trend_tag(ratio, prev_ratio)})"
        lines.append(f"{label} (15m): {int(rides)} rides / {int(searches)} searches — ratio {ratio_str}")
    findings = "\n".join(lines) if lines else "No ride/search data found for any city."
    return findings, {}


# ── Stage 1 — Istio mesh ────────────────────────────────────────────────────

def _stage_istio(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels: '5xx - Service Wise' + '4xx - Service Wise' (service
    ranking) and '5xx - Pod Wise'. response_code="0" (Istio's code for
    connection-level failures — no HTTP status at all, paired with a
    response_flags value like DC/DR/UF/UC) is queried and reported as its own
    "0DC" category, separate from literal HTTP 5xx, so the on-call isn't left
    guessing which failure mode dominates behind a generic "5xx" label.

    Every series is still analysed against its own 1h-ago baseline
    (_query_promql_with_baseline) — that's what separates a genuine new spike
    from a chatty client's routine 429 noise — but the Slack post is kept to
    ONE line per service with just its failing code categories, services
    ranked by severity (elevated-vs-baseline first, worst elevation ratio,
    then 5xx/0DC/4xx volume). Counts and baseline numbers stay out of the
    thread: 15 lines of per-code arithmetic made the first response
    unscannable, and the deep investigation re-derives them anyway."""
    common = 'destination_service_name!="istio-telemetry", reporter="destination"'
    known_service = ctx.get("known_service", "")

    if known_service:
        scope = f'{common}, destination_service_name="{known_service}"'
    else:
        namespace_exclude = _sanitize(ctx.get("namespace_exclude", ""))
        scope = f'{common}, destination_workload_namespace!="{namespace_exclude}"'

    promql_5xx = (
        f'sum(increase(istio_requests_total{{{scope}, response_code=~"5.."}}[5m])) '
        f'by (destination_service_name, response_code)'
    )
    promql_4xx = (
        f'sum(increase(istio_requests_total{{{scope}, response_code=~"4.."}}[5m])) '
        f'by (destination_service_name, response_code)'
    )
    promql_0dc = (
        f'sum(increase(istio_requests_total{{{scope}, response_code="0"}}[5m])) '
        f'by (destination_service_name, response_flags)'
    )
    top_5xx_rows = _query_promql_with_baseline(prom, promql_5xx, top_n)
    top_4xx_rows = _query_promql_with_baseline(prom, promql_4xx, top_n)
    top_0dc_rows = _query_promql_with_baseline(prom, promql_0dc, top_n)

    # Per-service rollup, ordered by severity. Tags are the service's distinct
    # failing code categories in 5xx -> 0DC -> 4xx order; services carrying a
    # series that jumped vs its own 1h-ago baseline get the ELEVATED suffix.
    per_svc: dict[str, dict] = {}

    def _collect(rows: list[tuple[dict, float, float]], category: str,
                 code_fn: Callable[[dict], str]) -> None:
        for labels, val, base in rows:
            name = labels.get("destination_service_name")
            if not name:
                continue
            entry = per_svc.setdefault(name, {
                "tags": [], "max_elev": 1.0,
                "volume": {"5xx": 0.0, "0DC": 0.0, "4xx": 0.0},
            })
            entry["volume"][category] += val
            if _is_elevated(val, base):
                entry["max_elev"] = max(entry["max_elev"], val / base if base > 0 else val)
            tag = code_fn(labels)
            if tag not in entry["tags"]:
                entry["tags"].append(tag)

    _collect(top_5xx_rows, "5xx", lambda l: f"HTTP {l.get('response_code', '?')}")
    _collect(top_0dc_rows, "0DC", lambda l: f"0/{l.get('response_flags') or '-'}")
    _collect(top_4xx_rows, "4xx", lambda l: f"HTTP {l.get('response_code', '?')}")

    def _severity(item: tuple[str, dict]) -> tuple:
        entry = item[1]
        return (
            entry["max_elev"] > 1.0,        # any elevated series first
            entry["max_elev"],              # worst jump vs baseline wins
            entry["volume"]["5xx"],         # then genuine server errors
            entry["volume"]["0DC"],         # then connection-level failures
            entry["volume"]["4xx"],
        )

    ranked = sorted(per_svc.items(), key=_severity, reverse=True)

    lines = [
        f"{name} — {', '.join(entry['tags'])}"
        + (" (ELEVATED vs 1h-ago)" if entry["max_elev"] > 1.0 else "")
        for name, entry in ranked
    ]
    findings = "\n".join(lines) if lines else "No 5xx/4xx/0DC traffic found."

    if known_service:
        services = [known_service]
    else:
        services = [name for name, _e in ranked][:top_n]

    return findings, {"services": services}


# ── Stage 2 — Release Monitoring ────────────────────────────────────────────

def _stage_release_monitoring(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels: 'Request Count / Route 5xx' + 'Request Count / Route
    non-200 non-5xx' (together = all non-200 traffic, matching the dashboard's
    own two panels) + '5xx Error codes' (app-level). Also 'Rides Created
    Count' + 'Search Request Count' + 'Rides To Search Ratio' when the alert
    carries a merchantOperatingCityId label (RideToSearchRatioDown/
    LowCityRides* alerts are grouped `by (merchantOperatingCityId)`, so it
    rides along on the fired alert) — same metrics + grouping the alert
    itself fires on, so the finding directly explains *why* the ratio/count
    condition tripped instead of only showing side-effects (mesh errors)."""
    lines: list[str] = []

    city_id = ctx.get("merchant_operating_city_id")
    if city_id:
        ride_rows = _query(prom, f'sum(increase(ride_created_count{{merchantOperatingCityId="{city_id}"}}[15m]))')
        search_rows = _query(prom, f'sum(increase(search_request_count{{merchantOperatingCityId="{city_id}"}}[15m]))')
        ride_count = ride_rows[0][1] if ride_rows else 0.0
        search_count = search_rows[0][1] if search_rows else 0.0
        ratio_str = f"{(ride_count / search_count * 100):.1f}%" if search_count else "n/a (0 searches)"
        lines.append(
            f"City {city_id} (15m): {int(ride_count)} rides / {int(search_count)} searches "
            f"— ratio {ratio_str}"
        )

    services = ctx.get("services") or []
    if not services:
        if lines:
            return "\n".join(lines), {}
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


# ── Stage 5 — Scheduler (driver-offer-allocator job pipeline) ──────────────

def _stage_scheduler(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels from 'Beckn - Scheduler Dashboard': 'Jobs Executed/min'
    (stream_jobs_counter, ranked by job_type/service — the exact counter
    DriverAllocatorLooksDead itself alerts on going to zero), 'Driver
    Producer Errors' (exact panel query, including its own hardcoded service
    literal — checks the job producer feeding the allocator, upstream of the
    executor), and 'Job pickup delays' p99 (unscoped, ranked by job/version —
    a stalling producer shows up here as rising delay before throughput hits
    zero)."""
    jobs = _topk_rows(_query(prom, (
        'sum(increase(stream_jobs_counter{job_type=~"Executor_.*"}[5m])) by (job_type, service)'
    )), top_n)
    producer_err = _query(prom, (
        'sum(sum(increase(producer_operation_duration_sum{operation="producer",'
        'service="beckn-driver-job-producer-service-production"}[10m])) < 10 '
        'OR absent(producer_operation_duration_sum{operation="producer",'
        'service="beckn-driver-job-producer-service-production"})) > 0'
    ))
    pickup_delay = _topk_rows(_query(prom, (
        'histogram_quantile(0.99, sum(increase(producer_operation_duration_bucket'
        '{operation=~"Job_pickup_.*"}[1m])) by (le, job, operation, version))'
    )), top_n)

    lines = []
    if jobs:
        lines.append("Jobs executed/min: " + ", ".join(
            f"{l.get('service') or l.get('job_type', '?')} ({v:.1f}/min)" for l, v in jobs
        ))
    else:
        lines.append("Jobs executed/min: no active job_type series found in the last 5m.")
    if producer_err:
        lines.append(
            f"Driver job producer: below-threshold or absent "
            f"({len(producer_err)} series flagged) — producer may be stalled upstream of the allocator."
        )
    if pickup_delay:
        lines.append("Job pickup delay p99: " + ", ".join(
            f"{l.get('job', '?')} ({v:.2f}s)" for l, v in pickup_delay
        ))
    findings = "\n".join(lines) if lines else "No scheduler pipeline data found."
    return findings, {}


# ── Stage 6 — Drainer (driver/rider drainer pipelines) ─────────────────────

def _stage_drainer(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels from 'Beckn Drainer Metrics': Driver/Rider Drainer Status
    (stop_status > 0 == not running — exactly what NoDriverDrainerRunning/
    NoRiderDrainerRunning alert on), Driver/Rider Avg Lag (drain_latency_sum/
    count, same ms-based ratio DriverDrainerLagIncreasing/
    CustomerDrainerLagIncreasing alert on), driver/app drainer pod counts
    (what NoDriverDrainerPodRunning/NoCustomerDrainerPodRunning alert on),
    and query execution failures (correlates with CustomerDrainerNotProcessing/
    DriverDrainerNotProcessing)."""
    driver_status = _query(prom, 'max(driver_drainer_stop_status)')
    rider_status = _query(prom, 'max(drainer_stop_status)')
    driver_lag = _query(prom, (
        '(sum(increase(driver_query_drain_latency_sum[1m])) / '
        'sum(increase(driver_query_drain_latency_count[1m])))'
    ))
    rider_lag = _query(prom, (
        '(sum(increase(query_drain_latency_sum[1m])) / '
        'sum(increase(query_drain_latency_count[1m])))'
    ))
    driver_pods = _query(prom, (
        'count(kube_pod_container_resource_requests{container!~"POD|",resource="cpu", '
        'unit="core", namespace=~"atlas", container=~"beckn-driver-drainer-production.*"}[120s])'
    ))
    rider_pods = _query(prom, (
        'count(kube_pod_container_resource_requests{container!~"POD|",resource="cpu", '
        'unit="core", namespace=~"atlas", container=~"beckn-app-drainer-production.*"}[120s])'
    ))
    driver_fail = _topk_rows(_query(prom, (
        'sum(increase(driver_query_execution_failure_error[5m])) by (model, action)'
    )), top_n)
    rider_fail = _topk_rows(_query(prom, (
        'sum(increase(query_execution_failure_error[5m])) by (model, action)'
    )), top_n)

    def _val(rows):
        return rows[0][1] if rows else None

    lines = []
    d_status, r_status = _val(driver_status), _val(rider_status)
    if d_status is not None or r_status is not None:
        lines.append(
            f"Drainer status: driver={'STOPPED' if d_status else 'running'}, "
            f"rider={'STOPPED' if r_status else 'running'}"
        )
    d_lag, r_lag = _val(driver_lag), _val(rider_lag)
    if d_lag is not None or r_lag is not None:
        lines.append(f"Avg lag: driver={d_lag or 0:.2f}ms, rider={r_lag or 0:.2f}ms")
    d_pods, r_pods = _val(driver_pods), _val(rider_pods)
    if d_pods is not None or r_pods is not None:
        lines.append(f"Pod count: driver-drainer={int(d_pods or 0)}, app-drainer={int(r_pods or 0)}")
    if driver_fail:
        lines.append("Driver query execution failures: " + ", ".join(
            f"{l.get('model', '?')}/{l.get('action', '?')} ({int(v)}x)" for l, v in driver_fail
        ))
    if rider_fail:
        lines.append("Rider query execution failures: " + ", ".join(
            f"{l.get('model', '?')}/{l.get('action', '?')} ({int(v)}x)" for l, v in rider_fail
        ))
    findings = "\n".join(lines) if lines else "No drainer status/lag/error data found."
    return findings, {}


# ── Stackdriver (GCP Cloud Monitoring) — Redis/AlloyDB/LB are not in Prometheus ──

_STACKDRIVER_DATASOURCE_UID = "dfdebqgdd5vk0a"  # Grafana "stackdriver" datasource — confirmed live via list_datasources
_GCP_PROJECT = "ny-prod"
_GCP_DEFAULT_URL_MAP = "k8s2-um-xxlhffi7-istio-system-istio-alb-ingress-msfkhdn4"  # public ALB ingress — the dashboard's own default selection


def _stackdriver_query(grafana, metric_type: str, extra_filters: list[list[str]] | None,
                        group_bys: list[str] | None, aligner: str, reducer: str,
                        alignment_period: str, time_from: str, time_to: str) -> list[tuple[dict, float]]:
    """
    POSTs to Grafana's generic backend-datasource proxy (/api/ds/query) using
    the exact same stackdriver timeSeriesList query shape the real dashboards
    use (captured live from their panel JSON via get_dashboard_by_uid) —
    reuses Grafana's own configured GCP credentials, so no separate GCP
    service account is needed in this pod. `extra_filters` is a list of
    [key, op, value] triples ANDed onto the base metric.type filter, mirroring
    each dashboard's own literal filter arrays.

    Response parsing targets Grafana's standard DataFrame JSON contract
    (results.<refId>.frames[].data.values) — confirmed live (2026-08-12)
    against this exact Grafana instance/plugin version via a real
    /api/ds/query POST (loadbalancing 5xx, memorystore CPU, and alloydb node
    CPU queries all returned real ny-prod data with the expected shape:
    label keys are the full "resource.label.x"/"metric.label.x" form). Still
    parses defensively and fails open (returns []) on any shape surprise
    rather than raising, in case the plugin version changes later.
    """
    filters = ["metric.type", "=", metric_type]
    for f in (extra_filters or []):
        filters += ["AND"] + list(f)
    body = {
        "queries": [{
            "refId": "A",
            "datasource": {"type": "stackdriver", "uid": _STACKDRIVER_DATASOURCE_UID},
            "queryType": "timeSeriesList",
            "timeSeriesList": {
                "projectName": _GCP_PROJECT,
                "filters": filters,
                "aggregation": {
                    "alignmentPeriod": alignment_period,
                    "crossSeriesReducer": reducer,
                    "perSeriesAligner": aligner,
                    "groupBys": group_bys or [],
                },
            },
        }],
        "from": time_from,
        "to": time_to,
    }
    try:
        r = grafana._session.post(f"{grafana.url}/api/ds/query", json=body, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.debug(f"Stackdriver query failed: {metric_type} — {e}")
        return []
    rows: list[tuple[dict, float]] = []
    for frame in data.get("results", {}).get("A", {}).get("frames", []):
        fields = frame.get("schema", {}).get("fields", [])
        values = frame.get("data", {}).get("values", [])
        if len(values) < 2 or not values[-1]:
            continue
        latest = values[-1][-1]
        if latest is None:
            continue
        labels = (fields[-1].get("labels") if fields else None) or {}
        try:
            rows.append((labels, float(latest)))
        except (TypeError, ValueError):
            continue
    return rows


def _stage_gcp_lb_5xx(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panel: 'GCP LB Cloud Monitoring Metrics' -> '5XX Error Rate' —
    loadbalancing.googleapis.com/https/request_count filtered by
    response_code_class=500, scoped to the same url_map_name the dashboard
    itself defaults to (the public ALB ingress). Compared against a 1h-ago
    baseline of the identical query — same relative-significance mechanism
    as the Istio stage, since load-balancer-level 5xx has the same
    baseline-noise risk (e.g. a chatty client's routine errors).

    Confirmed live (2026-08-12) that this project's Grafana/plugin version
    does NOT collapse results with crossSeriesReducer=REDUCE_SUM when
    groupBys is empty — it still returns one frame per (protocol,
    response_code) combination. Summing every returned frame's latest value
    client-side is correct either way: if the backend ever does reduce
    server-side there's just one frame and the sum is a no-op."""
    grafana = ctx.get("_grafana")
    if grafana is None:
        return "Grafana toolset not configured — stackdriver-backed check skipped.", {}
    url_map = ctx.get("url_map_name") or _GCP_DEFAULT_URL_MAP
    filters = [["resource.label.url_map_name", "=", url_map], ["metric.label.response_code_class", "=", "500"]]
    now_rows = _stackdriver_query(grafana, "loadbalancing.googleapis.com/https/request_count", filters, [],
                                   "ALIGN_RATE", "REDUCE_SUM", "300s", "now-5m", "now")
    base_rows = _stackdriver_query(grafana, "loadbalancing.googleapis.com/https/request_count", filters, [],
                                    "ALIGN_RATE", "REDUCE_SUM", "300s", "now-65m", "now-60m")
    current = sum(v for _l, v in now_rows)
    baseline = sum(v for _l, v in base_rows)
    findings = f"LB 5xx rate ({url_map}): {current:.2f}/s ({_sig_tag(current, baseline)})"
    return findings, {}


def _stage_gcp_redis(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels from 'GCP Memorystore for Valkey': CPU/Memory maximum
    utilization (memorystore.googleapis.com/instance/{cpu,memory}/
    maximum_utilization, already a 0-1 ratio per the dashboard's own
    percentunit formatting) and network-out throughput
    (.../stats/total_net_output_bytes_count) — covers Redis High CPU/Memory/
    Network Out from the one managed-instance dashboard."""
    grafana = ctx.get("_grafana")
    if grafana is None:
        return "Grafana toolset not configured — stackdriver-backed check skipped.", {}
    group_bys = ["resource.label.instance_id"]
    cpu = _stackdriver_query(grafana, "memorystore.googleapis.com/instance/cpu/maximum_utilization",
                              None, group_bys, "ALIGN_MEAN", "REDUCE_NONE", "300s", "now-5m", "now")
    mem = _stackdriver_query(grafana, "memorystore.googleapis.com/instance/memory/maximum_utilization",
                              None, group_bys, "ALIGN_MEAN", "REDUCE_NONE", "300s", "now-5m", "now")
    net_out = _stackdriver_query(grafana, "memorystore.googleapis.com/instance/stats/total_net_output_bytes_count",
                                  None, group_bys, "ALIGN_RATE", "REDUCE_SUM", "300s", "now-5m", "now")

    def _fmt(rows, unit_fn):
        return ", ".join(
            f"{_label(l, 'resource.label.instance_id', 'instance_id')} ({unit_fn(v)})"
            for l, v in _topk_rows(rows, top_n)
        )

    lines = []
    if cpu:
        lines.append("CPU util (max): " + _fmt(cpu, lambda v: f"{v * 100:.0f}%"))
    if mem:
        lines.append("Memory util (max): " + _fmt(mem, lambda v: f"{v * 100:.0f}%"))
    if net_out:
        lines.append("Network out: " + _fmt(net_out, lambda v: f"{v / 1e6:.2f} MB/s"))
    findings = "\n".join(lines) if lines else "No Redis/Memorystore instance metrics found."
    return findings, {}


def _stage_gcp_alloydb(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels from 'GCP AlloyDB': 'CPU Utilization - Driver DB' / '...
    Rider DB' — alloydb.googleapis.com/node/cpu/usage_time (percentunit,
    with a 0.9 threshold in the dashboard itself matching the alerts'
    own >90% condition), scoped to the dashboard's own literal instance_id
    filters (driver/rider-db-cluster-primary/-reader-pool) — covers all 4
    AlloyDB CPU-High alerts plus the general 'Alloy DB CPU Util Above 90%'
    from one stage."""
    grafana = ctx.get("_grafana")
    if grafana is None:
        return "Grafana toolset not configured — stackdriver-backed check skipped.", {}
    instances = [
        "driver-db-cluster-primary", "driver-db-cluster-reader-pool",
        "rider-db-cluster-primary", "rider-db-cluster-reader-pool",
    ]
    lines = []
    for instance_id in instances:
        rows = _stackdriver_query(
            grafana, "alloydb.googleapis.com/node/cpu/usage_time",
            [["resource.label.instance_id", "=", instance_id]],
            ["resource.label.node_id"], "ALIGN_MEAN", "REDUCE_NONE", "60s", "now-5m", "now",
        )
        if rows:
            worst = max(v for _l, v in rows)
            lines.append(f"{instance_id}: {worst * 100:.0f}% (worst node)")
    findings = "\n".join(lines) if lines else "No AlloyDB node CPU metrics found."
    return findings, {}


def _stage_clickhouse(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panel from 'ClickHouse Metrics': 'Disk Used % (default)' — exact
    panel formula (algebraically reduces to 100*Used/Total; kept verbatim
    rather than simplified since it's the dashboard's real definition), plus
    memory and failed-query-rate for context since disk pressure often
    correlates with a merge/insert backlog."""
    disk = _topk_rows(_query(prom, (
        '100 * (1 - ClickHouseAsyncMetrics_DiskUsed_default / ClickHouseAsyncMetrics_DiskTotal_default * 0 '
        '- (ClickHouseAsyncMetrics_DiskTotal_default - ClickHouseAsyncMetrics_DiskUsed_default) '
        '/ ClickHouseAsyncMetrics_DiskTotal_default)'
    )), top_n)
    mem = _topk_rows(_query(prom, 'ClickHouseAsyncMetrics_MemoryResident'), top_n)
    failed = _topk_rows(_query(prom, 'sum by (instance) (rate(ClickHouseProfileEvents_FailedQuery[5m]))'), top_n)

    lines = []
    if disk:
        lines.append("Disk used %: " + ", ".join(f"{l.get('instance', '?')} ({v:.1f}%)" for l, v in disk))
    if mem:
        lines.append("Memory resident: " + ", ".join(f"{l.get('instance', '?')} ({v / 1e9:.2f} GB)" for l, v in mem))
    if failed:
        lines.append("Failed query rate: " + ", ".join(f"{l.get('instance', '?')} ({v:.2f}/s)" for l, v in failed))
    findings = "\n".join(lines) if lines else "No ClickHouse disk/memory/query data found."
    return findings, {}


# ── Formatting + driver ──────────────────────────────────────────────────────

def _format_stage_findings(stage_name: str, findings: str) -> str:
    """Posts the stage's own findings verbatim as bullet points — no LLM
    rewrite into prose. Each stage already builds `findings` as one line per
    datapoint (e.g. "5xx routes: A [HTTP 500] (12x, ELEVATED vs ~2x/1h-ago)")
    with the significance judgment (baseline-normal/ELEVATED/NEW) already
    encoded in the line itself; running that back through an LLM to produce
    "2-3 terse sentences" only re-narrates already-structured data into
    vaguer paragraphs and strips the per-datapoint detail."""
    header = f":mag: *Quick Triage — {stage_name}*"
    if not findings.strip():
        return f"{header}\n(no data)"
    bullets = "\n".join(f"• {line}" for line in findings.strip().split("\n") if line.strip())
    return f"{header}\n{bullets}"


_STAGE_FNS: dict[str, Callable] = {
    "Business Impact": _stage_business_impact,
    "Istio mesh": _stage_istio,
    "Release Monitoring": _stage_release_monitoring,
    "DB/Redis": _stage_db_redis,
    "Pod CPU/Mem": _stage_pod_resources,
    "Scheduler": _stage_scheduler,
    "Drainer": _stage_drainer,
    "ClickHouse": _stage_clickhouse,
    "GCP LB 5xx": _stage_gcp_lb_5xx,
    "GCP Redis": _stage_gcp_redis,
    "GCP AlloyDB": _stage_gcp_alloydb,
}

_DEFAULT_ROUTE: list[str] = ["Istio mesh", "Release Monitoring", "DB/Redis", "Pod CPU/Mem"]

# Ordered (pattern, stage-list) pairs, checked top to bottom — first match
# wins. Matched against the alert's own title text, case-insensitively.
# Derived from the real VictoriaMetrics alert rules (beckn-alerts + node-exporter
# groups) and confirmed live against each dashboard's actual panel queries.
_ALERT_ROUTES: list[tuple[re.Pattern, list[str]]] = [
    # Scheduler dashboard owns the driver-offer-allocator job pipeline —
    # Istio 5xx/4xx traffic is irrelevant to a stalled job executor.
    (re.compile(r"DriverAllocatorLooksDead", re.I), ["Scheduler"]),
    # All 8 drainer-family alerts (driver + rider/customer variants) map to
    # the one Drainer Metrics dashboard, not the generic service dashboards.
    (re.compile(r"(NoDriverDrainerRunning|NoRiderDrainerRunning|NoDriverDrainerPodRunning|"
                r"NoCustomerDrainerPodRunning|DriverDrainerLagIncreasing|CustomerDrainerLagIncreasing|"
                r"CustomerDrainerNotProcessing|DriverDrainerNotProcessing)", re.I), ["Drainer"]),
    # Ride/search ratio + low-city-rides alerts are traffic-shape symptoms —
    # check the mesh for what's actually erroring first, then the app-level
    # route/error-code detail (confirmed order with the user).
    (re.compile(r"(RideToSearchRatioDown|LowCityRides)", re.I), ["Istio mesh", "Release Monitoring"]),
    # node-exporter alerts are host/OS-level (disk, network iface, conntrack,
    # clock, RAID, fd limits) — none of the 4 app dashboards apply, and
    # running them would just add noise ahead of the deep investigation.
    (re.compile(r"^Node[A-Z]", re.I), []),
    # GCP-native Cloud Monitoring alert policies (ny-prod project) — a
    # different layer entirely from the VictoriaMetrics beckn-alerts above.
    (re.compile(r"GCP ELB 5xx Alert", re.I), ["GCP LB 5xx"]),
    (re.compile(r"Redis\s*High\s*(Network Out|Memory|CPU)", re.I), ["GCP Redis"]),
    (re.compile(r"Alloy\s?DB.*CPU", re.I), ["GCP AlloyDB"]),
    (re.compile(r"Clickhouse disk usage", re.I), ["ClickHouse"]),
    # VM Instance (tummoc-db-v2) CPU/Mem/Disk — no matching dashboard found
    # yet (searched Grafana for vm instance/compute engine/tummoc/mtc) —
    # skip rather than invent queries with nothing to verify them against.
    (re.compile(r"VM Instance.*tummoc-db-v2", re.I), []),
]


def _route_for_alert(title: str) -> list[str]:
    for pattern, stages in _ALERT_ROUTES:
        if pattern.search(title or ""):
            return stages
    return _DEFAULT_ROUTE


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
    text = _format_stage_findings(name, findings)
    on_stage_ready(name, text)
    return text


def _run_triage_stages(prom, grafana, llm, issue, on_stage_ready: Callable[[str, str], None],
                        top_n: int, namespace_exclude: str, business_impact_cities: dict[str, str]) -> str:
    labels = issue.labels or {}
    ctx = {
        "known_service": _sanitize(labels.get("service") or labels.get("job") or ""),
        "namespace": _sanitize(labels.get("namespace") or labels.get("exported_namespace") or "") or None,
        "namespace_exclude": namespace_exclude,
        "services": [],
        "_grafana": grafana,
        "url_map_name": _sanitize(labels.get("url_map_name") or "") or None,
        "merchant_operating_city_id": _sanitize(labels.get("merchantOperatingCityId") or "") or None,
        "business_impact_cities": business_impact_cities,
    }
    # Business Impact always runs first, ahead of the alert-specific route —
    # it's independent of which category (Istio/DB-Redis/GCP-Redis/AlloyDB/
    # default) matched, so it isn't gated by `_route_for_alert` like the rest.
    all_stages = ["Business Impact"] + _route_for_alert(issue.title)
    summaries = [
        _run_stage(name, _STAGE_FNS[name], prom, ctx, top_n, llm, issue.title, on_stage_ready)
        for name in all_stages
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
    business_impact_cities: dict[str, str] | None = None,
) -> str:
    """
    Runs whichever stages `_route_for_alert(issue.title)` picks for this
    specific alert, sequentially — for an unmatched alert that's the default
    4-stage triage (Istio mesh -> Release Monitoring -> DB/Redis -> Pod
    CPU/Mem), where later stages use the service(s) identified by the Istio
    stage; for a matched alert (e.g. DriverAllocatorLooksDead -> Scheduler
    only) it's just the relevant dashboard(s), and for alerts with no
    matching dashboard at all (node-exporter) it's an empty route — no Slack
    messages post, the deep investigation runs alone. Calls
    `on_stage_ready(stage_name, summary_text)` after each stage so the caller
    can post one Slack message per stage as the investigation narrates live,
    instead of one message at the end.

    Also returns the concatenated per-stage summary text once all stages
    finish (or the total budget is exhausted) — callers that only care about
    the Slack narration can ignore the return value; callers that want to
    seed a system prompt with the findings (e.g. as pre-investigation evidence
    for a matched runbook) can use it.

    `business_impact_cities` (config: fast_triage.business_impact_cities) is
    a merchantOperatingCityId -> display-name map for the Business Impact
    stage — unset/empty falls back to every city under its raw id.

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
    grafana = toolset_manager.get("grafana")  # None if not configured — stackdriver-backed stages skip themselves

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_run_triage_stages, prom, grafana, llm, issue, on_stage_ready, top_n,
                          namespace_exclude, business_impact_cities or {})
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
