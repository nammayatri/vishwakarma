# Memorystore (Redis) Investigation Runbook (GCP)

## Goal
- **Primary Objective:** Investigate Memorystore for Redis alerts — high CPU, high memory, evictions, high connections, or replication lag.
- **Scope:** GCP Memorystore for Redis instances in `asia-south1`. Used for session caching, location data, state management, and KV store.
- **Agent Mandate:** Read-only. Do not flush, delete keys, or modify any Redis configuration. Provide RCA with possible root causes for the team to act on.
- **Expected Outcome:** Identify what is stressing Redis, which service/key pattern is responsible, and what the team should do.

## Time Window Instructions
- The alert's `startsAt` field contains the **exact time the alarm fired** — use this as your investigation window start.
- For all Prometheus and Elasticsearch queries use: `start-time = startsAt - 10 minutes`, `end-time = startsAt + 1 hour`
- If `startsAt` is not available, fall back to `now - 30 minutes`.
- Always state the time window used in your findings (e.g. "investigated 17:00–18:00 UTC").

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- Memorystore instance names (and their tier: BASIC vs STANDARD_HA primary/replica) and their roles
- Memorystore host/IP and port (use the value from the Site Knowledge Base)
- Elasticsearch endpoint + app log index name

## IMPORTANT: Tool Routing
- **Live Redis state (PRIMARY)**: Use bash with `redis-cli` against the instance host (READ-ONLY commands only) — `INFO`, `SLOWLOG GET`, `CLIENT LIST`, `DBSIZE`, `MEMORY STATS`. This is the most reliable source for current memory, evictions, slowlog, and connections.
- **Metric trends over the alert window**: Use the `prometheus_query_range` tool. GCP Memorystore metrics are mirrored into Prometheus for the GCP executor. Use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`). Do NOT use `gcloud monitoring` — `gcloud monitoring time-series list` does not exist (valid `gcloud monitoring` subcommands are only dashboards/policies/snoozes).
- **Topology / config / operations**: Use `gcloud redis instances list`, `gcloud redis instances describe`, and `gcloud redis operations list`.
- **Application logs**: Use `elasticsearch_search` tool

## Workflow

### Step 0: Alert Freshness Check
Check current state and the live `INFO` snapshot to determine if this is genuine, resolved, or stale.

First confirm the instance is up and get current memory pressure directly from Redis (HIGH confidence — read-only):
```
redis-cli -h <memorystore-host> INFO memory | grep -iE "used_memory_human|maxmemory_human|maxmemory_policy|mem_fragmentation_ratio"
```
And check the GCP-side state:
```
gcloud redis instances describe <instance> --region=asia-south1 --format="value(state)"
```
- **Current memory/CPU > threshold** → GENUINE, investigate urgently
- **Current value normal, startsAt < 30 min ago** → RESOLVED, transient — lighter investigation
- **Current value normal, startsAt > 2 hours ago** → STALE — note and move on

For the trend-side current value (last few minutes of memory usage), use the `prometheus_query_range` tool. Use the exact GCP Memorystore memory-usage metric name from the Site Knowledge Base (`knowledge-gcp.md`):
- query: `<gcp-memorystore-memory-usage-ratio-metric>{instance="<instance>"}` (exact metric name + label from knowledge-gcp.md)
- start: `<5min ago>`, end: `<now>`, step: `1m`

### Step 1: Identify the Alerting Instance
**If the alert specifies an instance name or instance ID**, skip the enumerate step and go directly to Step 2 using that instance.

**If the alert does NOT specify an instance**, list all Memorystore instances to find the affected one:
```
gcloud redis instances list --region=asia-south1 --format="table(name,tier,sizeGb,host,state,redisVersion)"
```

For each candidate instance, inspect its topology and config (STANDARD_HA = primary + replica; the primary/replica endpoints and read replica info are in the describe output — there is no separate "replication group" object in GCP):
```
gcloud redis instances describe <instance> --region=asia-south1 \
  --format="value(name,tier,host,port,readEndpoint,replicaCount,currentLocationId,state)"
```

Note the instance names — you will check CPU on all of them in Step 2.

### Step 2: Check CPU on ALL Instances (Find Which One is High)
For **each instance** (or just the alerting one if known from Step 1), use the `prometheus_query_range` tool. Use the exact GCP Memorystore CPU metric name from the Site Knowledge Base (`knowledge-gcp.md`):
- query: `<gcp-memorystore-cpu-utilization-metric>{instance="<instance>"}` (exact metric name + label from knowledge-gcp.md)
- start: `<startsAt-10min>`, end: `<startsAt+1h>`, step: `5m`

Identify which instances have high CPU (> 50%) or other anomalies. Focus the rest of the investigation on those.

**7-day baseline adversarial check (run in parallel with CPU checks):**
To distinguish a real incident from a recurring daily pattern (e.g. batch jobs, daily spikes), run the same CPU query over a wider window via `prometheus_query_range`:
- query: `<gcp-memorystore-cpu-utilization-metric>{instance="<instance>"}` (exact metric name from knowledge-gcp.md)
- start: `<7 days ago>`, end: `<now>`, step: `1h`

If today's CPU spike matches the 7-day pattern → recurring baseline, not a new incident. State this in your RCA.

### Step 3: Check All Key Metrics + Eviction Policy on the Affected Instance(s)
For each affected instance found in Step 2, the live `redis-cli` snapshot below is the PRIMARY source. For the trend over the alert window, use the `prometheus_query_range` tool, one query per metric. Use the exact GCP Memorystore metric names from the Site Knowledge Base (`knowledge-gcp.md`) — do not assume the metric strings; resolve them from knowledge-gcp.md. Window: start `<startsAt-10min>`, end `<startsAt+1h>`, step `5m`. Metrics to trend:
- Memory usage ratio (% of maxmemory) → `<gcp-memorystore-memory-usage-ratio-metric>{instance="<instance>"}`
- Used memory bytes → `<gcp-memorystore-memory-usage-bytes-metric>{instance="<instance>"}`
- Evicted keys (maps to AWS Evictions) → `<gcp-memorystore-evicted-keys-metric>{instance="<instance>"}`
- Connected clients → `<gcp-memorystore-connections-metric>{instance="<instance>"}`
- Cache hit ratio → `<gcp-memorystore-cache-hit-ratio-metric>{instance="<instance>"}`
- Total keys in keyspace → `<gcp-memorystore-keyspace-keys-metric>{instance="<instance>"}`
- Network traffic (bytes in/out) → `<gcp-memorystore-network-traffic-metric>{instance="<instance>"}`

(Resolve each `<...-metric>` placeholder to the real GCP metric name + label set from `knowledge-gcp.md` before querying.)

Prefer the live `redis-cli` snapshot (HIGH confidence, read-only) over the Prometheus trend for the current picture:
```
redis-cli -h <memorystore-host> INFO stats     # keyspace_hits, keyspace_misses, evicted_keys, expired_keys, total_connections_received
redis-cli -h <memorystore-host> INFO clients    # connected_clients, blocked_clients
redis-cli -h <memorystore-host> INFO memory     # used_memory, maxmemory, maxmemory_policy, mem_fragmentation_ratio
redis-cli -h <memorystore-host> DBSIZE          # total key count
redis-cli -h <memorystore-host> MEMORY STATS    # detailed memory breakdown
```

Interpret:
- CPU utilization — actual Redis engine CPU
- Memory usage ratio — memory % of maxmemory (= `used_memory / maxmemory` from `INFO memory`)
- Evicted keys — keys evicted due to memory pressure (> 0 = memory full; cross-check `evicted_keys` from `INFO stats`)
- Connected clients — current connections (cross-check `connected_clients` from `INFO clients`)
- Cache hit ratio — low ratio = evictions hurting cache (cross-check `keyspace_hits` / `keyspace_misses` from `INFO stats`)

**Also check eviction policy** (Memorystore exposes overridable Redis configs via the instance describe — there is no parameter group):
```
gcloud redis instances describe <instance> --region=asia-south1 --format="value(redisConfigs)"
```
Look for `maxmemory-policy` and `maxmemory-gb`. You can also read the live policy directly:
```
redis-cli -h <memorystore-host> INFO memory | grep -i maxmemory_policy
```
If evictions > 0: Redis is full. Cache misses will increase, forcing more DB reads → cascading database CPU spike.

### Step 4: Check Business Impact (Run in Parallel)
**Use `prometheus_query_range` tool for all PromQL. Do NOT use http_get.**

Run simultaneously:

**4a — 5xx error rate:**
- query: `sum by(service,handler)(rate(http_request_duration_seconds_count{handler!="/v1/",status_code=~"^5.."}[1m]))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**4b — P99 latency:**
- query: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**4c — Key business metrics (check Site Knowledge Base for deployment-specific metrics):**
- query: use business-critical metrics from knowledge base (e.g., conversion rates, transaction counts)
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `5m`

### Step 5: Correlate with Application Pods
Find all pods that use Redis across all namespaces (GKE):
`kubectl get pods -A | grep -iE "<your-app-services>"` (use service names from the knowledge base)

Grep their logs for Redis errors during the spike (use service names from the knowledge base):
`timeout 30 stern -n <namespace> <service-name> --since 1h | grep -iE "redis|cache|timeout|refused|clusterdown|moved|evict|conn" | head -200`

### Step 6: Search Elasticsearch for Redis Errors
Search the app log index (see knowledge base for index name) for Redis errors during the alert window:
```json
{
  "index": "<app-log-index-from-knowledge-base>",
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "<startsAt-10min ISO8601>", "lte": "<startsAt+1h ISO8601>"}}},
        {"bool": {
          "should": [
            {"match": {"message": "redis"}},
            {"match": {"message": "timeout"}},
            {"match": {"message": "connection refused"}},
            {"match": {"message": "CLUSTERDOWN"}},
            {"match": {"message": "MOVED"}},
            {"match": {"message": "evict"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```
Look inside the `message` field (it's JSON) for the `log` key which has the actual error text. Correlate error timestamps with the Prometheus metric spikes from Step 2.

### Step 7: Check for Connection Storm
High `connections/total` (or `connected_clients` from `INFO clients`) spike:
- Confirm the live client list and count (read-only):
  `redis-cli -h <memorystore-host> CLIENT LIST | head -50`
  `redis-cli -h <memorystore-host> INFO clients | grep -i connected_clients`
- Check if HPA scaled up a service recently (more pods = more Redis connections):
  `kubectl get events -A --sort-by='.lastTimestamp' | grep -iE "scaled|replica|hpa" | tail -20`
- Check HPA status: `kubectl get hpa -A`

### Step 8: Check for Expensive Commands
If Redis CPU is high, look for slow / expensive commands.

First, pull the slowlog directly (HIGH confidence, read-only):
```
redis-cli -h <memorystore-host> SLOWLOG GET 25
```
This returns the actual slow commands with their execution time and arguments — the strongest signal for "what is burning CPU".

Then correlate against app logs for `KEYS *`, `SMEMBERS` on large sets, `SORT`, `LRANGE` on large lists:
`timeout 30 stern -n <namespace> <service-name> --since 1h | grep -iE "KEYS|SMEMBERS|LRANGE|SORT|SCAN" | head -50`

### Step 9: Check for Maintenance / Failover Operations
A failover, scaling, or maintenance operation can cause a transient spike or connection reset. Check recent Memorystore operations and the current state:
```
gcloud redis operations list --region=asia-south1 --filter='target:<instance>' --format="table(name,metadata.verb,status,startTime,endTime)"
```
Also recheck `state` from the describe in Step 1 — `MAINTENANCE`, `REPAIRING`, or `FAILING_OVER` explains transient errors. For HA topology, check replication health live:
```
redis-cli -h <memorystore-host> INFO replication   # role, connected_slaves, master_link_status, master_repl_offset
```

## Synthesize Findings

- **High CPU + expensive command in SLOWLOG / logs** → `KEYS *` or large set operation. Report the command (from `SLOWLOG GET`) and service calling it.
- **High memory + evicted_keys > 0** → Cache full, keys not expiring. Report memory%, eviction count, and which service is storing large/unbounded keys.
- **Evictions + low cache hit ratio + DB CPU spike** → Cascading failure: Redis full → cache misses → DB overload. Redis is the root cause.
- **High connected_clients + recent HPA scale-up** → Connection storm from new pods. Report the service and pod count increase.
- **Connection refused in app logs** → Redis maxclients limit hit. Report current connections vs limit (`INFO clients` vs `redisConfigs` maxclients).
- **Replication lag / `master_link_status:down` + high write load** → Heavy write load on primary or HA replica resync. Report write-heavy service and any failover op from Step 9.

## Possible Fixes (for team to action)
- For high memory/evictions: increase `sizeGb` (scale the instance) or move BASIC → STANDARD_HA / add read replicas
- Set TTLs on keys that are growing unbounded
- Replace `KEYS *` with cursor-based `SCAN`
- Implement connection pooling in the service
- Review `maxmemory-policy` (in `redisConfigs`) — `allkeys-lru` is safer than `noeviction`

---

## Extended Investigation (if runbook steps did not find root cause)

If you have followed all the steps above and still cannot determine the root cause with HIGH or MEDIUM confidence, do not stop. Use your own judgment to continue investigating using any tools available. Consider:
- Correlate timestamps across all signals — metrics spike, log errors, pod restarts, deployments, Memorystore operations
- Check services that this component depends on (upstream/downstream)
- Look for patterns: is this affecting one pod or all? One namespace or cluster-wide?
- Check recent changes: deployments, config changes, scaling events, Memorystore maintenance/failover in the last 2 hours
- Query Elasticsearch for error patterns around the incident time
- Check Prometheus for any other anomalous metrics correlated with the alert time
- Use kubectl to inspect pod resource usage, node pressure, or scheduling issues on GKE

The goal is to find the root cause — the runbook covers the most likely scenarios but real incidents can be unexpected. Trust your investigation instincts and follow the evidence.
