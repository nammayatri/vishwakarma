# AlloyDB Investigation Runbook (GCP)

## Goal
Investigate GCP AlloyDB for PostgreSQL CPU/connection/memory alerts. Determine:
1. Which instance is affected (PRIMARY + any READ_POOL instances)
2. What is causing it (top SQL, wait events, locks, autovacuum)
3. Whether it is causing user-facing impact (5xx, latency, business metric degradation)
4. Confidence level in the root cause
5. Whether an immediate fix is needed or it will self-resolve

**Agent Mandate:** Read-only. Do not modify any DB settings, kill queries, or change instance configurations.

## Time Window
- Use `startsAt` from the alert as your investigation anchor
- Query window: `startsAt - 10 minutes` to `startsAt + 1 hour`
- If `startsAt` not available, use `now - 30 minutes`

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- AlloyDB cluster identifiers (used to discover all instances dynamically)
- GCP project id (prod project, e.g. `prod-project`) and region (`asia-south1`)
- Alert name → cluster mapping
- Elasticsearch endpoint + app log index name
- Service → DB mapping (which app connects to which DB)
- Business-critical Prometheus metrics

---

## IMPORTANT: Tool Routing
- **PRIMARY diagnostic path for GCP is direct SQL via `db_query`.** AlloyDB IS PostgreSQL — `pg_stat_statements`, `pg_stat_activity`, `pg_stat_user_tables`, `pg_locks` etc. are all available and are the highest-confidence source. Reach for SQL first.
- **AlloyDB instance metrics (CPU%, connections, memory, replication byte-lag)**: Use the `prometheus_query_range` tool. On GCP, infra metrics are mirrored into Prometheus for the GCP executor. (There is NO `gcloud monitoring time-series list` command — `gcloud monitoring` only has dashboards/policies/snoozes.) These are the SECONDARY, instance-level trend source; direct SQL is primary for query-level work. **The exact Prometheus metric names are deployment-specific — get them from the Site Knowledge Base (`knowledge-gcp.md`); do not hardcode.**
- **Point-in-time instance config/state**: Use `gcloud alloydb instances describe` / `gcloud alloydb operations list`.
- **Instance discovery**: Use `gcloud alloydb instances list` to find ALL instances including READ_POOL nodes — NEVER rely on hardcoded instance lists.
- **Top SQL / wait events**: Use `pg_stat_statements` and `pg_stat_activity` via `db_query` (AlloyDB has `pg_stat_statements` enabled). AlloyDB **System Insights / Query Insights** in the Cloud Monitoring console is the GUI equivalent of AWS Performance Insights, but for the agent the SQL path is authoritative.
- **Business impact (5xx, latency)**: Use `prometheus_query_range` — these ARE application metrics in Prometheus.
- **Application/DB logs**: Use `elasticsearch_search`
- **Direct SQL diagnostics**: Use `db_query(bap_pg)` or `db_query(bpp_pg)` for pg_stat_activity

---

## Step 0: Alert Freshness Check — Is This Real?

Before investigating, determine if this is a genuine ongoing issue, a resolved transient spike, or a stale/duplicate alert.

**Check current metric value RIGHT NOW** on the instance from the alert (if identifiable). Prefer SQL — it's instant and reliable:
```
db_query(<connection>, "SELECT count(*) AS active_queries, count(*) FILTER (WHERE now() - query_start > interval '5 seconds') AS slow_queries FROM pg_stat_activity WHERE state = 'active'")
```
For the instance-level CPU% trend right now (secondary) use `prometheus_query_range`:
- query (shape): `avg(alloydb_instance_cpu_utilization{instance="<instance-id>"})` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<now - 5m>`, end: `<now>`, step: `1m`

**Interpret:**
- **Current CPU > threshold** → GENUINE, ONGOING — investigate urgently, full depth
- **Current CPU normal, startsAt < 30 min ago** → RESOLVED, TRANSIENT — still investigate but note self-recovery
- **Current CPU normal, startsAt > 2 hours ago** → STALE ALERT — note "alert is stale, issue resolved X hours ago" and do a lighter investigation
- **Alert fingerprint matches a recent investigation** → DUPLICATE — skip

**Include this assessment in your RCA under "Alert Assessment".**

---

## Step 1: Discover ALL Instances + Identify the Affected One

**CRITICAL: AlloyDB clusters can have multiple READ_POOL instances (read pools) in addition to the PRIMARY. Always discover dynamically.**

Run all of these in parallel:

**1a — Discover all instances in the AlloyDB cluster (PRIMARY + READ_POOL):**
```
gcloud alloydb instances list --cluster=<cluster-id> --region=asia-south1 \
  --format="table(name,instanceType,state,machineConfig.cpuCount)"
```

**1b — Identify PRIMARY vs READ_POOL (writer vs readers):**
The `instanceType` column from 1a already distinguishes `PRIMARY` (writer) from `READ_POOL` (readers). To inspect a specific instance in detail:
```
gcloud alloydb instances describe <instance-id> --cluster=<cluster-id> --region=asia-south1
```

**1c — CPU across ALL discovered instances (include every instance from 1a):**
For each instance from 1a, run `prometheus_query_range`. Or query them all at once with a label regex:
- query (shape): `avg by (instance) (alloydb_instance_cpu_utilization{instance=~"<inst-1>|<inst-2>|..."})` — use the exact GCP metric + label name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

Note: AlloyDB CPU utilization may be reported as a fraction (0.0–1.0); multiply by 100 for a percentage if so.

**1d — Look up the alerting policy definition (works regardless of current state):**
```
gcloud monitoring policies list --project=<project> \
  --filter='display_name="<alert-name-from-alert>"' \
  --format="table(displayName,enabled,conditions[].displayName)"
```

**1e — Detect flapping/recurring pattern:**
Use `prometheus_query_range` on the same metric the alert fires on (see 1c) over `startsAt - 2h` to `startsAt + 1h` and look for the metric crossing the alert threshold repeatedly. Cross-check against prior incidents already loaded into your context for this alert.

**1f — Recent deploys (did code change recently?):**
```
kubectl --context=gke_prod-project_asia-south1_gke-cluster get replicasets -n <namespace> --sort-by=.metadata.creationTimestamp -o wide | tail -15
```

**1g — AlloyDB operations (scaling, failover, maintenance):**
```
gcloud alloydb operations list --cluster=<cluster-id> --region=asia-south1 \
  --format="table(name,operationType,status,startTime)"
```

**After Step 1:** Identify the **highest-CPU instance** — this is the target for all subsequent steps. Note whether it's the PRIMARY or a READ_POOL instance. Also note if ALL instances spiked simultaneously (points to traffic surge or external dependency, not a single bad query).

---

## Step 2: Characterize the Spike — Metrics Deep Dive

Run all of these in parallel on the **highest-CPU instance**. Direct SQL is the primary characterization source; `prometheus_query_range` fills in instance-level resource trends.

**2a — Related instance metrics (one `prometheus_query_range` call each for parallelism):**
Run `prometheus_query_range` for each of the following, with start `<startsAt - 10m>`, end `<startsAt + 1h>`, step `1m`. **Use the exact GCP metric names from the Site Knowledge Base (`knowledge-gcp.md`)** — the shapes below are illustrative:
- Connections: `avg(alloydb_instance_postgres_connections{instance="<target-instance>"})`
- Memory available: `avg(alloydb_instance_memory_min_available_memory{instance="<target-instance>"})`
- Replication byte-lag (READ_POOL only): `max(alloydb_instance_postgresql_replication_replicas_byte_lag{instance="<target-instance>"})`
- Transaction / disk rate (if mirrored): consult the Site Knowledge Base — AlloyDB does not expose ReadIOPS/WriteIOPS the way RDS does.

Live connection/state snapshot via SQL (high confidence, do this regardless):
```
db_query(<connection>, "SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY count DESC")
```

**2b — 7-day baseline comparison (MANDATORY — "high" is meaningless without a baseline):**
Run `prometheus_query_range` on the CPU metric for the same instance over the window 7 days earlier:
- query (shape): `avg(alloydb_instance_cpu_utilization{instance="<target-instance>"})` — exact name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 7d - 15m>`, end: `<startsAt - 7d + 1h>`, step: `5m`

Do the same for the connections metric. If current values are within 20% of 7-day-ago values → this is normal load, not an incident.

**Characterization matrix (fill this out after Step 2):**
```
Instance: <name> (<PRIMARY/READ_POOL>, <cpuCount> vCPU)
CPU:         <current>% (baseline: <7d-ago>%)
Connections: <current> (baseline: <7d-ago>)
FreeMemory:  <current MB> (baseline: <7d-ago MB>)
ReplicaLag:  <if READ_POOL, byte lag>
Pattern:     SPIKE / GRADUAL / STEP / NORMAL
```

---

## Step 3: Find the Culprit Query — pg_stat_statements + Query Insights

AlloyDB has `pg_stat_statements` enabled. This is the GCP equivalent of AWS Performance Insights top-SQL, and it is the high-confidence source. AlloyDB **System Insights / Query Insights** (Cloud Monitoring console) is the GUI equivalent if you need a visual.

Run these in parallel:

**3a — Top SQL by load:**
```
db_query(<connection>, "SELECT queryid, calls, round(total_exec_time) AS total_ms, round(mean_exec_time) AS mean_ms, rows, left(query,150) AS query FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10")
```

**3b — Wait events (what the DB is spending time on) — approximated via pg_stat_activity:**
```
db_query(<connection>, "SELECT wait_event_type, wait_event, count(*) FROM pg_stat_activity WHERE state = 'active' AND wait_event IS NOT NULL GROUP BY wait_event_type, wait_event ORDER BY count DESC LIMIT 15")
```

**3c — Top SQL on the PRIMARY too (if target is a READ_POOL instance):**
If the highest-CPU instance is a READ_POOL node, also run 3a against the PRIMARY — heavy writes on the PRIMARY cause replication load on read pools. (Note: `pg_stat_statements` is per-instance; connect to the PRIMARY's connection to see its statements.)

**Interpret wait events:**
| Wait Event | Meaning | Likely Cause |
|---|---|---|
| `IO:DataFileRead` dominant | Full table scans | Missing index or bloated table |
| `IO:XactSync` + `Timeout:VacuumDelay` | Write + vacuum | Autovacuum running on large table |
| `Lock:relation` or `Lock:tuple` | Lock contention | DDL or long-running transaction holding locks |
| `CPU` (wait_event NULL while active) dominant | Pure compute | Complex query, JSON parsing, regex in WHERE |
| `LWLock:BufferMapping` | Buffer pool contention | Working set exceeds shared_buffers |
| `Client:ClientRead` | Waiting for app | App is slow consuming results, connection pool issue |

**If `pg_stat_statements` is empty or the extension is unavailable:**
```
db_query(<connection>, "SELECT extname FROM pg_extension WHERE extname = 'pg_stat_statements'")   # confirm extension is present
```
Fall back to live activity sampling via `pg_stat_activity` (Step 4) and AlloyDB Query Insights in the Cloud Monitoring console. AlloyDB does not expose downloadable instance log files the way RDS does; use Cloud Logging:
```
gcloud logging read 'resource.type="alloydb.googleapis.com/Instance" AND resource.labels.instance_id="<instance-id>"' --project=<project> --limit=50 --freshness=1h   # VERIFY exact resource.type / label names against Site Knowledge Base
```

---

## Step 4: Direct SQL Diagnostics (if database toolset is enabled)

Run `learnings_read(database)` first to get the PostgreSQL diagnostic query templates.

**Run all of these in parallel on the affected DB's PG connection (bap_pg for customer, bpp_pg for driver):**

**4a — Active queries consuming CPU:**
```
db_query(<connection>, "SELECT pid, now() - query_start AS duration, state, wait_event_type, wait_event, left(query, 200) as query FROM pg_stat_activity WHERE state = 'active' AND query NOT LIKE '%pg_stat_activity%' ORDER BY duration DESC LIMIT 20")
```

**4b — Long-running queries (stuck queries):**
```
db_query(<connection>, "SELECT pid, now() - query_start AS duration, state, wait_event_type, wait_event, left(query, 200) as query FROM pg_stat_activity WHERE state = 'active' AND now() - query_start > interval '5 seconds' ORDER BY duration DESC LIMIT 20")
```

**4c — Connection count by application (pool exhaustion check):**
```
db_query(<connection>, "SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY application_name, state ORDER BY count DESC LIMIT 30")
```

**4d — Tables with sequential scans (missing index check):**
```
db_query(<connection>, "SELECT relname, seq_scan, idx_scan, seq_tup_read, CASE WHEN seq_scan > 0 THEN round(seq_tup_read::numeric / seq_scan) ELSE 0 END AS avg_rows_per_scan FROM pg_stat_user_tables WHERE seq_scan > 100 AND seq_tup_read > 100000 ORDER BY seq_tup_read DESC LIMIT 20")
```

**4e — Lock contention:**
```
db_query(<connection>, "SELECT blocked.pid, left(blocked.query, 100) as blocked_query, blocking.pid as blocking_pid, left(blocking.query, 100) as blocking_query, now() - blocked.query_start AS blocked_duration FROM pg_stat_activity blocked JOIN pg_locks bl ON bl.pid = blocked.pid AND NOT bl.granted JOIN pg_locks kl ON kl.locktype = bl.locktype AND kl.database = bl.database AND kl.relation = bl.relation AND kl.page = bl.page AND kl.tuple = bl.tuple AND kl.pid != bl.pid AND kl.granted JOIN pg_stat_activity blocking ON blocking.pid = kl.pid LIMIT 10")
```

**4f — Table bloat (autovacuum check):**
```
db_query(<connection>, "SELECT relname, n_live_tup, n_dead_tup, round(n_dead_tup::numeric / greatest(n_live_tup, 1) * 100, 1) AS dead_pct, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables WHERE n_dead_tup > 100000 ORDER BY n_dead_tup DESC LIMIT 15")
```

**4g — Currently running autovacuum:**
```
db_query(<connection>, "SELECT pid, now() - query_start AS duration, left(query, 150) as query FROM pg_stat_activity WHERE query LIKE 'autovacuum:%' ORDER BY duration DESC")
```

---

## Step 4B: Deep Query Analysis (ALWAYS DO THIS when a culprit query is identified)

When Step 3 (`pg_stat_statements`) identifies a high-load query, you MUST dig into **why** that query is slow. Don't just report "query X is consuming 40% of total_exec_time" — find out if it's missing an index, doing a sequential scan, or hitting a bloated table.

**4B-a — EXPLAIN ANALYZE the culprit query:**
Take the normalized SQL from `pg_stat_statements` (Step 3a), fill in reasonable parameter values, and run:
```
db_query(<connection>, "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) <the-culprit-query-with-sample-params>")
```
Look for:
- `Seq Scan` on large tables → missing index
- `Rows Removed by Filter:` huge number → index exists but doesn't cover the WHERE clause
- `Sort` with `external merge Disk` → not enough work_mem, spilling to disk
- `Nested Loop` with high actual rows vs estimated → planner misestimate, needs ANALYZE
- `Buffers: shared read` very high → cold cache, data not in shared_buffers

**4B-b — Check indexes on the culprit table:**
```
db_query(<connection>, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = '<table-from-culprit-query>' ORDER BY indexname")
```

**4B-c — Check table size and row count:**
```
db_query(<connection>, "SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) as total_size, pg_size_pretty(pg_relation_size(oid)) as table_size, pg_size_pretty(pg_indexes_size(oid)) as index_size, reltuples::bigint as estimated_rows FROM pg_class WHERE relname = '<table-from-culprit-query>'")
```

**4B-d — Check if table stats are stale (planner may be using wrong estimates):**
```
db_query(<connection>, "SELECT relname, last_analyze, last_autoanalyze, n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname = '<table-from-culprit-query>'")
```

**4B-e — Check column statistics for WHERE clause columns:**
```
db_query(<connection>, "SELECT attname, n_distinct, most_common_vals, most_common_freqs, correlation FROM pg_stats WHERE tablename = '<table-from-culprit-query>' AND attname IN ('<where-column-1>', '<where-column-2>')")
```

**Interpretation:**
- If `Seq Scan` + no matching index for the WHERE clause → **Missing index**. Report which columns need indexing.
- If index exists but `Seq Scan` still used → Table stats may be stale (run ANALYZE), or query planner estimated index scan as more expensive (check `n_distinct`, `correlation`).
- If `Index Scan` but still slow → Index is there but query returns too many rows, or table is extremely large. Check if a composite index would help.
- If `Rows Removed by Filter` >> actual rows → The index doesn't cover the filter. A more selective index is needed.

---

## Step 5: Check Business Impact (Run in Parallel with Steps 3-4)

**5a — 5xx error rate (Prometheus):**
- query: `sum by(service,handler)(rate(http_request_duration_seconds_count{handler!="/v1/",status_code=~"^5.."}[1m]))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5b — P99 latency (Prometheus):**
- query: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5c — GCP HTTPS Load Balancer 5xx (fallback if app Prometheus series unavailable):**
Use `prometheus_query_range` on the mirrored LB metric:
- query (shape): `sum(rate(loadbalancing_googleapis_com_https_request_count{response_code_class="500"}[1m]))` — use the exact GCP metric + label name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5d — GCP HTTPS LB backend latency:**
Use `prometheus_query_range`:
- query (shape): `avg(loadbalancing_googleapis_com_https_backend_latencies)` — exact name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5e — Key business metrics (check Site Knowledge Base for deployment-specific metrics):**
- query: use business-critical metrics from knowledge base (e.g., conversion rates, transaction counts)
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `5m`

**Impact assessment — fill this out:**
```
5xx rate:    NONE / LOW (<10/min) / MEDIUM (10-100/min) / HIGH (>100/min)
P99 latency: NORMAL / DEGRADED (>3s) / SEVERE (>10s)
LB 5xx:      <count per minute>
Business:    STABLE / DEGRADED — <which metrics affected>
User impact: YES / NO — <describe affected user operations>
```

---

## Step 6: Correlate — Application-Side Evidence

**ONLY run this step if Steps 1-5 did NOT give a clear root cause.** Skip if you already identified the culprit.

**6a — App-side DB errors from services connected to this DB:**
```
kubectl --context=gke_prod-project_asia-south1_gke-cluster logs -n <namespace> -l app=<service-name> --since=15m --tail=200 2>/dev/null \
  | grep -iE 'connection|timeout|refused|deadlock|pool|too many|query' | head -30
```

**6b — Elasticsearch search for DB errors:**
Use `elasticsearch_search` tool:
```json
{
  "index": "<app-log-index-from-knowledge-base>",
  "size": 20,
  "sort": [{"@timestamp": "desc"}],
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "<startsAt-10min>", "lte": "<startsAt+1h>"}}},
        {"bool": {
          "should": [
            {"match": {"message": "connection refused"}},
            {"match": {"message": "too many connections"}},
            {"match": {"message": "deadlock"}},
            {"match": {"message": "query timeout"}},
            {"match": {"message": "connection pool"}},
            {"match": {"message": "statement timeout"}},
            {"match": {"message": "canceling statement"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "_source": ["message", "@timestamp", "service"]
}
```

**6c — Check if errors are NEW or pre-existing:**
Run the same ES query for **yesterday's same time window**. If the same errors appear yesterday → pre-existing, NOT caused by this incident.

---

## Synthesis — Hypothesis Verification Matrix

**MANDATORY: Work through EVERY hypothesis below. For each one, state CONFIRMED / RULED OUT / INCONCLUSIVE with specific evidence.**

### Hypothesis 1: Missing Index / Full Table Scan
**Check:** `pg_stat_statements` top SQL shows one query > 40% of total_exec_time + read I/O / DataFileRead wait spike
**Verify:** The query's table has high `seq_scan` in `pg_stat_user_tables` + low `idx_scan`
**Rule out:** If read I/O is normal and no single query dominates → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 2: Bad Deploy / New Expensive Query
**Check:** `pg_stat_statements` top SQL shows a query pattern not seen before + CPU spike correlates with deploy time
**Verify:** `kubectl --context=gke_prod-project_asia-south1_gke-cluster get replicasets -n <ns> --sort-by=.metadata.creationTimestamp` shows deploy within 30min of spike
**Rule out:** If no deploy happened recently AND top queries are known patterns → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 3: Autovacuum
**Check:** Wait events show `Timeout:VacuumDelay` or `IO:XactSync` dominant + write activity high
**Verify:** `pg_stat_activity` shows `autovacuum:` query running + `pg_stat_user_tables` shows high `n_dead_tup` on affected table
**Rule out:** If no autovacuum in pg_stat_activity AND VacuumDelay not in wait events → NOT this
**Self-resolves:** YES — autovacuum will complete. Note estimated time based on dead tuple count.
**Confidence if confirmed:** HIGH

### Hypothesis 4: Connection Pool Exhaustion
**Check:** AlloyDB connections metric surged > 2x baseline + app logs show "too many clients" / "connection pool" / "could not obtain connection"
**Verify:** `pg_stat_activity` grouped by `application_name` shows one app dominating connections
**Rule out:** If connections are normal and no pool errors in logs → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 5: Lock Contention
**Check:** Wait events show `Lock:relation` or `Lock:tuple` + pg_locks query shows blocked queries
**Verify:** Identify the blocking query and how long it's been holding locks
**Rule out:** If no lock-related wait events AND pg_locks shows no contention → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 6: Replication Lag (READ_POOL only)
**Check:** Target is a READ_POOL instance + PRIMARY write activity is high + `replicas_byte_lag` metric is increasing
**Verify:** READ_POOL CPU correlates with PRIMARY write activity, not the read pool's own queries
**Rule out:** If target is the PRIMARY OR byte lag is negligible → NOT this
**Confidence if confirmed:** HIGH if lag is large / growing

### Hypothesis 7: Memory Pressure
**Check:** `min_available_memory` dropped > 50% from baseline + swap activity
**Verify:** If `min_available_memory` is very low (< 500MB), instance is under memory pressure → CPU spent on I/O, not queries
**Rule out:** If `min_available_memory` is > 2GB and stable → NOT this
**Confidence if confirmed:** MEDIUM

### Hypothesis 8: Background Job / Analytics Query
**Check:** CPU high + no 5xx + no business impact + `pg_stat_statements` shows batch/analytics query
**Verify:** The heavy query has identifiable batch pattern (large table scan, aggregation, COPY, pg_dump)
**Rule out:** If there IS 5xx or business impact → NOT just a background job
**Confidence if confirmed:** MEDIUM (low urgency)

### Hypothesis 9: Traffic Surge (ALL instances spike together)
**Check:** CPU spiked on ALL instances simultaneously (PRIMARY + all READ_POOL nodes) + connections surged across the board
**Verify:** Prometheus shows incoming request rate spike at the same time: `sum(rate(http_request_duration_seconds_count[1m])) by (service)`
**Rule out:** If only one instance spiked → NOT traffic surge, it's a query/instance-specific issue
**Confidence if confirmed:** HIGH
**Fix:** Read pool node count / autoscaling should absorb it. If it didn't, check AlloyDB read pool sizing.

### Hypothesis 10: Configuration / Database Flag Change
**Check:** `gcloud alloydb operations list` shows an instance modification (UPDATE) around the spike time
**Verify:**
```
gcloud alloydb operations list --cluster=<cluster-id> --region=asia-south1 \
  --format="table(name,operationType,status,startTime)"
```
Look for: `UPDATE` / `UPGRADE` operations, or a database-flag change on the instance (`gcloud alloydb instances describe <instance-id> --cluster=<cluster-id> --region=asia-south1` → check `databaseFlags`).
**Rule out:** If no operations in the last 24h → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 11: Normal Load / False Alarm
**Check:** Current CPU is within 20% of 7-day baseline + no anomaly in any metric
**Verify:** Alert threshold may be set too low for this instance's normal load
**Rule out:** If CPU is genuinely > 2x baseline → this IS an anomaly
**Confidence if confirmed:** HIGH (no action needed)
**Fix:** Adjust alert policy threshold to match normal load pattern.

---

## Final Verdict

After verifying all hypotheses, state:

```
## Verified Hypotheses
| # | Hypothesis | Verdict | Key Evidence |
|---|-----------|---------|--------------|
| 1 | Missing index / full table scan | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 2 | Bad deploy / new query | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 3 | Autovacuum | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 4 | Connection pool exhaustion | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 5 | Lock contention | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 6 | Replication lag | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 7 | Memory pressure | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 8 | Background job / analytics | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 9 | Traffic surge (all instances) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 10 | Configuration / flag change | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 11 | Normal load / false alarm | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |

## Root Cause
<Confirmed hypothesis with full evidence chain>

## Confidence: HIGH / MEDIUM / LOW
<Why this confidence level — what evidence supports it, what's missing>

## Business Impact
5xx: <rate and trend>
Latency: <p99 and trend>
Users affected: <yes/no, which operations>

## Immediate Fix
<Exact action needed, or "No action needed — self-resolving" with estimated time>
- If missing index: "CREATE INDEX CONCURRENTLY idx_<table>_<column> ON <table>(<column>)" — requires DBA approval
- If bad deploy: "Rollback deployment <name> to previous revision: kubectl --context=gke_prod-project_asia-south1_gke-cluster rollout undo deployment/<name> -n <ns>"
- If autovacuum: "No action — autovacuum will complete in ~X minutes. Monitor CPU."
- If connection pool: "Scale down <service> HPA / restart pods to release connections"
- If lock contention: "Identify and terminate blocking PID <pid>: SELECT pg_terminate_backend(<pid>)" — requires DBA approval
- If replication lag: "No immediate fix — reduce write load or add read pool capacity. Monitor lag."
- If memory pressure: "Consider machine config upgrade (more vCPU/memory) from <current> to <recommended>"
- If background job: "No urgent action — schedule batch jobs during off-peak hours"
- If traffic surge: "Verify read pool sizing absorbed it. If not, increase read pool node count."
- If configuration / flag change: "Revert the database flag change or instance modification"
- If normal load: "Adjust alert policy threshold from <current> to <recommended>"

## Prevention
<What change prevents recurrence — be specific>

## Needs More Investigation
YES / NO — <if YES, what specifically needs checking and by whom>
```

---

## Extended Investigation

If ALL hypotheses are INCONCLUSIVE after the above steps:
- Correlate timestamps across ALL sources: metrics spike, log errors, pod restarts, deploys, external events
- Check upstream/downstream services this DB depends on
- Look for scheduled jobs (cron, batch) running at the incident time
- Check `kubectl --context=gke_prod-project_asia-south1_gke-cluster get events -n <namespace>` for pod restarts or node pressure
- Check if an AlloyDB scaling/maintenance event occurred: `gcloud alloydb operations list --cluster=<cluster-id> --region=asia-south1 --format="table(name,operationType,status,startTime)"`
- Check current database flags for an unexpected change: `gcloud alloydb instances describe <instance-id> --cluster=<cluster-id> --region=asia-south1` → inspect `databaseFlags`
