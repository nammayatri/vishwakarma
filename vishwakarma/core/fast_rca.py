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
- **Scenario A (Low 5xx + baseline noise)**: target_5xx < 20/min consistently, no spike pattern → Root cause: Normal baseline noise, not a real incident
- **Scenario B (High target_5xx + high latency)**: target_5xx spike + response_time avg > 3s → Root cause: Downstream timeout (504), a backend service or DB is slow
- **Scenario C (High target_5xx + low latency + specific handler)**: target_5xx spike + response_time < 1s + api_5xx shows one handler dominating → Root cause: Application bug in that specific API handler (500)
- **Scenario D (High elb_5xx + unhealthy hosts)**: elb_5xx > 0 + unhealthy_hosts > 0 → Root cause: ALB cannot route to backends, pods are failing health checks or down
- **Scenario E (High 5xx + bad_pods)**: target_5xx spike + bad_pods shows CrashLoopBackOff or OOMKilled for a key service → Root cause: Pod crashes causing 5xx, identify which service
- **Scenario F (Broad 5xx across all services)**: api_5xx and istio_5xx show multiple services failing → Root cause: Shared dependency failure (DB, Redis, or network), not a single service issue"""

_RDS_CPU_DECISION_TREE = """\
- **Scenario A (Missing index / full table scan)**: rds_cpu high + ReadIOPS spike + pi_top_sql shows one query > 40% db.load → Root cause: Expensive query doing sequential scan, needs index
- **Scenario B (Bad deploy)**: rds_cpu high + pi_top_sql shows new query pattern not seen before → Root cause: Recent deploy introduced expensive query or connection leak
- **Scenario C (Autovacuum)**: rds_cpu high + WriteIOPS high + pi_wait_events shows VacuumDelay or XactSync dominant → Root cause: Autovacuum running on large table, will self-resolve
- **Scenario D (Connection pool exhaustion)**: rds_DatabaseConnections surged + app_db_errors shows "too many clients" or "connection pool" → Root cause: PgBouncer or app connection pool exhausted
- **Scenario E (Replication lag)**: rds_cpu high on reader + WriteIOPS high on writer → Root cause: Heavy write load causing replica lag, reads falling behind
- **Scenario F (DB causing 5xx)**: rds_cpu high + investigate_alb_5xx_target_5xx shows spike → Root cause: Database overload is causing downstream 5xx errors — HIGH IMPACT, user-facing
- **Scenario G (Background job / low urgency)**: rds_cpu high + no app_db_errors + investigate_alb_5xx_target_5xx is low/zero → Root cause: Background analytics or batch job consuming CPU, no user impact"""

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
_DECISION_TREES["ProducerNotProducing"] = _PRODUCER_DECISION_TREE
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
    if not decision_tree and "redis" in alert_name.lower():
        decision_tree = _REDIS_DECISION_TREE
    if not decision_tree:
        decision_tree = _DRAINER_DECISION_TREE

    prompt = f"""You are an SRE analyzing a "{alert_name}" alert. Classify the root cause from these parallel check results.

## Check Results
{json.dumps(checks, indent=2)}

## Decision Tree
{decision_tree}

Respond ONLY with valid JSON (no markdown fences):
{{"root_cause": "one-line description", "confidence": "high|medium|low", "scenario": "A|B|C|D|E|F", "impact": "what is broken for users", "suggested_fix": "immediate action", "evidence_summary": "2-3 key facts from checks"}}"""

    try:
        raw = llm.summarize(prompt).strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Fast RCA synthesis failed: {e}")
        return {
            "root_cause": "Unable to classify — check results available for deep investigation",
            "confidence": "low",
            "scenario": "unknown",
            "impact": "Unknown — see check results",
            "suggested_fix": "Wait for deep investigation",
            "evidence_summary": str(e),
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
