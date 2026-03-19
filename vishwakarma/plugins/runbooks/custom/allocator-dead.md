# AllocatorLooksDead Investigation Runbook

## Goal
- **Primary Objective:** Determine if the allocator (ride matching) has actually stopped, or if this is a stale metric. If genuinely down, find root cause and quantify business impact.
- **Scope:** `protocol-driver-offer-allocator-service-production` in namespace `atlas`. Depends on: location-redis (location data), driver RDS, producer service (job creation).
- **Covers:** AllocatorLooksDead.
- **Expected Outcome:** Verified status, root cause with evidence, business impact (rides affected), and comparison against baseline to distinguish real issues from noise.

## Context
The allocator matches ride requests to nearby drivers. When it stops, no new rides can be matched — **this is a high-business-impact alert**. The alert fires when `sum(delta(stream_jobs_counter[2m])) == 0`.

However, this alert can also fire as a false alarm if the metric scrape has a gap or the counter temporarily stalls.

**A Fast RCA may already be injected above.** If present, it checked pod status, stream_jobs, ride_created rate, and pod logs. Don't repeat — verify and go deeper.

## Time Window Instructions
- Use `startsAt` as anchor. Queries: `start = startsAt - 10 min`, `end = startsAt + 1 hour`.
- **CRITICAL: Always compare against yesterday's same time window** to distinguish real issues from baseline noise.

---

## Step 1 — Verify if Allocator is Actually Working (skip if Fast RCA provided)

Run in parallel:

**Stream jobs counter (is allocator consuming?):**
```
prometheus: sum(delta(stream_jobs_counter[2m]))
```
- \> 0 → allocator IS consuming jobs, likely false alarm
- = 0 → allocator has stopped

**Failed jobs:**
```
prometheus: sum(increase(stream_jobs_failed_counter[10m]))
```

**Pod status:**
```bash
kubectl get pods -n atlas --no-headers | grep -i allocator
```

**Pod logs (errors only):**
```bash
stern -n atlas protocol-driver-offer-allocator --since=15m --no-follow | grep -i 'ERROR' | head -20
```

---

## Step 2 — Business Impact Check (ALWAYS DO THIS — high-impact alert)

**Ride creation rate (are rides being matched?):**
```
prometheus_query_range: rate(ride_created_count[5m]), start=<startsAt-30m>, end=<startsAt+1h>, step=1m
```

**Search request rate:**
```
prometheus_query_range: rate(search_request_count[5m]), start=<startsAt-30m>, end=<startsAt+1h>, step=1m
```

**Ride-to-search ratio:**
```
prometheus: rate(ride_created_count[5m]) / rate(search_request_count[5m])
```

**BASELINE COMPARISON (MANDATORY):**
Compare current ride_created rate with yesterday's same time:
```
prometheus_query_range: rate(ride_created_count[5m]), start=<yesterday-same-time-30m>, end=<yesterday-same-time+1h>, step=1m
```

If ride_created today is within 20% of yesterday → business is fine, allocator is working.
If ride_created dropped > 50% vs yesterday → real impact, investigate urgently.

---

## Step 3 — Determine Scenario

| Condition | Scenario |
|-----------|----------|
| Pods Running + stream_jobs > 0 + rides being created | **A: False alarm** |
| Pods CrashLoopBackOff or OOMKilled | **B: Pod crash** |
| Pods Running + stream_jobs = 0 + errors in logs | **C: Stuck (dependency failure)** |
| Pods Running + stream_jobs = 0 + no producer jobs | **D: Producer down** |
| Pods Running + stream_jobs low + failures high | **E: Partial failure** |

---

## Step 4 — Deep Dive by Scenario

### Scenario A: False Alarm (MOST COMMON)
1. Confirm: stream_jobs > 0, ride_created > 0, all pods Running
2. Compare stream_jobs rate with yesterday — if similar, this is normal
3. Check if alert metric had a scrape gap:
   ```
   prometheus_query_range: sum(delta(stream_jobs_counter[2m])), start=<startsAt-30m>, end=<startsAt+30m>, step=1m
   ```
   A single 0-value data point in an otherwise healthy series → scrape gap, not a real outage.
4. Report as false alarm with evidence: stream_jobs count, ride creation rate, comparison with yesterday.

### Scenario B: Pod Crash
1. Which pod is crashing:
   ```bash
   kubectl describe pod -n atlas <crashing-pod> | grep -A10 "Last State\|OOMKilled\|Reason\|Exit Code"
   ```
2. Last logs before crash:
   ```bash
   kubectl logs -n atlas <crashing-pod> --previous --tail=50 2>/dev/null
   ```
3. Check if it's OOM — allocator uses location-redis heavily, memory issues possible
4. Check if recent deployment:
   ```bash
   kubectl get events -n atlas --sort-by=.lastTimestamp | grep -iE "pulled|deploy|image" | grep -i allocator | tail -5
   ```

### Scenario C: Stuck (Dependency Failure)
The allocator depends on: **location-redis** (driver locations), **driver RDS** (driver data), **Kafka** (job queue).

1. Check pod logs for the specific dependency error:
   ```bash
   stern -n atlas protocol-driver-offer-allocator --since=30m --no-follow | grep -iE 'redis|CLUSTERDOWN|MOVED|timeout|connection refused|kafka|db|postgres' | head -30
   ```

2. **If Redis errors** → check location-redis health:
   ```bash
   for node in location-redis-001 location-redis-002 location-redis-003 location-redis-004; do
     echo "=== $node ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization \
       --dimensions Name=CacheClusterId,Value=$node \
       --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[-3:][]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
   done
   ```

3. **If DB errors** → check driver RDS:
   ```bash
   for i in provider-db-w3 driver-r1; do
     echo "=== $i ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
       --dimensions Name=DBInstanceIdentifier,Value=$i \
       --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[-3:][]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
   done
   ```

4. **If Kafka errors** → check Kafka connectivity from logs (Kafka disconnects are visible in pod logs as `rdkafka` errors)

5. Search ES for allocator errors and **compare with yesterday**:
   ```json
   {
     "index": "protocol-lp-logs-<YYYY-MM-DD>",
     "size": 20,
     "query": {
       "bool": {
         "must": [
           {"match": {"message": "allocator"}},
           {"match": {"message": "ERROR"}},
           {"range": {"@timestamp": {"gte": "<start>", "lte": "<end>"}}}
         ]
       }
     },
     "_source": ["message", "@timestamp"]
   }
   ```
   **Then run the SAME query on yesterday's index** (`protocol-lp-logs-<yesterday>`). If same errors exist yesterday → these are baseline noise, NOT the cause.

### Scenario D: Producer Down
1. Check if producer is creating jobs:
   ```
   prometheus: sum(increase(producer_operation_duration_sum{operation="producer"}[10m]))
   ```
   If = 0 → producer stopped, not allocator. Switch to ProducerNotProducing investigation.
2. Check producer pods:
   ```bash
   kubectl get pods -n atlas --no-headers | grep -i 'driver.*job.*producer'
   ```

### Scenario E: Partial Failure
1. Check which jobs are failing:
   ```bash
   stern -n atlas protocol-driver-offer-allocator --since=30m --no-follow | grep -iE 'failed|exception|error' | head -30
   ```
2. Check if one pod is bad or all:
   ```
   prometheus: sum(delta(stream_jobs_counter[2m])) by (pod)
   ```
3. Common partial failure patterns:
   - SECONDARY_CLUSTER errors: normal, multi-cloud fallback
   - FCM/OSRM 400 errors: notification/routing failures, not ride matching failures
   - Filter these baseline errors and look for NEW error patterns

---

## Step 5 — Error Baseline Comparison (ALWAYS DO THIS)

**This is critical.** Many allocator errors (SECONDARY_CLUSTER, FCM 400, OSRM NoMatch) are **baseline noise** that appear every day. You must compare today's errors with yesterday's to find what's NEW.

1. Count error types in current window:
   ```bash
   stern -n atlas protocol-driver-offer-allocator --since=30m --no-follow | grep -i 'ERROR' | sed 's/.*ERROR.*|> //' | cut -d: -f1 | sort | uniq -c | sort -rn | head -15
   ```

2. **If possible**, check yesterday's ES logs for the same error patterns. Only errors that are NEW or significantly increased (>3x yesterday's rate) are relevant to this incident.

---

## Step 6 — Correlate with Other Signals

Check if this is part of a broader issue:

**5xx error rate:**
```
prometheus: sum(increase(istio_requests_total{response_code=~"5..",reporter="source",destination_service_name="protocol-driver-offer-allocator-service-production"}[5m]))
```

**Recent deploys:**
```bash
kubectl get events -n atlas --sort-by=.lastTimestamp | grep -iE "pulled|deploy|image" | tail -15
```

**Other services affected?**
```
prometheus: sum(increase(istio_requests_total{response_code=~"5..",reporter="source"}[5m])) by (destination_service_name)
```

---

## If Fast RCA Was Provided

1. **Don't repeat metric checks** — fast RCA already checked pod_status, stream_jobs, stream_jobs_failed, ride_created, search_requests, and pod logs.
2. **Start from the scenario** fast RCA identified and go deeper.
3. **Always do the baseline comparison** — fast RCA can't compare with yesterday. This is the key deep-RCA value-add.
4. **Check dependencies** that fast RCA didn't check: location-redis, driver RDS, Kafka connectivity.

---

## RCA Report Requirements

Your final report MUST clearly distinguish between **verified facts** and **assumptions**:

- **VERIFIED**: "stream_jobs_counter delta = 671 in 2m, ride_created rate = 3.7/s — allocator is actively matching rides" — data proves it
- **VERIFIED (baseline)**: "SECONDARY_CLUSTER errors appear at the same rate as yesterday (15/min vs 14/min yesterday) — these are baseline noise, not the cause"
- **LIKELY**: "Alert fired due to metric scrape gap at 14:32 — stream_jobs shows a single zero point surrounded by healthy values"
- **UNVERIFIED**: "Did not check Kafka health or location-redis latency"

### Structure:
1. **Is the allocator actually dead?** (YES/NO with evidence)
2. **Business impact** (ride_created rate today vs yesterday, search-to-ride ratio)
3. **If dead — what caused it?** (dependency failure, pod crash, producer down)
4. **Error analysis** — which errors are NEW vs baseline noise (with yesterday comparison)
5. **Recommended action**
