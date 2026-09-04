"""
Fast triage — staged, per-dashboard Slack narration posted ahead of the deep RCA.

Mirrors the on-call's manual first response to an alert, stage by stage, each
stage's real Grafana panel queries run directly against the same
Prometheus/VictoriaMetrics backend the `prometheus` toolset already talks to:

  - Business Impact    — "Release Monitoring": rides/searches/ratio for every city, tagged
                         dropping/rising/steady vs the previous 15m — always runs, independent of routing
  - Istio mesh         — "Istio Mesh Dashboard": ELEVATED-only per-service 5xx/4xx/0DC codes —
                         baseline-normal services stay out of the Slack post entirely
  - Release Monitoring — "Release Monitoring": failing route + app error codes
  - DB/Redis           — "KV Metrics": SQL errors, P99 handler latency
  - Pod CPU/Mem        — "Pods / CPU New": CPU/mem %, throttling, restarts
  - Scheduler          — "Beckn - Scheduler Dashboard": job throughput/pickup delay
  - Drainer            — "Beckn Drainer Metrics": drainer stop-status/lag/pod-count/errors
  - Logs & Infra       — checks pod health (CrashLoopBackOff / restarts) then stern's the
                         mesh-ELEVATED service(s) directly (bypasses the LLM loop) and
                         classifies the infra failure mode from log text (Redis timeout,
                         no healthy upstream, OOM, connection refused, request timeout,
                         ...) — diagnostic only, no prescribed fix

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
import json
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
    findings = "\n".join(lines) if lines else ""
    return findings, {}


# ── Stage 1 — Istio mesh ────────────────────────────────────────────────────

def _stage_istio(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels: '5xx - Service Wise' + '4xx - Service Wise' (service
    ranking) and '5xx - Pod Wise'. response_code="0" (Istio's code for
    connection-level failures — no HTTP status at all, paired with a
    response_flags value like DC/DR/UF/UC) is queried and reported as its own
    "0DC" category, separate from literal HTTP 5xx, so the on-call isn't left
    guessing which failure mode dominates behind a generic "5xx" label.

    Every series is analysed against its own 1h-ago baseline
    (_query_promql_with_baseline) — that's what separates a genuine new spike
    from a chatty client's routine 429 noise. The Slack post is ELEVATED-only:
    one line per (service, category) that's actually above its own baseline —
    `svc — 0DC [0/DC]` — not every service present in the traffic mix. A
    service sitting at its usual 429 rate doesn't get a line just for being
    in the top-N; the full ranked mix (elevated or not) still flows into ctx
    for downstream stages that need it (Release Monitoring, Pod CPU/Mem)."""
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

    def _category_rank(rows: list[tuple[dict, float, float]], category: str,
                        code_fn: Callable[[dict], str]) -> tuple[list[str], list[str], list[str]] | None:
        """Ranks services within one category (5xx/0DC/4xx): any
        baseline-elevated series first (worst jump wins), then raw volume.
        Returns (all ranked names, elevated-only names, one Slack line per
        ELEVATED service — `svc — CATEGORY [codes]`). Baseline-normal
        services still count toward `names` (other stages use the full mix
        to decide what to query), but never get their own Slack line — a
        service just sitting in the traffic mix at its usual rate isn't
        something on-call needs to be told about."""
        per_svc: dict[str, dict] = {}
        for labels, val, base in rows:
            name = labels.get("destination_service_name")
            if not name:
                continue
            entry = per_svc.setdefault(name, {"max_elev": 1.0, "volume": 0.0, "codes": []})
            entry["volume"] += val
            if _is_elevated(val, base):
                entry["max_elev"] = max(entry["max_elev"], val / base if base > 0 else val)
            code = code_fn(labels)
            if code not in entry["codes"]:
                entry["codes"].append(code)
        if not per_svc:
            return None
        ranked = sorted(
            per_svc.items(),
            key=lambda kv: (kv[1]["max_elev"] > 1.0, kv[1]["max_elev"], kv[1]["volume"]),
            reverse=True,
        )
        names = [name for name, _e in ranked]
        elevated = [name for name, e in ranked if e["max_elev"] > 1.0]
        elevated_lines = [f"{name} — {category} [{', '.join(per_svc[name]['codes'])}]" for name in elevated]
        return names, elevated, elevated_lines

    rendered = [
        _category_rank(top_5xx_rows, "5xx", lambda l: f"HTTP {l.get('response_code', '?')}"),
        _category_rank(top_0dc_rows, "0DC", lambda l: f"0/{l.get('response_flags') or '-'}"),
        _category_rank(top_4xx_rows, "4xx", lambda l: f"HTTP {l.get('response_code', '?')}"),
    ]
    lines = [ln for r in rendered if r for ln in r[2]]
    findings = "\n".join(lines) if lines else ""

    if known_service:
        services = [known_service]
        elevated_services = [known_service]
    else:
        services: list[str] = []
        elevated_services: list[str] = []
        for r in rendered:
            if r is None:
                continue
            names, elev, _lines = r
            services += [n for n in names if n not in services]
            elevated_services += [n for n in elev if n not in elevated_services]
        services = services[:top_n]
        elevated_services = elevated_services[:top_n]

    return findings, {"services": services, "elevated_services": elevated_services}


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
        return "", {}
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
    findings = "\n".join(lines) if lines else ""

    # Failing route/API names (ranked, deduped) — handed to Logs & Infra so
    # it can grep stern output for the specific API's failure lines instead
    # of just a generic service-wide error scan.
    routes: list[str] = []
    for labels, _v in (top_5xx + top_other):
        handler = labels.get("handler")
        if handler and handler != "?" and handler not in routes:
            routes.append(handler)
    return findings, {"routes": routes[:top_n]}


# ── Stage 3 — DB/Redis choking ──────────────────────────────────────────────

def _stage_db_redis(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels from the 'KV Metrics' dashboard (Prometheus-native app-level
    KVConnector instrumentation) — shared infra, not scoped to a specific
    service: SQL error rate + P99 handler latency, ranked by worst DB table.

    Deliberately does NOT report `kvRedis_{soft,hard}_db_limit_exceeded`
    ("Redis limit breaches") — verified against the real source
    (euler-hs/src/EulerHS/KVConnector/{Flow,Utils}.hs) that this is a
    query-shape/scaling smell (a KVConnector find fanned out into >2500/5000
    individual Redis calls for one query), not an outage/timeout signal, and
    not actionable for on-call triage — confirmed with the user, drop it."""
    # A `model` label exists in these count metrics as soon as it's EVER had
    # a data point — most rows in any topk are legitimately 0x (nothing
    # breached for that table). Drop them; only tables that actually
    # errored in the last 5m are worth a line.
    sql_err = [r for r in _topk_rows(_query(prom, f'sum by (model) (increase(kv_sql_error_counter[5m]))'), top_n) if r[1] > 0]
    p99 = _topk_rows(_query(prom, (
        'sort_desc(histogram_quantile(0.99, sum by (model, le) (rate(kv_handler_latency_bucket[5m]))))'
    )), top_n)

    lines = []
    if sql_err:
        lines.append("SQL errors by table: " + ", ".join(f"{l.get('model', '?')} ({int(v)}x)" for l, v in sql_err))
    if p99:
        lines.append("Worst P99 handler latency: " + ", ".join(f"{l.get('model', '?')} ({v:.2f}s)" for l, v in p99))
    findings = "\n".join(lines) if lines else ""
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
        return "", {}
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
    findings = "\n".join(lines) if lines else ""
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
    findings = "\n".join(lines) if lines else ""
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
    findings = "\n".join(lines) if lines else ""
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


_INFRA_SIGNATURES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"redis[^\n]{0,120}(timeout|timed out|connection refused|connection reset|econnreset|"
                r"max retries|limit breach|connection (is )?closed)", re.I),
     "Redis connection/limit issue — pod can't reliably reach Redis/Memorystore"),
    (re.compile(r"(too many connections|connection pool exhausted|remaining connection slots are reserved)", re.I),
     "DB connection pool exhausted"),
    (re.compile(r"(oomkilled|out of memory|cannot allocate memory)", re.I),
     "OOM — pod killed / allocation failed for exceeding memory limit"),
    (re.compile(r"(no healthy upstream|upstream connect error|connection reset by peer|ucupstream)", re.I),
     "No healthy upstream — destination pod(s) not ready / crashlooping"),
    (re.compile(r"(context deadline exceeded|i/o timeout|dial tcp[^\n]{0,40}timeout|request timeout)", re.I),
     "Request timeout calling a downstream dependency"),
    (re.compile(r"connection refused", re.I),
     "Connection refused — dependency unreachable / not accepting connections"),
    (re.compile(r"(panic:|nullpointerexception|unhandled exception|fatal error)", re.I),
     "Application panic / unhandled exception"),
]


def _find_infra_match(lines: list[str]) -> tuple[str, str, "re.Pattern"] | None:
    """Scans from the most recent line backwards; the first line matching a
    known signature wins — label, line and pattern all come from that SAME
    line, so what's reported as evidence is always what actually justified
    the label (not just "the last line", which could be unrelated noise)."""
    for ln in reversed(lines):
        for pattern, label in _INFRA_SIGNATURES:
            if pattern.search(ln):
                return label, ln, pattern
    return None


def _find_route_match(lines: list[str], route_terms: list[str]) -> str | None:
    for ln in reversed(lines):
        if any(re.search(rt, ln, re.I) for rt in route_terms):
            return ln
    return None


def _snippet(line: str, pattern: "re.Pattern | None" = None, width: int = 160) -> str:
    """A window of `line` around whatever `pattern` actually matched, not
    just the first `width` chars — a structured JSON log line's interesting
    part is rarely at the start, and a start-anchored slice tends to just
    show field boilerplate (hostname/pid/...) with the real evidence cut off."""
    m = pattern.search(line) if pattern else None
    if not m:
        return line[:width] + ("…" if len(line) > width else "")
    start = max(0, m.start() - 40)
    end = min(len(line), start + width)
    return ("…" if start > 0 else "") + line[start:end] + ("…" if end < len(line) else "")


def _log_targets(ctx: dict) -> list[tuple[str, str | None]]:
    """(service, namespace) pairs to stern, in priority order:
    1. the alert's own `service`/`job` label — explicit, always relevant
    2. services the Istio-mesh stage flagged as ELEVATED (not just present
       in the traffic mix — a service ranked #3 in a "5xx: a, b, c" line
       with no (ELEVATED) tag is baseline-normal noise, not something to
       stern)
    3. the configured `service_hints` fallback (fast_triage.service_hints in
       config.yaml), but ONLY when the mesh stage found no traffic at all to
       judge (empty `services`) — if it found traffic and none of it was
       elevated, that's a real "nothing abnormal here" signal, not a gap to
       fill with a guess."""
    namespace = ctx.get("namespace")
    if ctx.get("known_service"):
        return [(ctx["known_service"], namespace)]
    if ctx.get("elevated_services"):
        return [(s, namespace) for s in ctx["elevated_services"][:3]]
    if ctx.get("services"):
        return []  # mesh checked — nothing was actually elevated
    hints = ctx.get("service_hints") or []
    return [(h.get("service", ""), h.get("namespace") or namespace) for h in hints[:3] if h.get("service")]


def _pod_health_lines(prom, targets: list[tuple[str, str | None]], top_n: int) -> list[str]:
    """Recent container restarts + last termination reason for the pods
    behind `targets` — kube-state-metrics, two cheap Prometheus queries, no
    log parsing required. Tells "the pod itself is unhealthy" apart from
    "the pod is fine but a dependency is slow/erroring", which the
    log-signature scan below can't distinguish on its own.

    NOT literally `kube_pod_container_status_waiting_reason{reason=
    "CrashLoopBackOff"}` — confirmed live against this deployment's own
    VictoriaMetrics (2026-09-03) that its kube-state-metrics build doesn't
    expose that metric at all (zero series; only container_info,
    resource_limits/requests, status_restarts_total and
    status_terminated_reason exist here). `terminated_reason` (Error/
    OOMKilled/ContainerStatusUnknown) paired with a live restart count is
    the closest available proxy for the same on-call question ("is this pod
    actually crashing") on THIS cluster — verify against your own
    `{__name__=~"kube_pod.*"}` label values before assuming otherwise."""
    svc_names = [_sanitize(s) for s, _ns in targets if s]
    svc_names = [s for s in svc_names if s]
    if not svc_names:
        return []
    svc_match = "|".join(re.escape(s) for s in svc_names)
    namespace = next((ns for _s, ns in targets if ns), None)
    ns_clause = f', namespace="{_sanitize(namespace)}"' if namespace else ""

    terminated = _query(prom, (
        f'kube_pod_container_status_terminated_reason{{reason=~"Error|OOMKilled|ContainerStatusUnknown", '
        f'pod=~"({svc_match}).*"{ns_clause}}} == 1'
    ))
    restarts = _topk_rows(_query(prom, (
        f'topk({top_n}, sum by (pod) (increase(kube_pod_container_status_restarts_total{{'
        f'pod=~"({svc_match}).*"{ns_clause}}}[15m])))'
    )), top_n)

    reason_by_pod: dict[str, str] = {}
    for labels, _v in terminated:
        pod = labels.get("pod", "?")
        reason_by_pod.setdefault(pod, labels.get("reason", "?"))

    lines = []
    reported: set[str] = set()
    for labels, v in restarts:
        if v <= 0:
            continue
        pod = labels.get("pod", "?")
        reported.add(pod)
        line = f"{pod}: {int(v)} container restart(s) in last 15m"
        reason = reason_by_pod.get(pod)
        if reason:
            line += f" (last termination: {reason})"
        lines.append(line)
    for pod, reason in reason_by_pod.items():
        if pod in reported:
            continue
        lines.append(f"{pod}: last termination: {reason}")
    return lines


_ES_LOGS_INDEX = "beckn-logs-*"


def _es_top_errors(es_tool, svc: str, size: int = 3, since: str = "now-10m") -> list[str]:
    """Volume-ranked error categories for `svc` from the beckn-logs-*
    Elasticsearch index (fluentd-shipped app logs) — confirmed live against
    this deployment's real schema: Rust/bunyan-format services (their
    `hostname` field IS the pod name) log a `response_code`/`tag` pair per
    error (e.g. tag "[INCOMING API - ERROR]", response_code
    "INVALID_REQUEST") — this ranks those by doc_count so on-call sees which
    error DOMINATES ("INVALID_REQUEST (16666x), UNSERVICEABLE (4637x)"), not
    just one sampled line. Haskell-format services embed their error text in
    free-form `log_message` instead of a clean field, so this naturally
    returns nothing for them — not a gap: their app-level error codes are
    already ranked by the Release Monitoring stage's `error_counter` metric.
    Fails open (returns []) on any ES error/timeout/shape surprise."""
    query = {
        "bool": {
            "filter": [
                {"range": {"@timestamp": {"gte": since}}},
                {"wildcard": {"hostname.keyword": f"{svc}*"}},
                {"bool": {"should": [
                    {"range": {"level": {"gte": 50}}},
                    {"wildcard": {"tag.keyword": "*ERROR*"}},
                ], "minimum_should_match": 1}},
            ]
        }
    }
    aggs = {"top_response_code": {"terms": {"field": "response_code.keyword", "size": size}}}
    try:
        out = es_tool.execute("elasticsearch_aggregate", {
            "index": _ES_LOGS_INDEX, "query": query, "aggs": aggs, "size": 0,
        })
    except Exception as e:
        log.debug(f"ES top-error query failed for {svc}: {e}")
        return []
    from vishwakarma.core.models import ToolStatus
    if out.status != ToolStatus.SUCCESS or not out.output:
        return []
    try:
        agg_results = json.loads(out.output)
    except Exception:
        return []
    buckets = agg_results.get("top_response_code", {}).get("buckets", [])
    return [f"{b['key']} ({b['doc_count']}x)" for b in buckets if b.get("key") and b.get("doc_count")]


def _stage_logs_infra(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real log dig — NOT routed through the LLM/agentic loop, so it lands
    inside the same few-second quick-triage window as the metric stages
    instead of waiting on the (up to 40-step, minutes-long) deep
    investigation. For `_log_targets` (known_service -> mesh-ELEVATED
    services only -> configured service_hints fallback): first checks pod
    health directly (CrashLoopBackOff / recent restarts, via Prometheus —
    always runs, no toolset dependency), then runs `stern` via the bash
    toolset with a grep pattern built from the known infra-failure
    signatures plus any failing route/API names `_stage_release_monitoring`
    already ranked — so what comes back is already narrowed to
    plausibly-relevant lines instead of a generic "error|timeout|..."
    bag-of-words that matches half of any structured JSON log stream. Only
    posts a line when it actually resolves to a pod-health issue, a
    classified infra condition, or a route/API-specific hit — unclassifiable
    noise (or no elevated service at all) contributes nothing rather than a
    wall of raw JSON. Caps at 3 services and a short stern window to stay
    well inside budget."""
    targets = _log_targets(ctx)
    if not targets:
        if ctx.get("services") and not ctx.get("elevated_services"):
            return "", {}
        return "", {}

    lines = _pod_health_lines(prom, targets, top_n)

    es_tool = ctx.get("_es")
    if es_tool is not None:
        for raw_svc, _ns in targets:
            svc = _sanitize(raw_svc)
            if not svc:
                continue
            top_errors = _es_top_errors(es_tool, svc)
            if top_errors:
                lines.append(f"{svc}: top errors (ES, last 10m) — " + ", ".join(top_errors))

    bash_tool = ctx.get("_bash")
    if bash_tool is None:
        findings = "\n".join(lines) if lines else ""
        return findings, {}

    # Route/handler label values reach here from Prometheus, not from a
    # fixed set of choices — they get embedded inside a single-quoted shell
    # argument below, so anything that could break out of that quoting
    # (', `, $, \, newline) is dropped rather than escaped. re.escape()
    # neutralizes regex metacharacters but NOT shell quoting, so this check
    # has to happen before the pattern is built, not be assumed away by it.
    routes = ctx.get("routes") or []
    _unsafe_in_shell_quotes = re.compile(r"['`$\\\n]")
    route_terms = [
        re.escape(r) for r in routes
        if r and r != "?" and not _unsafe_in_shell_quotes.search(r)
    ]
    sig_terms = [p.pattern for p, _ in _INFRA_SIGNATURES]
    grep_pattern = "|".join(f"({t})" for t in sig_terms + route_terms)

    from vishwakarma.core.models import ToolStatus

    for raw_svc, raw_ns in targets:
        svc = _sanitize(raw_svc)
        if not svc:
            continue
        ns = _sanitize(raw_ns) if raw_ns else ""
        ns_flag = f"-n {ns}" if ns else "-A"
        cmd = (
            f"stern '{svc}' {ns_flag} --since=10m --no-follow "
            f"--template '{{{{.Message}}}}{{{{\"\\n\"}}}}' 2>/dev/null "
            f"| grep -iE '{grep_pattern}' | tail -50"
        )
        try:
            out = bash_tool.execute("bash", {"command": cmd})
        except Exception as e:
            log.debug(f"Log dig failed for {svc}: {e}")
            continue
        if out.status != ToolStatus.SUCCESS:
            continue
        text = (out.output or "").strip()
        if not text:
            continue
        matched = text.splitlines()

        infra_hit = _find_infra_match(matched)
        if infra_hit:
            label, ln, pattern = infra_hit
            lines.append(f"{svc}: {label} — e.g. \"{_snippet(ln, pattern)}\"")
            continue
        if route_terms:
            route_ln = _find_route_match(matched, route_terms)
            if route_ln:
                lines.append(f"{svc}: API failure — e.g. \"{_snippet(route_ln)}\"")
                continue
        # Matched the retrieval grep (so there WAS something error-shaped)
        # but nothing classified — deliberately not reported; a raw
        # unclassified dump is exactly the noise this stage should avoid.
    findings = "\n".join(lines) if lines else ""
    return findings, {}


def _stage_gcp_redis(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    grafana = ctx.get("_grafana")
    if grafana is None:
        return "Grafana toolset not configured — stackdriver-backed check skipped.", {}
    node_group_bys = ["resource.label.instance_id", "resource.label.node_id"]
    cpu = _stackdriver_query(grafana, "memorystore.googleapis.com/instance/node/cpu/utilization",
                              None, node_group_bys, "ALIGN_MEAN", "REDUCE_NONE", "300s", "now-5m", "now")
    mem = _stackdriver_query(grafana, "memorystore.googleapis.com/instance/node/memory/utilization",
                              None, node_group_bys, "ALIGN_MEAN", "REDUCE_NONE", "300s", "now-5m", "now")
    net_out = _stackdriver_query(grafana, "memorystore.googleapis.com/instance/stats/total_net_output_bytes_count",
                                  None, ["resource.label.instance_id"], "ALIGN_RATE", "REDUCE_SUM", "300s", "now-5m", "now")

    def _fmt(rows, unit_fn):
        by_instance: dict[str, list[tuple[dict, float]]] = {}
        for labels, v in rows:
            inst = _label(labels, "resource.label.instance_id", "instance_id")
            by_instance.setdefault(inst, []).append((labels, v))
        ranked = sorted(by_instance.items(), key=lambda kv: max(v for _l, v in kv[1]), reverse=True)[:top_n]
        parts = []
        for inst, entries in ranked:
            avg = sum(v for _l, v in entries) / len(entries)
            worst_labels, worst = max(entries, key=lambda e: e[1])
            node = _label(worst_labels, "resource.label.node_id", "node_id")
            node_tag = f", node {node}" if node != "?" else ""
            parts.append(f"{inst} (avg {unit_fn(avg)}, worst {unit_fn(worst)}{node_tag})")
        return ", ".join(parts)

    lines = []
    if cpu:
        lines.append("CPU util (max): " + _fmt(cpu, lambda v: f"{v * 100:.0f}%"))
    if mem:
        lines.append("Memory util (max): " + _fmt(mem, lambda v: f"{v * 100:.0f}%"))
    if net_out:
        lines.append("Network out: " + _fmt(net_out, lambda v: f"{v / 1e6:.2f} MB/s"))
    findings = "\n".join(lines) if lines else ""
    return findings, {}


_REDIS_SCAN_ITER = 5
_REDIS_SCAN_COUNT = 2000
_REDIS_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_REDIS_NUM_RE = re.compile(r"\d{3,}")


def _redis_cli(bash_tool, host: str, port, args: str) -> str | None:
    from vishwakarma.core.models import ToolStatus
    cmd = f"redis-cli -h {host} -p {port} --no-auth-warning {args}"
    try:
        out = bash_tool.execute("bash", {"command": cmd})
    except Exception as e:
        log.debug(f"redis-cli failed ({host}:{port} {args[:40]}): {e}")
        return None
    if out.status != ToolStatus.SUCCESS:
        return None
    return (out.output or "").strip()


def _parse_info_field(info_text: str, field: str, numeric: bool = True):
    for ln in info_text.splitlines():
        ln = ln.strip()
        if ln.startswith(f"{field}:"):
            val = ln.split(":", 1)[1].strip()
            if not numeric:
                return val
            try:
                return float(val)
            except ValueError:
                return None
    return None


def _redis_bigvalue_sample(bash_tool, host: str, port, sample_size: int = 30) -> str | None:
    out = _redis_cli(bash_tool, host, port, f"SCAN 0 COUNT {sample_size}")
    if not out:
        return None
    rows = out.splitlines()
    keys = rows[1:] if len(rows) > 1 else []
    biggest = None
    for k in keys[:sample_size]:
        size_out = _redis_cli(bash_tool, host, port, f"MEMORY USAGE {k}")
        if not size_out:
            continue
        try:
            size = int(size_out)
        except ValueError:
            continue
        if biggest is None or size > biggest[1]:
            biggest = (k, size)
    if biggest is None:
        return None
    return f"{biggest[0]} (~{biggest[1] / 1e6:.1f}MB, sampled {len(keys)} keys)"


def _redis_key_pattern_histogram(bash_tool, host: str, port) -> str | None:
    from collections import Counter
    counter: Counter = Counter()
    cursor = "0"
    total_sampled = 0
    for _ in range(_REDIS_SCAN_ITER):
        out = _redis_cli(bash_tool, host, port, f"SCAN {cursor} COUNT {_REDIS_SCAN_COUNT}")
        if not out:
            break
        rows = out.splitlines()
        if not rows:
            break
        cursor = rows[0].strip()
        keys = rows[1:]
        total_sampled += len(keys)
        for k in keys:
            norm = _REDIS_NUM_RE.sub("<N>", _REDIS_UUID_RE.sub("<UUID>", k))
            counter[norm] += 1
        if cursor == "0" or not cursor.isdigit():
            break
    if not counter:
        return None
    top_pattern, top_count = counter.most_common(1)[0]
    pct = (top_count / total_sampled * 100) if total_sampled else 0
    return f'"{top_pattern}" ({top_count}/{total_sampled} sampled, {pct:.0f}%)'


def _redis_hotspot_check(bash_tool, inst_name: str, spec: dict) -> str | None:
    host, port, mode = spec["host"], spec["port"], spec["mode"]
    target_host, target_port = host, port
    shard_note = ""

    if mode == "cluster":
        nodes_out = _redis_cli(bash_tool, host, port, "CLUSTER NODES")
        if not nodes_out:
            return f"{inst_name}: unreachable (CLUSTER NODES failed) — PSC IP may have drifted, re-check `gcloud memorystore instances list`"
        masters = []
        for ln in nodes_out.splitlines():
            parts = ln.split()
            if len(parts) < 3 or "master" not in parts[2]:
                continue
            addr = parts[1].split("@")[0]
            if ":" in addr:
                h, p = addr.rsplit(":", 1)
                if p.isdigit():
                    masters.append((h, p))
        if not masters:
            return f"{inst_name}: CLUSTER NODES returned no master shards"
        worst = None
        for h, p in masters[:16]:
            mem_out = _redis_cli(bash_tool, h, p, "INFO memory")
            used = _parse_info_field(mem_out, "used_memory") if mem_out else None
            if used is None:
                continue
            if worst is None or used > worst[2]:
                worst = (h, p, used)
        if worst is None:
            return f"{inst_name}: reached {len(masters)} shard(s) but none responded to INFO memory"
        target_host, target_port, worst_used = worst
        shard_note = f" (hot shard {target_host}:{target_port}, {worst_used / 1e6:.0f}MB)"

    info_mem = _redis_cli(bash_tool, target_host, target_port, "INFO memory")
    if not info_mem:
        return f"{inst_name}{shard_note}: unreachable for INFO memory"
    used_h = _parse_info_field(info_mem, "used_memory_human", numeric=False)
    max_h = _parse_info_field(info_mem, "maxmemory_human", numeric=False)
    policy = _parse_info_field(info_mem, "maxmemory_policy", numeric=False)
    dataset_pct = _parse_info_field(info_mem, "used_memory_dataset_perc", numeric=False)

    line = f"{inst_name}{shard_note}: used {used_h or '?'} / max {max_h or '?'} (policy {policy or '?'}, dataset {dataset_pct or '?'})"

    info_stats = _redis_cli(bash_tool, target_host, target_port, "INFO stats")
    evicted = _parse_info_field(info_stats, "evicted_keys") if info_stats else None
    if evicted is not None and evicted > 0:
        line += f" — evicted_keys={int(evicted)} (past maxmemory, LRU actively discarding)"

    dataset_val = None
    if dataset_pct:
        try:
            dataset_val = float(dataset_pct.rstrip("%"))
        except ValueError:
            dataset_val = None

    if dataset_val is not None and dataset_val >= 70:
        top_key = _redis_bigvalue_sample(bash_tool, target_host, target_port)
        if top_key:
            line += f" — big-value candidate: {top_key}"
    elif dataset_val is not None:
        pattern = _redis_key_pattern_histogram(bash_tool, target_host, target_port)
        if pattern:
            line += f" — top key pattern: {pattern}"

    return line


def _stage_redis_hotspot(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    bash_tool = ctx.get("_bash")
    if bash_tool is None:
        return "bash toolset not configured — redis-cli hotspot check skipped.", {}
    redis_instances = ctx.get("redis_instances") or {}
    if not redis_instances:
        return "", {}
    alerted = ctx.get("gcp_resource_instance_id")
    targets = {alerted: redis_instances[alerted]} if alerted in redis_instances else redis_instances
    lines = []
    for inst_name, spec in targets.items():
        line = _redis_hotspot_check(bash_tool, inst_name, spec)
        if line:
            lines.append(line)
    findings = "\n".join(lines) if lines else ""
    return findings, {}


def _stage_gcp_alloydb(prom, ctx: dict, top_n: int) -> tuple[str, dict]:
    """Real panels from 'GCP AlloyDB': 'CPU Utilization - Driver DB' / '...
    Rider DB' — alloydb.googleapis.com/node/cpu/usage_time (percentunit,
    with a 0.9 threshold in the dashboard itself matching the alerts'
    own >90% condition), scoped to the dashboard's own literal instance_id
    filters (driver/rider-db-cluster-primary/-reader-pool) — covers all 4
    AlloyDB CPU-High alerts plus the general 'Alloy DB CPU Util Above 90%'
    from one stage. Reports both avg (cluster-wide skew/hotspot signal) and
    worst node, with that worst node's own node_id so on-call doesn't have
    to go look it up separately."""
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
            avg = sum(v for _l, v in rows) / len(rows)
            worst_labels, worst = max(rows, key=lambda r: r[1])
            worst_node = _label(worst_labels, "resource.label.node_id", "node_id")
            lines.append(f"{instance_id}: avg {avg * 100:.0f}%, worst {worst * 100:.0f}% (node {worst_node})")
    findings = "\n".join(lines) if lines else ""
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
    findings = "\n".join(lines) if lines else ""
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
    "Redis Hotspot": _stage_redis_hotspot,
    "GCP AlloyDB": _stage_gcp_alloydb,
    "Logs & Infra": _stage_logs_infra,
}

_DEFAULT_ROUTE: list[str] = ["Istio mesh", "Release Monitoring", "DB/Redis", "Pod CPU/Mem", "Logs & Infra"]

_ALERT_ROUTES: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"DriverAllocatorLooksDead", re.I), ["Scheduler"]),
    (re.compile(r"(NoDriverDrainerRunning|NoRiderDrainerRunning|NoDriverDrainerPodRunning|"
                r"NoCustomerDrainerPodRunning|DriverDrainerLagIncreasing|CustomerDrainerLagIncreasing|"
                r"CustomerDrainerNotProcessing|DriverDrainerNotProcessing)", re.I), ["Drainer"]),
    (re.compile(r"(RideToSearchRatioDown|LowCityRides)", re.I), ["Istio mesh", "Release Monitoring", "Logs & Infra"]),
    (re.compile(r"^Node[A-Z]", re.I), []),
    (re.compile(r"GCP ELB 5xx Alert", re.I), ["Istio mesh", "GCP LB 5xx", "Logs & Infra"]),
    (re.compile(r"GCPRedis|Redis\s*High\s*(Network Out|Memory|CPU)", re.I), ["GCP Redis", "Redis Hotspot", "Logs & Infra"]),
    (re.compile(r"Alloy\s?DB.*CPU", re.I), ["GCP AlloyDB", "Logs & Infra"]),
    (re.compile(r"Clickhouse disk usage", re.I), ["ClickHouse"]),
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
    if not (findings or "").strip():
        # Genuinely nothing to report (every "no X found"/"skipped" case is
        # `""`, by convention, across every stage function) — stay silent
        # rather than post a "(no data)" placeholder message. Also excluded
        # from the joined evidence text fed to the deep investigation: a
        # "nothing here" line isn't worth the tokens there either.
        return ""
    text = _format_stage_findings(name, findings)
    on_stage_ready(name, text)
    return text


def _resolve_service_hints(title: str, service_hints: dict[str, list[dict]]) -> list[dict]:
    """First alert-title regex match wins, same style as `_route_for_alert`
    — lets on-call encode "if it's X, go check Y" (fast_triage.service_hints
    in config.yaml) as the Logs & Infra stage's last-resort fallback when
    live discovery finds no service at all."""
    for pattern, hints in service_hints.items():
        try:
            if re.search(pattern, title or "", re.I):
                return hints
        except re.error:
            log.warning(f"Invalid service_hints regex, skipping: {pattern!r}")
    return []


def _run_triage_stages(prom, grafana, bash_tool, es_tool, llm, issue, on_stage_ready: Callable[[str, str], None],
                        top_n: int, namespace_exclude: str, business_impact_cities: dict[str, str],
                        service_hints: dict[str, list[dict]], redis_instances: dict[str, dict]) -> str:
    labels = issue.labels or {}
    ctx = {
        "known_service": _sanitize(labels.get("service") or labels.get("job") or ""),
        "namespace": _sanitize(labels.get("namespace") or labels.get("exported_namespace") or "") or None,
        "namespace_exclude": namespace_exclude,
        "services": [],
        "_grafana": grafana,
        "_bash": bash_tool,
        "_es": es_tool,
        "url_map_name": _sanitize(labels.get("url_map_name") or "") or None,
        "merchant_operating_city_id": _sanitize(labels.get("merchantOperatingCityId") or "") or None,
        "business_impact_cities": business_impact_cities,
        "service_hints": _resolve_service_hints(issue.title, service_hints or {}),
        "redis_instances": redis_instances or {},
        "gcp_resource_instance_id": _sanitize(labels.get("instance_id") or ""),
    }
    # Business Impact always runs first, ahead of the alert-specific route —
    # it's independent of which category (Istio/DB-Redis/GCP-Redis/AlloyDB/
    # default) matched, so it isn't gated by `_route_for_alert` like the rest.
    all_stages = ["Business Impact"] + _route_for_alert(issue.title)
    summaries = [
        _run_stage(name, _STAGE_FNS[name], prom, ctx, top_n, llm, issue.title, on_stage_ready)
        for name in all_stages
    ]
    return "\n\n".join(s for s in summaries if s)


def run_fast_triage_staged(
    issue,
    toolset_manager,
    llm,
    on_stage_ready: Callable[[str, str], None],
    timeout_seconds: int = 240,
    top_n: int = 5,
    namespace_exclude: str = "app-monitor",
    business_impact_cities: dict[str, str] | None = None,
    service_hints: dict[str, list[dict]] | None = None,
    redis_instances: dict[str, dict] | None = None,
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

    `service_hints` (config: fast_triage.service_hints) is an alert-title
    regex -> [{"service": ..., "namespace": ...}] map — the Logs & Infra
    stage's last-resort fallback when live discovery (Istio mesh ranking,
    the alert's own `service` label) finds nothing, e.g. a Redis-spike alert
    that carries no service label of its own but on-call knows to check
    location-tracking-service for.

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
    bash_tool = toolset_manager.get("bash")  # None if not configured — Logs & Infra's stern dig skips itself
    es_tool = toolset_manager.get("elasticsearch")  # None if not configured — ES top-errors skips itself

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_run_triage_stages, prom, grafana, bash_tool, es_tool, llm, issue, on_stage_ready, top_n,
                          namespace_exclude, business_impact_cities or {}, service_hints or {}, redis_instances or {})
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
