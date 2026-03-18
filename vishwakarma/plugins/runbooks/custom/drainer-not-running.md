# Drainer Investigation Runbook

## Goal
- **Primary Objective:** Determine why the driver or customer drainer has stopped processing or is lagging, and identify root cause.
- **Scope:** Drainer deployments in namespace `atlas`: `drainer-service-production`, `protocol-app-drainer-production`.
- **Covers:** NoDriverDrainerRunning, NoAppDrainerRunning, NoDriverDrainerPodRunning, NoCustomerDrainerPodRunning, DriverDrainerLagIncreasing, CustomerDrainerLagIncreasing.
- **Expected Outcome:** Identify the exact scenario, root cause, and remediation.

## Context
Drainers are workers that read from a Redis queue and write to PostgreSQL. When a drainer stops, DB writes are delayed — rides, driver updates, and payments can be affected. This alert fires 15+/week and has only 4-6 known root causes.

**A Fast RCA (preliminary analysis) may already be injected above.** If present, verify or refute it — don't repeat the same checks. Focus on going deeper.

## Time Window Instructions
- Use the alert's `startsAt` as your investigation anchor.
- For all queries: `start = startsAt - 10 min`, `end = startsAt + 1 hour`
- For stern/kubectl logs: calculate `--since` from startsAt.
- Always state the time window in your findings.

## Infrastructure Reference
Refer to the **Site Knowledge Base** for:
- Drainer pod labels and deployment names
- RDS instance identifiers (driver: provider-db-w3, driver-r1; customer: customer-w1, app-db-r1)
- Redis cluster IDs
- VictoriaMetrics URL

---

## Step 1 — Confirm Pod State and Metrics (parallel)

Run these in parallel:

```bash
kubectl get pods -n atlas --no-headers | grep -i drainer
```

```
prometheus: driver_drainer_stop_status   (driver drainer)
prometheus: drainer_stop_status          (customer drainer)
prometheus: rate(driver_query_drain_latency_count[5m])   (driver drain rate)
prometheus: rate(query_drain_latency_count[5m])           (customer drain rate)
```

```bash
kubectl get events -n atlas --sort-by=.lastTimestamp | grep -i drainer | tail -20
```

From these three results, determine which scenario applies:

| Condition | Scenario |
|-----------|----------|
| Pods DOWN (0/0 or CrashLoopBackOff) | → Scenario A |
| Pods UP + stop_status > 0 + SQL errors in logs | → Scenario B |
| Pods UP + stop_status > 0 + connection errors in logs | → Scenario C |
| Pods UP + stop_status > 0 + Redis errors in logs | → Scenario D |
| Pods UP + drain_rate > 0 + no errors | → Scenario E |
| Pods UP + stop_status > 0 + no clear errors | → Scenario F |

---

## Step 2 — Follow the Matching Scenario

### Scenario A: Pods are DOWN (0 replicas or CrashLoopBackOff)

1. Check deployment:
   ```bash
   kubectl get deployment -n atlas | grep drainer
   ```

2. Check events for why:
   ```bash
   kubectl describe pod -n atlas <last-drainer-pod> | grep -A5 "Last State\|OOMKilled\|Reason\|Exit Code"
   ```

3. Check node pressure:
   ```bash
   kubectl get nodes -o wide | head -5
   kubectl describe node <node-of-drainer-pod> | grep -A10 "Conditions"
   ```

**Root cause:** OOM kill, node eviction, image pull failure, or manual scale-down.
**Fix:** `kubectl rollout restart deployment/<drainer> -n atlas`

---

### Scenario B: Pods RUNNING + SQL Error (MOST COMMON)

The drainer processes a queue of DB writes. A single bad record with a fatal SQL error halts the drainer internally while the pod stays alive.

1. Get error logs:
   ```bash
   stern -n atlas drainer-service --since=30m --no-follow | grep -i 'ERROR' | head -50
   ```

2. Look specifically for SQL patterns:
   ```bash
   stern -n atlas drainer-service --since=1h --no-follow | grep -iE "sqlState|BATCH_INSERT|integer out of range|value too long|constraint|deadlock|22003|23505" | head -30
   ```

3. If you find the error, search Elasticsearch for the full stack trace:
   ```json
   {
     "index": "<app-log-index-from-knowledge-base>-<YYYY-MM-DD>",
     "size": 10,
     "query": {
       "bool": {
         "must": [
           {"match": {"message": "BATCH_INSERT"}},
           {"match": {"message": "drainer"}},
           {"range": {"@timestamp": {"gte": "<startsAt-10min>", "lte": "<startsAt+1h>"}}}
         ]
       }
     },
     "_source": ["message", "@timestamp"]
   }
   ```

**Common SQL errors:**
- `sqlState 22003` — integer out of range (column overflow, e.g. Int32 exceeded)
- `value too long for type character varying` — varchar column overflow
- `BATCH_INSERT_FAILED` — bulk insert failed, check which table/column
- `23505 unique_violation` — duplicate key

**Root cause:** Bad/corrupt record in Redis drainer queue → SQL failure → drainer halts.
**Fix:** `kubectl rollout restart deployment/<drainer> -n atlas` (clears stopped state). Then fix the schema or bad record.

---

### Scenario C: Pods RUNNING + DB Connectivity Issue

1. Get connection error logs:
   ```bash
   stern -n atlas drainer-service --since=30m --no-follow | grep -iE "connection refused|too many connections|timeout|postgres|FATAL" | head -30
   ```

2. Check RDS health (driver drainer → driver RDS):
   ```bash
   for i in provider-db-w3 driver-r1; do
     echo "=== $i ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
       --dimensions Name=DBInstanceIdentifier,Value=$i \
       --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
   done
   ```

   For customer drainer, check customer RDS instead: `customer-w1`, `app-db-r1`, `customer-r3`.

3. Check RDS connections:
   ```bash
   for i in provider-db-w3 driver-r1; do
     echo "=== $i ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections \
       --dimensions Name=DBInstanceIdentifier,Value=$i \
       --start-time <start> --end-time <end> --period 60 --statistics Maximum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Maximum) connections"'
   done
   ```

**Root cause:** DB overloaded (CPU > 80%), connection pool exhausted (> 3000 connections), or RDS failover.
**Fix:** Resolve DB issue first, then restart drainer.

---

### Scenario D: Pods RUNNING + Redis Errors

1. Check drainer logs for Redis patterns:
   ```bash
   stern -n atlas drainer-service --since=30m --no-follow | grep -iE "CLUSTERDOWN|NOGROUP|MOVED|redis|connection refused" | head -30
   ```

2. Check Redis bandwidth saturation:
   ```bash
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache \
     --metric-name NetworkBandwidthOutAllowanceExceeded \
     --dimensions Name=CacheClusterId,Value=app-redis-cluster-001-0001 \
     --start-time <start> --end-time <end> --period 60 --statistics Sum \
     --region ap-south-1 --output json |
   jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Sum) packets"'
   ```

3. Check Redis CPU and memory:
   ```bash
   for cluster in app-redis-cluster-001 location-redis; do
     echo "=== $cluster ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization \
       --dimensions Name=ReplicationGroupId,Value=$cluster \
       --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
   done
   ```

**Root cause:** Redis cluster issue — bandwidth saturation, CLUSTERDOWN, or shard failure.
**Fix:** Redis issue must be resolved first; drainer will recover automatically once Redis is healthy.

---

### Scenario E: False Alarm (drain_rate > 0, no errors)

1. Confirm drainer is actively processing:
   ```
   prometheus: rate(driver_query_drain_latency_count[5m])
   ```
   If values are > 0 for all pods → drainer IS working.

2. Check if stop_status metric is stale:
   ```
   prometheus: driver_drainer_stop_status
   ```
   If all values = 0 but alert is still firing → stale alert, will self-resolve.

**Root cause:** Stale metric or transient spike that already resolved.
**Fix:** No action needed. Monitor for 5 minutes.

---

### Scenario F: Pods UP + stop_status > 0 + No Clear Errors

1. Get ALL recent logs (not just errors):
   ```bash
   stern -n atlas drainer-service --since=10m --no-follow | tail -100
   ```

2. Check drainer query execution metrics:
   ```
   prometheus: driver_drainer_query_executes
   ```

3. Check if there was a recent deployment:
   ```bash
   kubectl get events -n atlas --sort-by=.lastTimestamp | grep -iE "pulled|deploy|image|drainer" | tail -10
   ```

**Root cause:** Unknown — could be a new failure mode. Escalate if not resolved after restart.
**Fix:** `kubectl rollout restart deployment/<drainer> -n atlas` and monitor.

---

## Step 3 — Verify Impact

Regardless of scenario, check user impact:

```
prometheus: rate(driver_query_drain_latency_count[5m])
```

If drain rate is 0 for all pods → writes are fully stalled. Check how long by looking at drain rate over the last hour:
```
prometheus_query_range: rate(driver_query_drain_latency_count[5m]), start=now-1h, step=1m
```

---

## Alert: DriverDrainerLagIncreasing / CustomerDrainerLagIncreasing

**Trigger:** Drainer processing lag exceeds threshold — drainer is running but falling behind.

### Step 1 — Confirm lag

```
prometheus: (sum(increase(driver_query_drain_latency_sum[5m])) / sum(increase(driver_query_drain_latency_count[5m]))) / (1000*60*60)
```
For customer drainer:
```
prometheus: (sum(increase(query_drain_latency_sum[5m])) / sum(increase(query_drain_latency_count[5m]))) / (1000*60*60)
```

### Step 2 — Check drainer health

```bash
kubectl get pods -n atlas --no-headers | grep -i drainer
```

```bash
stern -n atlas drainer-service --since=30m --no-follow | grep -i 'ERROR' | head -50
```

### Step 3 — Check if DB is the bottleneck (most common cause of lag)

```bash
for i in provider-db-w3 driver-r1; do
  echo "=== $i ===" &&
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$i \
    --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
    --region ap-south-1 --output json |
  jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
done
```

For customer drainer, use: `customer-w1`, `app-db-r1`, `customer-r3`.

```bash
for i in provider-db-w3 driver-r1; do
  echo "=== $i ===" &&
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections \
    --dimensions Name=DBInstanceIdentifier,Value=$i \
    --start-time <start> --end-time <end> --period 60 --statistics Maximum \
    --region ap-south-1 --output json |
  jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Maximum) connections"'
done
```

### Step 4 — Check if pod count dropped

```bash
kubectl get hpa -n atlas | grep -i drainer
```

**Possible root causes:** DB overloaded (CPU > 80%), drainer pods scaled down by HPA, traffic spike, slow query from recent deployment.
**Fix:** If DB bottleneck → resolve DB issue. If pods reduced → scale up manually. If slow query → check Performance Insights for top queries.

---

## If Fast RCA Was Provided

If a Fast RCA result was injected at the top of this investigation:
1. **Don't repeat the same checks** — the fast RCA already ran pod_status, pod_logs, stop_metric, drain_rate, rds_cpu, pod_events, and redis_health.
2. **Go deeper** — if Fast RCA said "Scenario B: SQL error", find the exact table/column, search ES for the full stack trace, check if it's a known issue.
3. **Verify or refute** — if your evidence contradicts the Fast RCA, explain why in your final report.
4. **Add remediation detail** — Fast RCA gives a one-liner fix; your report should include the full remediation plan.
