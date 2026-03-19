# Redis / ElastiCache Investigation Runbook

## Goal
- **Primary Objective:** Investigate Redis/ElastiCache alerts — identify what is stressing Redis, which service/key pattern is responsible, and determine root cause with remediation.
- **Scope:** AWS ElastiCache Redis clusters: app-redis-cluster-001 (44 shards, main app), location-redis (4 nodes, location tracking), utils-redis (1 node).
- **Covers:** RedisHighCPU, RedisHighMemory, RedisEvictions, RedisHighConnections, Redis Node Memory beyond 90%, ProtocolRedis-CPU/EngineCPU/Memory/Connections, bandwidth saturation.
- **Expected Outcome:** Which cluster, which metric is abnormal, which service caused it, root cause, and fix.

## Context
Redis is used for session caching, location data (location-redis), state management, and KV store (drainer queues). When Redis is stressed, it cascades — evictions cause cache misses → more DB reads → RDS CPU spike → 5xx errors.

**A Fast RCA (preliminary analysis) may already be injected above.** If present, it already checked CPU/memory/evictions/connections/bandwidth across all clusters and app-side Redis errors. Don't repeat those — go deeper.

## IMPORTANT: Tool Routing
- **ElastiCache metrics**: Use `aws cloudwatch get-metric-statistics` via bash — NOT prometheus. ElastiCache metrics are NOT in VictoriaMetrics.
- **Application logs**: Use `elasticsearch_search` tool
- **Pod logs for Redis errors**: Use `stern` or `kubectl logs`

## Time Window Instructions
- Use the alert's `startsAt` as your investigation anchor.
- For all queries: `start = startsAt - 10 min`, `end = startsAt + 1 hour`
- Always state the time window in your findings.

## Infrastructure Reference
Refer to the **Site Knowledge Base** for:
- Redis cluster IDs: app-redis-cluster-001, location-redis, utils-redis
- Node naming: app-redis-cluster-001-XXXX-001 (shard-replica), location-redis-001 to location-redis-004
- Services → Redis mapping: BAP + BPP → app-redis-cluster-001, LTS + allocator → location-redis

---

## Step 1 — Identify Which Cluster and What's Wrong (parallel, skip if Fast RCA provided)

**CPU across all clusters:**
```bash
for cluster in app-redis-cluster-001 location-redis utils-redis; do
  echo "=== $cluster ===" &&
  aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization \
    --dimensions Name=ReplicationGroupId,Value=$cluster \
    --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
    --region ap-south-1 --output json |
  jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
done
```

**Per-node memory + connections + evictions** (sample a few nodes from the hot cluster):
```bash
for node in app-redis-cluster-001-0001-001 app-redis-cluster-001-0022-001 location-redis-001; do
  echo "=== $node ===" &&
  for metric in DatabaseMemoryUsagePercentage CurrConnections Evictions; do
    echo "-- $metric --" &&
    aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name $metric \
      --dimensions Name=CacheClusterId,Value=$node \
      --start-time <start> --end-time <end> --period 60 --statistics Average Maximum Sum \
      --region ap-south-1 --output json |
    jq -r '.Datapoints|sort_by(.Timestamp)[-3:][]|"\(.Timestamp): \(.Average // .Maximum // .Sum)"'
  done
done
```

---

## Step 2 — Check if Scaling is In Progress

**Always check this first** — many Redis alerts fire during planned scaling operations:
```bash
aws elasticache describe-replication-groups --replication-group-id app-redis-cluster-001 --region ap-south-1 --query 'ReplicationGroups[0].Status' --output text
```
```bash
aws elasticache describe-events --source-type replication-group --duration 60 --region ap-south-1 --output json | jq -r '.Events[:10][]|"\(.Date): \(.SourceIdentifier) — \(.Message)"'
```

- If status is `modifying` + events show "Migrating slots" or "Adding cache node" → **Scenario A: Scaling in progress**. MOVED errors and brief timeouts are EXPECTED. Will self-resolve.
- If status is `available` → proceed to determine which scenario below.

## Step 3 — Determine Scenario

| Condition | Scenario |
|-----------|----------|
| Cluster status "modifying" + slot migration events | **A: Scaling in progress** |
| EngineCPU high + app logs show KEYS/SMEMBERS/SORT | **B: Expensive command** |
| Memory > 80% + evictions > 0 | **C: Memory full** |
| CurrConnections surged + "connection refused" in logs | **D: Connection storm** |
| NetworkBandwidthOutAllowanceExceeded > 0 | **E: Bandwidth saturation** |
| App logs show CLUSTERDOWN / MOVED + status NOT modifying | **F: Cluster topology issue** |
| All metrics normal, no errors | **G: Transient / self-resolved** |

---

## Step 4 — Full Metrics Assessment (ALWAYS DO THIS)

Regardless of scenario, collect ALL key metrics and explicitly state for each whether it is normal or abnormal. The final report MUST include a table like:

| Metric | Cluster | Value | Status |
|--------|---------|-------|--------|
| EngineCPUUtilization | app-redis-cluster-001 | 1% avg / 3% max | NORMAL |
| EngineCPUUtilization | location-redis | 17% avg | NORMAL (baseline ~15%) |
| DatabaseMemoryUsagePercentage | app-redis-cluster-001 | 2% | NORMAL |
| Evictions | all clusters | 0 | NORMAL |
| CurrConnections | app-redis-cluster-001 | 1530 | NORMAL |
| NetworkBandwidthOutAllowanceExceeded | all | 0 | NORMAL |
| Cluster Status | app-redis-cluster-001 | modifying | SCALING IN PROGRESS |

Check every metric for every cluster, even the ones that seem fine. State "NORMAL" or "ABNORMAL: <reason>" for each.

For memory, also compute per-shard distribution if memory is high — some shards may be hot while average looks OK:
```bash
for i in $(seq -w 1 44); do
  node="app-redis-cluster-001-00${i}-001"
  val=$(aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name DatabaseMemoryUsagePercentage \
    --dimensions Name=CacheClusterId,Value=$node \
    --start-time <start> --end-time <end> --period 300 --statistics Maximum \
    --region ap-south-1 --output json | jq -r '.Datapoints|sort_by(.Timestamp)[-1:][]|.Maximum')
  [ -n "$val" ] && echo "$node: ${val}%"
done | sort -t: -k2 -rn | head -10
```

---

## Step 5 — Deep Dive by Scenario

### Scenario A: Scaling In Progress
1. Confirm: `cluster_status` is "modifying", events show slot migration
2. Check how long scaling has been running:
   ```bash
   aws elasticache describe-events --source-type replication-group --duration 1440 --region ap-south-1 --output json | jq -r '.Events[]|select(.SourceIdentifier=="app-redis-cluster-001")|"\(.Date): \(.Message)"' | tail -20
   ```
3. Check if MOVED errors are causing app-side 5xx:
   ```
   prometheus: sum(increase(istio_requests_total{response_code=~"5..",reporter="source"}[5m])) by (destination_service_name)
   ```
4. **Conclusion:** If scaling is in progress and errors are minor → report as expected, will self-resolve. If scaling has been running > 2 hours or causing significant 5xx → escalate.

### Scenario B: Expensive Redis Command
1. Check app logs for expensive commands:
   ```bash
   stern -n atlas app-backend-production-pilot --since=30m --no-follow | grep -iE 'KEYS|SMEMBERS|LRANGE|SORT|SCAN|SLOWLOG' | head -30
   stern -n atlas provider-app-production --since=30m --no-follow | grep -iE 'KEYS|SMEMBERS|LRANGE|SORT|SCAN|SLOWLOG' | head -30
   ```

2. Search ES for Redis-related errors:
   ```json
   {
     "index": "protocol-lp-logs-<YYYY-MM-DD>",
     "size": 20,
     "query": {
       "bool": {
         "must": [
           {"range": {"@timestamp": {"gte": "<start>", "lte": "<end>"}}},
           {"bool": {
             "should": [
               {"match": {"message": "redis"}},
               {"match": {"message": "SLOWLOG"}},
               {"match": {"message": "timeout"}}
             ],
             "minimum_should_match": 1
           }}
         ]
       }
     },
     "_source": ["message", "@timestamp"]
   }
   ```

3. Check recent deployments (a new deploy may have introduced a bad command pattern):
   ```bash
   kubectl get events -n atlas --sort-by=.lastTimestamp | grep -iE "pulled|deploy|image" | tail -15
   ```

### Scenario C: Memory Full / Evictions
1. Check memory across more nodes to find the hottest shard:
   ```bash
   for i in $(seq -w 1 44); do
     node="app-redis-cluster-001-00${i}-001"
     val=$(aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name DatabaseMemoryUsagePercentage \
       --dimensions Name=CacheClusterId,Value=$node \
       --start-time <start> --end-time <end> --period 300 --statistics Maximum \
       --region ap-south-1 --output json | jq -r '.Datapoints|sort_by(.Timestamp)[-1:][]|.Maximum')
     [ -n "$val" ] && echo "$node: ${val}%"
   done | sort -t: -k2 -rn | head -10
   ```

2. Check eviction policy:
   ```bash
   aws elasticache describe-cache-parameters --cache-parameter-group-name default.redis7.cluster.on --region ap-south-1 | grep -iE "maxmemory|eviction"
   ```

3. Check if cache hit ratio dropped (evictions → cache misses → DB overload):
   ```bash
   for metric in CacheHits CacheMisses; do
     echo "=== $metric ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name $metric \
       --dimensions Name=CacheClusterId,Value=app-redis-cluster-001-0001-001 \
       --start-time <start> --end-time <end> --period 300 --statistics Sum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Sum)"'
   done
   ```

### Scenario D: Connection Storm
1. Check NewConnections rate:
   ```bash
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name NewConnections \
     --dimensions Name=CacheClusterId,Value=app-redis-cluster-001-0001-001 \
     --start-time <start> --end-time <end> --period 60 --statistics Maximum \
     --region ap-south-1 --output json |
   jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Maximum) new/min"'
   ```

2. Check if HPA scaled up recently (more pods = more connections):
   ```bash
   kubectl get events -n atlas --sort-by=.lastTimestamp | grep -iE "scaled|replica|hpa" | tail -20
   kubectl get hpa -n atlas | head -20
   ```

3. Check app logs for connection errors:
   ```bash
   stern -n atlas app-backend-production-pilot --since=15m --no-follow | grep -iE 'redis.*connection|too many|refused|pool' | head -20
   ```

### Scenario E: Bandwidth Saturation
1. Check bandwidth exceeded per node:
   ```bash
   for node in app-redis-cluster-001-0001-001 app-redis-cluster-001-0022-001 location-redis-001; do
     echo "=== $node ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name NetworkBandwidthOutAllowanceExceeded \
       --dimensions Name=CacheClusterId,Value=$node \
       --start-time <start> --end-time <end> --period 60 --statistics Sum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Sum) exceeded"'
   done
   ```

2. This usually means a large key is being read frequently. Check app logs for large value transfers.
3. **Fix:** Upgrade node type for more bandwidth, or split hot keys.

### Scenario F: CLUSTERDOWN / Shard Failure
1. Check cluster status:
   ```bash
   aws elasticache describe-replication-groups --replication-group-id app-redis-cluster-001 --region ap-south-1 \
     --query 'ReplicationGroups[0].[Status,NodeGroups[*].[NodeGroupId,Status,PrimaryEndpoint.Address]]'
   ```

2. Check app logs for MOVED/CLUSTERDOWN:
   ```bash
   stern -n atlas app-backend-production-pilot --since=15m --no-follow | grep -iE 'CLUSTERDOWN|MOVED|NOGROUP|ASK' | head -20
   ```

3. Check if failover happened:
   ```bash
   aws elasticache describe-events --source-type replication-group --duration 60 --region ap-south-1
   ```

### Scenario G: Transient / Self-Resolved
1. Run 7-day baseline to confirm this is recurring:
   ```bash
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization \
     --dimensions Name=ReplicationGroupId,Value=app-redis-cluster-001 \
     --start-time <7-days-ago> --end-time <now> --period 3600 --statistics Average Maximum \
     --region ap-south-1
   ```
2. If matches daily pattern → report as baseline.

---

## Step 4 — Check Cascading Impact

Redis issues often cascade. Check:

**RDS CPU spike (cache misses → DB overload):**
```bash
for i in provider-db-w3 driver-r1 customer-w1 app-db-r1; do
  echo "=== $i ===" &&
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$i \
    --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
    --region ap-south-1 --output json |
  jq -r '.Datapoints|sort_by(.Timestamp)[-5:][]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
done
```

**5xx error rate:**
```
prometheus: sum(increase(istio_requests_total{response_code=~"5..",reporter="source"}[5m])) by (destination_service_name, response_code)
```

**Business metrics:**
```
prometheus: rate(ride_created_count[5m]) / rate(search_request_count[5m])
```

---

## If Fast RCA Was Provided

If a Fast RCA result was injected at the top:
1. **Don't repeat metric collection** — fast RCA already checked CPU, memory, evictions, connections, bandwidth across all clusters.
2. **Go deeper based on scenario:**
   - **Memory full:** Find the hottest shard, check eviction policy, check cache hit/miss ratio
   - **CPU high:** Search app logs for expensive commands (KEYS, SMEMBERS)
   - **Connection storm:** Check HPA events, NewConnections rate, app connection pool config
   - **Bandwidth:** Identify which node is saturated, look for hot keys
3. **Check cascading impact** — RDS CPU, 5xx rate, business metrics
4. **7-day baseline** to determine if this is a new issue or recurring pattern

---

## RCA Report Requirements

Your final report MUST clearly distinguish between **verified facts** and **assumptions**:

### For every claim, state your evidence:
- **VERIFIED**: "CPU spiked to 85% at 14:32 UTC (CloudWatch data shows...)" — you have the metric/log/event data
- **LIKELY**: "This is likely caused by the autovacuum based on IO:VacuumDelay wait events" — you have supporting but not conclusive evidence
- **UNVERIFIED ASSUMPTION**: "Traffic spike may have caused the connection surge — no traffic metrics checked" — you're inferring without data

### Structure your conclusion as:
1. **What happened** (facts only — timestamps, metrics, error messages)
2. **Why it happened** (verified root cause with evidence, OR "likely cause" with reasoning)
3. **What was NOT checked** (explicitly list things you couldn't verify — e.g. "did not check if key TTLs changed recently")
4. **Impact** (verified: 5xx rate, business metrics. Or "impact not measured")
5. **Recommended fix** (with confidence level)

Never state a root cause without evidence. If you can't determine the root cause with data, say "root cause undetermined — nearest hypothesis is X based on Y, but Z was not checked."

---

## Extended Investigation

If the above steps don't identify root cause:
- Check if a cron job runs at this time (see knowledge base for cron schedules)
- Check if traffic spiked (HPA scale-up events, request rate metrics)
- Look for patterns: is it one shard or all? One cluster or multiple?
- Check if a deployment changed Redis key patterns or TTLs
- Use `db_query` to check if app-side cache miss rate increased
