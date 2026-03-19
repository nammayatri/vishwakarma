"""
Fast RCA — quick classification for alerts with known root cause patterns.

For alerts like NoDriverDrainerRunning (15+/week, only 4 known root causes),
this module runs targeted checks via a specialized toolset and classifies
the result with a single fast_model LLM call (~5-10s) instead of a full
40-step agentic investigation (~15 min).

The fast RCA is posted to Slack immediately; the deep investigation follows
as a thread reply.
"""
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# ── Registry: alert_name → (toolset_name, tool_name, params) ─────────────────

# ── Companion checks: additional tools to run in parallel for certain alerts ──
# Maps tool_name → list of (toolset_name, tool_name, params) to run alongside
_COMPANION_CHECKS: dict[str, list[tuple[str, str, dict]]] = {
    "investigate_rds_cpu": [("cloud_alerts", "investigate_alb_5xx", {})],
    "investigate_ratio_drop": [("cloud_alerts", "investigate_alb_5xx", {})],
}

_REGISTRY: dict[str, tuple[str, str, dict]] = {
    # Drainer stopped
    "NoDriverDrainerRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "driver"}),
    "NoDriverDrainerPodRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "driver"}),
    "NoAppDrainerRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "app"}),
    "NoAppDrainerPodRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "app"}),
    "NoCustomerDrainerPodRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "app"}),
    # Drainer lag
    "DriverDrainerLagIncreasing": ("cloud_alerts", "investigate_drainer", {"drainer_type": "driver"}),
    "CustomerDrainerLagIncreasing": ("cloud_alerts", "investigate_drainer", {"drainer_type": "app"}),
    # ALB 5xx
    "ALB5xxErrors": ("cloud_alerts", "investigate_alb_5xx", {}),
    "HTTPCode_Target_5XX_Count": ("cloud_alerts", "investigate_alb_5xx", {}),
    "HTTPCode_ELB_5XX_Count": ("cloud_alerts", "investigate_alb_5xx", {}),
    "HTTP_ELB_CODE_5XX": ("cloud_alerts", "investigate_alb_5xx", {}),
    # RDS high CPU
    "RDSHighCPU": ("cloud_alerts", "investigate_rds_cpu", {"db_cluster": "driver"}),
    "RDS_CPU_High": ("cloud_alerts", "investigate_rds_cpu", {"db_cluster": "driver"}),
    "RDSCPUUtilization": ("cloud_alerts", "investigate_rds_cpu", {"db_cluster": "driver"}),
    "DriverDBHighCPU": ("cloud_alerts", "investigate_rds_cpu", {"db_cluster": "driver"}),
    "CustomerDBHighCPU": ("cloud_alerts", "investigate_rds_cpu", {"db_cluster": "customer"}),
    # Config parse failure
    "SystemConfigParseFailure": ("cloud_alerts", "investigate_config_failure", {}),
    # Allocator
    "AllocatorLooksDead": ("cloud_alerts", "investigate_allocator", {}),
    # RDS high connections
    "RDSHighConnections": ("cloud_alerts", "investigate_rds_connections", {"db_cluster": "driver"}),
    "DriverDBHighConnections": ("cloud_alerts", "investigate_rds_connections", {"db_cluster": "driver"}),
    "CustomerDBHighConnections": ("cloud_alerts", "investigate_rds_connections", {"db_cluster": "customer"}),
    # Producer
    "ProducerNotProducing": ("cloud_alerts", "investigate_producer", {}),
    # RDS replication lag
    "RDSReplicationLag": ("cloud_alerts", "investigate_rds_replication_lag", {"db_cluster": "driver"}),
    "ReplicationSlotLag": ("cloud_alerts", "investigate_rds_replication_lag", {"db_cluster": "driver"}),
    "DriverDBReplicationLag": ("cloud_alerts", "investigate_rds_replication_lag", {"db_cluster": "driver"}),
    "CustomerDBReplicationLag": ("cloud_alerts", "investigate_rds_replication_lag", {"db_cluster": "customer"}),
    "AuroraReplicaLag": ("cloud_alerts", "investigate_rds_replication_lag", {"db_cluster": "driver"}),
    # Business metrics — ride to search ratio
    "RideToSearchRatioDown": ("cloud_alerts", "investigate_ratio_drop", {}),
    # Login success rate
    "LoginSuccessRate": ("cloud_alerts", "investigate_login_success", {}),
    # Redis
    "RedisHighCPU": ("cloud_alerts", "investigate_redis", {"cluster": "all"}),
    "RedisHighMemory": ("cloud_alerts", "investigate_redis", {"cluster": "all"}),
    "RedisEvictions": ("cloud_alerts", "investigate_redis", {"cluster": "all"}),
    "RedisHighConnections": ("cloud_alerts", "investigate_redis", {"cluster": "all"}),
}


def match_fast_rca(alert_name: str) -> tuple[str, str, dict] | None:
    """Check if an alert has a fast-RCA handler. Returns (toolset, tool, params) or None."""
    # Exact match first
    if alert_name in _REGISTRY:
        return _REGISTRY[alert_name]
    # Substring match for ALB 5xx variants (alarm names vary)
    alert_lower = alert_name.lower()
    if "5xx" in alert_lower and ("alb" in alert_lower or "elb" in alert_lower):
        return ("cloud_alerts", "investigate_alb_5xx", {})
    # Substring match for RDS CPU variants — CloudWatch alarm names vary widely — CloudWatch alarm names vary widely
    # e.g. "app-db-r1 high cpu", "Atlas Driver DB Writer High CPU Utilisation"
    if ("cpu" in alert_lower and ("rds" in alert_lower or "db" in alert_lower
            or "driver" in alert_lower or "customer" in alert_lower or "atlas" in alert_lower)):
        cluster = "customer" if "customer" in alert_lower else "driver"
        return ("cloud_alerts", "investigate_rds_cpu", {"db_cluster": cluster})
    # Substring match for RDS connection variants
    # e.g. "Database connections on Driver DB More than 3000", "Atlas Customer DB High Connection count"
    if "connection" in alert_lower and ("db" in alert_lower or "rds" in alert_lower
            or "driver" in alert_lower or "customer" in alert_lower or "database" in alert_lower):
        cluster = "customer" if "customer" in alert_lower else "driver"
        return ("cloud_alerts", "investigate_rds_connections", {"db_cluster": cluster})
    # Substring match for RDS replication lag variants
    if ("replication" in alert_lower or "replica" in alert_lower or "slot" in alert_lower) and \
       ("lag" in alert_lower or "rds" in alert_lower or "aurora" in alert_lower or "db" in alert_lower):
        cluster = "customer" if "customer" in alert_lower else "driver"
        return ("cloud_alerts", "investigate_rds_replication_lag", {"db_cluster": cluster})
    # Substring match for Redis variants
    if "redis" in alert_lower and any(kw in alert_lower for kw in ["cpu", "memory", "evict", "connect", "bandwidth"]):
        return ("cloud_alerts", "investigate_redis", {"cluster": "all"})
    # Substring match for Producer
    if "producer" in alert_lower and ("not" in alert_lower or "producing" in alert_lower or "silent" in alert_lower):
        return ("cloud_alerts", "investigate_producer", {})
    # Substring match for Allocator
    if "allocator" in alert_lower and ("dead" in alert_lower or "not" in alert_lower or "down" in alert_lower):
        return ("cloud_alerts", "investigate_allocator", {})
    return None


def get_companion_checks(tool_name: str) -> list[tuple[str, str, dict]]:
    """Return additional tools to run in parallel for this alert type."""
    return _COMPANION_CHECKS.get(tool_name, [])


# ── Decision tree prompts per alert category ──────────────────────────────────

_DRAINER_DECISION_TREE = """\
- **Scenario A (Pods DOWN)**: pod_status shows 0/0 or CrashLoopBackOff + pod_events shows OOMKilled/Evicted → Root cause: OOM or eviction
- **Scenario B (Pods UP + SQL errors)**: pod_logs contains "integer out of range" or "value too long" or "BATCH_INSERT" with sqlState → Root cause: SQL data type overflow
- **Scenario C (Pods UP + connection errors)**: pod_logs contains "connection refused" or "too many connections" + rds_cpu is high → Root cause: Database overload
- **Scenario D (Pods UP + Redis errors)**: pod_logs contains "CLUSTERDOWN" or "NOGROUP" or redis_health shows bandwidth exceeded → Root cause: Redis cluster issue
- **Scenario E (Pods UP + no errors + drain_rate > 0)**: Drainer is actually processing queries, metric may be stale → Root cause: False alarm / stale metric
- **Scenario F (Pods UP + stop_metric active)**: stop_metric value is > 0, drainer intentionally stopped → Root cause: Drainer stopped"""

_ALB_5XX_DECISION_TREE = """\
BASELINES — normal values (not an incident):
- ALB target_5xx: 20-40/min is NORMAL baseline. Only >100/min sustained is a real spike.
- ALB elb_5xx: 0-2/min is normal. >10/min is concerning.
- Response time avg: 10-30ms is normal. >500ms is slow. >3s is timeout territory.
- Bad pods: protocol-rider-person-stat, protocol-safety-dashboard, gps-processor, sdk-config-server in CrashLoopBackOff are KNOWN and should be IGNORED.

- **Scenario A (Low 5xx + baseline noise)**: target_5xx < 50/min consistently, no spike pattern → Root cause: Normal baseline noise, not a real incident. NO ACTION NEEDED.
- **Scenario B (High target_5xx + high latency)**: target_5xx >100/min + response_time avg > 500ms → Root cause: Downstream timeout (504), a backend service or DB is slow
- **Scenario C (High target_5xx + low latency + specific handler)**: target_5xx >100/min + response_time < 100ms + api_5xx shows one handler dominating → Root cause: Application bug in that specific API handler (500)
- **Scenario D (High elb_5xx + unhealthy hosts)**: elb_5xx > 10/min + bad_pods shows CrashLoopBackOff for a MAIN service (not the known bad pods listed above) → Root cause: ALB cannot route to backends
- **Scenario E (Broad 5xx across services)**: api_5xx and istio_5xx show multiple services failing >100/min total → Root cause: Shared dependency failure (DB, Redis, or network)"""

_RDS_CPU_DECISION_TREE = """\
IMPORTANT BASELINES — use these to determine if values are actually anomalous:
- driver-w3 normal CPU: ~17-20%. customer-w1 normal CPU: ~10-12%. driver-r1 normal CPU: ~28-33%.
- Normal ALB 5xx: 20-40/min (baseline noise). A "spike" means >100/min sustained.
- Normal PI wait events: IO:DataFileRead, IO:XactSync, CPU are always present. Only significant if one event is >50% of total db.load.
- If CPU is within normal range AND 5xx is within normal range → this is NOT an incident. Classify as Scenario H.

- **Scenario A (Missing index / full table scan)**: rds_cpu >50% (well above baseline) + rds_ReadIOPS spike >5x normal + pi_top_sql shows one query >40% db.load → Root cause: Expensive query doing sequential scan, needs index
- **Scenario B (Bad deploy)**: rds_cpu >50% + pi_top_sql shows new query pattern not seen before → Root cause: Recent deploy introduced expensive query or connection leak
- **Scenario C (Autovacuum)**: rds_cpu elevated + rds_WriteIOPS high + pi_wait_events shows VacuumDelay or XactSync as >50% of total load → Root cause: Autovacuum running on large table, will self-resolve
- **Scenario D (Connection pool exhaustion)**: rds_DatabaseConnections surged >2x normal + app_db_errors shows "too many clients" or "connection pool" → Root cause: PgBouncer or app connection pool exhausted
- **Scenario E (Replication lag)**: rds_cpu high on reader + WriteIOPS high on writer → Root cause: Heavy write load causing replica lag, reads falling behind
- **Scenario F (DB causing 5xx)**: rds_cpu >60% (genuinely high, not baseline) + investigate_alb_5xx_target_5xx >100/min (sustained, not baseline noise of 20-40/min) → Root cause: Database overload is causing downstream 5xx errors — HIGH IMPACT
- **Scenario G (Background job / low urgency)**: rds_cpu elevated but <60% + no app_db_errors + investigate_alb_5xx_target_5xx in normal range (<50/min) → Root cause: Background analytics or batch job, no user impact
- **Scenario H (Normal load / false alarm)**: rds_cpu within normal baseline range (driver-w3 <25%, customer-w1 <15%) + 5xx within baseline (<50/min) + no anomaly in any metric → Root cause: Normal load, alert threshold too sensitive or transient spike already resolved. NOT AN INCIDENT."""

_RDS_CONNECTIONS_DECISION_TREE = """\
- **Scenario A (HPA scale-up)**: connections surged + hpa_and_scale shows recent SuccessfulRescale or replica increase → Root cause: Pod autoscaling increased pod count, each new pod opens DB connections. Expected during traffic spikes, will stabilize.
- **Scenario B (Connection leak / pool exhaustion)**: connections surged + app_conn_errors shows "too many connections" or "FATAL: sorry, too many clients" + no recent scale events → Root cause: Application connection pool leak or misconfiguration. Connections keep growing without being released.
- **Scenario C (Long-running queries holding connections)**: connections high + pi_wait_events shows Lock or Client:ClientRead dominant → Root cause: Slow/blocked queries holding connections open. Check Performance Insights for the blocking query.
- **Scenario D (Correlated with CPU spike)**: connections high + rds_cpu high → Root cause: High CPU is causing queries to take longer, which keeps connections open longer. Fix the CPU issue first (see RDS CPU scenarios).
- **Scenario E (Baseline / normal)**: connections are within normal range for pod count + no errors in app logs → Root cause: False alarm, connections are proportional to running pods. Alert threshold may need adjustment."""

_RDS_REPLICATION_LAG_DECISION_TREE = """\
- **Scenario A (Stale logical replication slot)**: slot_lag max > 30 days + slot_disk_usage is low/zero + wal_disk_usage stable → Root cause: Inactive logical replication slot (AWS→GCP subscriber disconnected). Slot is not consuming WAL but preventing cleanup. REQUIRES: DBA to drop the stale slot or reconnect the subscriber.
- **Scenario B (Active slot but falling behind)**: slot_lag is growing (hours to days) + slot_disk_usage is growing + writer_write_iops is high → Root cause: Logical replication subscriber is connected but can't keep up with write volume. Check GCP subscriber health and network.
- **Scenario C (Heavy write load causing lag)**: writer_write_iops > 2x normal + writer_cpu > 70% + slot_lag increasing + replica_lag also increasing → Root cause: Writer under heavy write load, all downstream replication (both logical slots and Aurora replicas) falling behind.
- **Scenario D (Aurora replica lag spike)**: replica_lag_* > 100ms + slot_lag is normal/stable → Root cause: Aurora read replica lag only (not logical replication). Usually caused by long-running query on reader or heavy writer load. Will typically self-resolve.
- **Scenario E (WAL disk growth risk)**: wal_disk_usage growing steadily + slot_lag very high → Root cause: Stale replication slot preventing WAL cleanup, causing storage growth. URGENT: risk of storage full if not addressed.
- **Scenario F (Multiple slots with mixed health)**: slot_lag min/max differ wildly (e.g. min=2 days, max=200 days in same window) → Root cause: Multiple replication slots exist, some active (low lag) and some stale (high lag). Need to query pg_replication_slots to identify which slot is stale.
- **Scenario G (Normal / false alarm)**: slot_lag < 1 hour + replica_lag < 50ms + all metrics stable → Root cause: Replication is healthy, alert threshold too sensitive or transient spike already resolved."""

_REDIS_DECISION_TREE = """\
- **Scenario A (Scaling in progress)**: cluster_status shows "modifying" + scaling_events shows slot migration/rebalancing → Root cause: Redis cluster is being scaled (adding/removing shards). MOVED errors and brief timeouts are EXPECTED during scaling. Will self-resolve once scaling completes.
- **Scenario B (High CPU + expensive command)**: EngineCPU high on a cluster + app_redis_errors shows KEYS/SMEMBERS/SORT patterns → Root cause: Expensive O(N) Redis command from an application service
- **Scenario C (High memory + evictions)**: DatabaseMemoryUsagePercentage > 80% + Evictions > 0 → Root cause: Cache full, keys not expiring or growing unbounded. Evictions degrade cache hit ratio → cascading DB load
- **Scenario D (Connection storm)**: CurrConnections surged + app_redis_errors shows "connection refused" or "too many connections" → Root cause: HPA scale-up created too many connections, or connection pool leak
- **Scenario E (Bandwidth saturation)**: NetworkBandwidthOutAllowanceExceeded > 0 on any node → Root cause: Network bandwidth limit hit on ElastiCache node, causes packet drops and timeouts
- **Scenario F (CLUSTERDOWN / shard failure)**: cluster_status is NOT "modifying" but app_redis_errors shows CLUSTERDOWN or MOVED errors → Root cause: Unexpected cluster topology issue — node crash or unplanned failover
- **Scenario G (Baseline / normal)**: All metrics within normal range, cluster_status is "available", no errors in app logs → Root cause: Transient spike or stale alert, self-resolved"""

_PRODUCER_DECISION_TREE = """\
- **Scenario A (Stale metric / false alarm)**: pod_status shows all pods Running with 0 restarts + producer_metric > 0 + stream_jobs > 0 → Root cause: Producer IS working, metric is stale or alert threshold too sensitive. FALSE ALARM.
- **Scenario B (Pod crash)**: pod_status shows CrashLoopBackOff or high restart count + pod_events shows BackOff/OOMKilled → Root cause: Producer pod is crash-looping (check logs for error)
- **Scenario C (Pod running but not producing)**: pod_status shows Running + producer_metric = 0 or very low + pod_logs shows errors → Root cause: Producer is alive but stuck — check error in logs (DB/Redis/Kafka connectivity)
- **Scenario D (Recent deployment)**: pod_status shows Running + pod_events shows recent image pull + producer_metric dropped → Root cause: New deployment may have broken producer — check if error started after deploy
- **Scenario E (Downstream issue)**: pod_status Running + producer_metric > 0 + stream_jobs = 0 + stream_jobs_failed > 0 → Root cause: Producer is creating jobs but allocator is failing to consume them"""

_ALLOCATOR_DECISION_TREE = """\
- **Scenario A (Stale metric / false alarm)**: pod_status shows all pods Running + stream_jobs > 0 (delta 2m) + ride_created > 0 → Root cause: Allocator IS working, metric is stale. FALSE ALARM.
- **Scenario B (Pod crash)**: pod_status shows CrashLoopBackOff or high restart count + pod_events shows OOM/BackOff → Root cause: Allocator pod crash-looping
- **Scenario C (Running but not consuming)**: pod_status Running + stream_jobs = 0 + pod_logs shows errors (Redis/DB/Kafka) → Root cause: Allocator stuck due to dependency failure
- **Scenario D (Producer down, not allocator)**: pod_status Running + stream_jobs = 0 + stream_jobs_failed = 0 + ride_created dropping → Root cause: No jobs to consume — check if producer is actually creating jobs (this is a producer issue, not allocator)
- **Scenario E (Partial failure)**: pod_status Running + stream_jobs > 0 but lower than normal + stream_jobs_failed > 0 → Root cause: Allocator partially working but some jobs failing — check error pattern in logs"""

_LOGIN_SUCCESS_DECISION_TREE = """\
- **Scenario A (OTP request failing)**: auth_by_status shows 500 rate > 0 → Root cause: /v2/auth/ endpoint returning 500, users can't even request OTP. Check BAP logs for the error.
- **Scenario B (OTP delivery failure)**: auth_by_status 200 rate is normal BUT verify_by_status total dropped to near zero → Root cause: OTP sent successfully but never delivered to user (SMS/WhatsApp provider down). Users can't verify because they never received the OTP. Check OTP provider status.
- **Scenario C (Verify endpoint broken)**: verify_by_status shows 500 rate > 0 → Root cause: /v2/auth/:authId/verify/ endpoint returning 500, OTP received but verification fails. Check BAP logs and DB health.
- **Scenario D (Rate limit attack)**: rate_limit_429 >> 16/s baseline (e.g. >50/s) + auth_errors shows rate limit entries → Root cause: Bot traffic flooding auth endpoint, rate limiter blocking legitimate users too.
- **Scenario E (Database/Redis issue)**: rds_cpu high or auth_errors shows "connection" or "timeout" → Root cause: Customer DB or Redis issue preventing auth token read/write.
- **Scenario F (BAP pods crashing)**: bap_pods shows CrashLoopBackOff or not all Running → Root cause: BAP app-backend pods are down, all endpoints including auth are affected.
- **Scenario G (Bad deploy)**: bap_pods shows recent restart + auth_errors shows new error pattern → Root cause: Recent deployment broke the auth flow.
- **Scenario H (Low traffic noise)**: auth_volume_ratio < 0.1 (traffic 90% below yesterday) + success_rate fluctuating → Root cause: Too few requests at this hour, ratio is unreliable. False alarm.
- **Scenario I (Normal baseline)**: success_rate > 90% OR success_rate close to success_rate_yesterday → Root cause: Login rate is normal, alert may have fired on a brief transient dip that already recovered."""

_RATIO_DROP_DECISION_TREE = """\
- **Scenario A (Metrics not flowing)**: search_volume = 0 or ride_volume = 0 → Root cause: Metrics pipeline broken — search_request_count or ride_created_count stopped incrementing. NOT a real business issue. Check if the exporting pods are healthy.
- **Scenario B (Search 5xx spike)**: search_5xx shows high rate on /protocol/:merchantId/search/ + ratio dropped → Root cause: Search API returning 500s, users can't search → no rides created. Fix the search service.
- **Scenario C (Allocator dead)**: allocator_jobs = 0 or near 0 + search volume normal + ride volume dropped → Root cause: Allocator is not assigning drivers to searches. Searches succeed but rides never get created. Check allocator pods.
- **Scenario D (External API failure)**: external_api_errors shows OpenNetwork/Acme gateway returning errors + search_5xx elevated → Root cause: External dependency (OpenNetwork gateway, Acme gateway) is failing, breaking the search→ride flow.
- **Scenario E (Broad 5xx across services)**: top_5xx shows multiple handlers failing + istio_5xx shows multiple services → Root cause: Shared infrastructure issue (DB, Redis, network) cascading into ride creation failure.
- **Scenario F (Searches up, rides flat — demand spike without driver supply)**: search_now_vs_yesterday > 1.5 + ride_now_vs_yesterday ~1.0 + no 5xx → Root cause: Traffic surge with insufficient driver supply. Not a system issue — operational/supply problem.
- **Scenario G (Normal baseline / time of day)**: ratio_by_city similar to ratio_yesterday + search_now_vs_yesterday ~1.0 → Root cause: Ratio is normal for this time of day/week. False alarm or threshold too aggressive.
- **Scenario H (City-specific issue)**: only 1-2 cities in ratio_by_city below threshold, rest normal → Root cause: Localized issue in specific city — check city-specific external factors, regional service issues."""

_DECISION_TREES: dict[str, str] = {}
# Map each alert to its decision tree
for _name in ["NoDriverDrainerRunning", "NoDriverDrainerPodRunning", "NoAppDrainerRunning",
              "NoAppDrainerPodRunning", "NoCustomerDrainerPodRunning",
              "DriverDrainerLagIncreasing", "CustomerDrainerLagIncreasing"]:
    _DECISION_TREES[_name] = _DRAINER_DECISION_TREE
for _name in ["ALB5xxErrors", "HTTPCode_Target_5XX_Count", "HTTPCode_ELB_5XX_Count",
              "HTTP_ELB_CODE_5XX", "ALB_5xx"]:
    _DECISION_TREES[_name] = _ALB_5XX_DECISION_TREE
for _name in ["RDSHighCPU", "RDS_CPU_High", "RDSCPUUtilization",
              "DriverDBHighCPU", "CustomerDBHighCPU"]:
    _DECISION_TREES[_name] = _RDS_CPU_DECISION_TREE
for _name in ["RedisHighCPU", "RedisHighMemory", "RedisEvictions",
              "RedisHighConnections", "Redis"]:
    _DECISION_TREES[_name] = _REDIS_DECISION_TREE
for _name in ["RDSHighConnections", "DriverDBHighConnections", "CustomerDBHighConnections"]:
    _DECISION_TREES[_name] = _RDS_CONNECTIONS_DECISION_TREE
for _name in ["RDSReplicationLag", "ReplicationSlotLag", "DriverDBReplicationLag",
              "CustomerDBReplicationLag", "AuroraReplicaLag"]:
    _DECISION_TREES[_name] = _RDS_REPLICATION_LAG_DECISION_TREE
_DECISION_TREES["ProducerNotProducing"] = _PRODUCER_DECISION_TREE
_CONFIG_FAILURE_DECISION_TREE = """\
- **Scenario A (Redis cache decode failure — baseline)**: config_failures_by_service shows failures + logs show "Decode Failure for key:CacheHash" with "encountered Null" → Root cause: Config key missing from Redis cache for a specific MerchantOperatingCityId. This is usually BASELINE noise — the config was never set for that city. Check if it's a new city or existing.
- **Scenario B (Post-deployment config break)**: config_failures increased + recent_deploys shows deploy within 30min → Root cause: New deployment introduced a config schema change that existing cached values don't match
- **Scenario C (Config service/DB down)**: config_failures spiking rapidly + logs show "connection refused" or "timeout" to config service or DB → Root cause: Config source (DB or config service) is unreachable
- **Scenario D (Bad config push)**: config_failures spiking + config_changes shows recent configmap/secret update → Root cause: Someone pushed a bad config value
- **Scenario E (Stable baseline)**: config_failures_1h count is low and stable + same errors yesterday → Root cause: Baseline noise, not a new issue"""

_DECISION_TREES["AllocatorLooksDead"] = _ALLOCATOR_DECISION_TREE
_DECISION_TREES["SystemConfigParseFailure"] = _CONFIG_FAILURE_DECISION_TREE
_DECISION_TREES["RideToSearchRatioDown"] = _RATIO_DROP_DECISION_TREE
_DECISION_TREES["LoginSuccessRate"] = _LOGIN_SUCCESS_DECISION_TREE
for _name in ["RDSReplicationLag", "ReplicationSlotLag", "DriverDBReplicationLag",
              "CustomerDBReplicationLag", "AuroraReplicaLag"]:
    _DECISION_TREES[_name] = _RDS_REPLICATION_LAG_DECISION_TREE


def synthesize_fast_rca(llm, checks: dict, alert_name: str) -> dict:
    """
    Single fast_model LLM call to classify the root cause from check results.

    Returns dict with: root_cause, confidence, scenario, impact, suggested_fix, evidence_summary
    """
    # Pick the right decision tree; default to ALB if alert contains "5xx"
    decision_tree = _DECISION_TREES.get(alert_name)
    if not decision_tree and "5xx" in alert_name.lower():
        decision_tree = _ALB_5XX_DECISION_TREE
    if not decision_tree and ("replication" in alert_name.lower() or "replica" in alert_name.lower() or "slot" in alert_name.lower()):
        decision_tree = _RDS_REPLICATION_LAG_DECISION_TREE
    if not decision_tree and ("rds" in alert_name.lower() or ("cpu" in alert_name.lower() and "redis" not in alert_name.lower())):
        decision_tree = _RDS_CPU_DECISION_TREE
    if not decision_tree and ("ratio" in alert_name.lower() or "search" in alert_name.lower()):
        decision_tree = _RATIO_DROP_DECISION_TREE
    if not decision_tree and "redis" in alert_name.lower():
        decision_tree = _REDIS_DECISION_TREE
    if not decision_tree:
        decision_tree = _DRAINER_DECISION_TREE

    prompt = f"""RESPOND WITH ONLY A JSON OBJECT. NO REASONING. NO EXPLANATION. NO THINKING. JUST THE JSON.

Alert: "{alert_name}". Classify root cause.

Baselines: RDS CPU driver-w3 normal=17-20%. ALB 5xx normal=20-40/min. If all within baseline → scenario H.

Check Results:
{json.dumps(checks, indent=2)[:3000]}

Decision Tree:
{decision_tree}

OUTPUT ONLY THIS JSON (replace values, nothing else before or after):
{{"root_cause": "one-line", "confidence": "high|medium|low", "scenario": "letter", "impact": "user impact or No user impact", "suggested_fix": "action or No action needed", "evidence_summary": "key facts with numbers"}}"""

    try:
        # Use main model chain for classification — fast models can't return clean JSON
        raw_response = llm._call_with_fallback(
            models=llm._get_main_chain(),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            timeout=60,
            total_budget=90,
        )
        msg = raw_response.choices[0].message
        raw = (msg.content or "").strip()
        # Reasoning models put output in reasoning_content when content is empty
        if not raw:
            raw = getattr(msg, "reasoning_content", "") or ""
            raw = raw.strip()
        # Log what we got for debugging
        log.info(f"Fast RCA synthesis response: content_len={len(msg.content or '')}, "
                 f"reasoning_len={len(getattr(msg, 'reasoning_content', '') or '')}, "
                 f"raw_len={len(raw)}, raw_preview={raw[:100]}")
        # Strip reasoning preamble and extract JSON
        import re
        # Try multiple extraction strategies
        # Strategy 1: Find JSON block containing "root_cause" (handles nested reasoning text)
        json_match = re.search(r'\{"root_cause".*?"evidence_summary"\s*:\s*"[^"]*"\s*\}', raw, re.DOTALL)
        if not json_match:
            # Strategy 2: Find last JSON object in the response (reasoning models put JSON at the end)
            all_jsons = list(re.finditer(r'\{[^{}]{20,}\}', raw))
            if all_jsons:
                json_match = all_jsons[-1]
        if not json_match:
            # Strategy 3: Find anything that looks like our expected JSON
            json_match = re.search(r'\{[^{}]*"root_cause"[^}]*\}', raw, re.DOTALL)
        if json_match:
            raw = json_match.group()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Fast RCA synthesis failed ({type(e).__name__}): {e}")
        # Graceful degradation: summarize raw checks without LLM
        check_summary = []
        for k, v in (checks or {}).items():
            if isinstance(v, str) and not v.startswith("(error"):
                lines = v.strip().split("\n")
                last = lines[-1] if lines else ""
                if last:
                    check_summary.append(f"{k}: {last[:80]}")
        evidence = "; ".join(check_summary[:5]) if check_summary else str(e)
        return {
            "root_cause": "LLM classification unavailable — raw check results below (deep investigation will follow)",
            "confidence": "low",
            "scenario": "unknown",
            "impact": "Unknown — LLM unavailable, raw checks collected",
            "suggested_fix": "Deep investigation in progress",
            "evidence_summary": evidence,
        }


def format_slack_message(result: dict, title: str) -> str:
    """Format fast RCA result as Slack mrkdwn."""
    confidence = result.get("confidence", "low")
    scenario = result.get("scenario", "?")

    if confidence == "high":
        icon = ":large_green_circle:"
    elif confidence == "medium":
        icon = ":large_yellow_circle:"
    else:
        icon = ":red_circle:"

    lines = [
        f":zap: *Fast RCA: {title}*",
        "",
        f"{icon} *Confidence:* {confidence.upper()} (Scenario {scenario})",
        f":mag: *Root Cause:* {result.get('root_cause', 'Unknown')}",
        f":warning: *Impact:* {result.get('impact', 'Unknown')}",
        f":wrench: *Suggested Fix:* {result.get('suggested_fix', 'N/A')}",
        "",
        f"_Evidence: {result.get('evidence_summary', 'N/A')}_",
        "",
        ":hourglass_flowing_sand: _Deep investigation in progress — full RCA with PDF will follow in this thread..._",
    ]
    return "\n".join(lines)
