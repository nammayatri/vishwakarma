# ProducerNotProducing Investigation Runbook

## Goal
- **Primary Objective:** Determine if the driver job producer has actually stopped producing, or if this is a stale metric / false alarm. If genuinely down, find root cause.
- **Scope:** `protocol-driver-job-producer-service-production` in namespace `atlas`.
- **Covers:** ProducerNotProducing.
- **Expected Outcome:** Verified status (producing or not), root cause if down, with evidence.

## Context
The driver job producer creates jobs for the allocator service (ride matching). When it stops, new ride requests can't be matched to drivers. However, **this alert fires frequently as a false alarm** — the producer is often still working but the metric appears stale due to scrape timing.

**A Fast RCA (preliminary analysis) may already be injected above.** If present, it already checked pod status, producer metric, stream jobs counter, and pod logs. Don't repeat — verify and go deeper.

## Time Window Instructions
- Use the alert's `startsAt` as your investigation anchor.
- For all queries: `start = startsAt - 10 min`, `end = startsAt + 1 hour`
- Always state the time window in your findings.

---

## Step 1 — Verify if Producer is Actually Working (skip if Fast RCA provided)

Run in parallel:

**Producer metric (is it producing?):**
```
prometheus: sum(increase(producer_operation_duration_sum{operation="producer"}[10m]))
```
- If > 0 → producer IS working, this is likely a false alarm
- If = 0 → producer has genuinely stopped

**Per-pod breakdown:**
```
prometheus: sum(increase(producer_operation_duration_sum{operation="producer"}[10m])) by (pod)
```
- Check if all pods are producing or only some

**Stream jobs counter (is allocator consuming?):**
```
prometheus: sum(delta(stream_jobs_counter[5m]))
```
- If > 0 → jobs are being consumed, system is working
- If = 0 → either producer stopped creating OR allocator stopped consuming

**Failed jobs:**
```
prometheus: sum(increase(stream_jobs_failed_counter[10m]))
```

**Pod status:**
```bash
kubectl get pods -n atlas --no-headers | grep -i 'driver.*job.*producer'
```

**Pod events:**
```bash
kubectl get events -n atlas --sort-by=.lastTimestamp | grep -i producer | tail -10
```

---

## Step 2 — Determine Scenario

| Condition | Scenario |
|-----------|----------|
| Pods Running + producer_metric > 0 + stream_jobs > 0 | **A: False alarm / stale metric** |
| Pods CrashLoopBackOff or OOMKilled | **B: Pod crash** |
| Pods Running + producer_metric = 0 + errors in logs | **C: Producer stuck** |
| Pods Running + recent deploy + metric dropped | **D: Bad deployment** |
| Pods Running + producer_metric > 0 + stream_jobs = 0 | **E: Allocator down** |

---

## Step 3 — Deep Dive by Scenario

### Scenario A: False Alarm / Stale Metric (MOST COMMON)
1. **Confirm with data**: producer_metric > 0, stream_jobs > 0, all pods Running with 0 restarts
2. Check if the alert metric itself is stale:
   ```
   prometheus: producer_operation_duration_sum{operation="producer"}
   ```
   Look at the metric timestamps — if they're current, the alert threshold may be too sensitive.
3. **Conclusion**: Report as false alarm with evidence. State: "Producer is actively producing at rate X/10min across Y pods. Stream jobs count confirms Z jobs consumed in 5min."

### Scenario B: Pod Crash
1. Check which pod is crashing:
   ```bash
   kubectl get pods -n atlas --no-headers | grep -i 'driver.*job.*producer'
   ```
2. Get crash reason:
   ```bash
   kubectl describe pod -n atlas <crashing-pod> | grep -A10 "Last State\|OOMKilled\|Reason\|Exit Code"
   ```
3. Get last logs before crash:
   ```bash
   kubectl logs -n atlas <crashing-pod> --previous --tail=50 2>/dev/null
   ```
4. Check if it's OOM:
   ```bash
   kubectl get pod -n atlas <crashing-pod> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}'
   ```

### Scenario C: Producer Stuck (Running but Not Producing)
1. Get error logs:
   ```bash
   stern -n atlas protocol-driver-job-producer --since=30m --no-follow | grep -i 'ERROR' | head -50
   ```
2. Check for DB/Redis connectivity errors:
   ```bash
   stern -n atlas protocol-driver-job-producer --since=30m --no-follow | grep -iE 'connection|timeout|refused|redis|db|postgres' | head -30
   ```
3. If DB errors → check RDS health:
   ```bash
   for i in provider-db-w3 driver-r1; do
     echo "=== $i ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
       --dimensions Name=DBInstanceIdentifier,Value=$i \
       --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[-5:][]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
   done
   ```
4. Search ES for producer errors:
   ```json
   {
     "index": "protocol-lp-logs-<YYYY-MM-DD>",
     "size": 20,
     "query": {
       "bool": {
         "must": [
           {"match": {"message": "producer"}},
           {"match": {"message": "ERROR"}},
           {"range": {"@timestamp": {"gte": "<start>", "lte": "<end>"}}}
         ]
       }
     },
     "_source": ["message", "@timestamp"]
   }
   ```

### Scenario D: Bad Deployment
1. Check recent deploys:
   ```bash
   kubectl get events -n atlas --sort-by=.lastTimestamp | grep -iE "pulled|deploy|image" | grep -i producer | tail -10
   ```
2. Check if the image tag changed recently:
   ```bash
   kubectl get deployment -n atlas -o wide | grep producer
   ```
3. Correlate deploy time with when producer_metric dropped to 0

### Scenario E: Allocator Down (Producer Working but Jobs Not Consumed)
1. Check allocator pods:
   ```bash
   kubectl get pods -n atlas --no-headers | grep -i allocator
   ```
2. Check allocator metric:
   ```
   prometheus: sum(delta(stream_jobs_counter[5m]))
   prometheus: sum(increase(stream_jobs_failed_counter[10m]))
   ```
3. Check allocator logs:
   ```bash
   stern -n atlas protocol-driver-offer-allocator --since=15m --no-follow | grep -i 'ERROR' | head -30
   ```
4. **This is a different alert** — if the producer is fine but allocator is down, report that the ProducerNotProducing alert is misleading and the real issue is allocator.

---

## Step 4 — Verify Impact

**Is ride matching working?**
```
prometheus: rate(ride_created_count[5m]) / rate(search_request_count[5m])
```

**Are searches happening?**
```
prometheus: sum(rate(search_request_count[5m]))
```

If ride-to-search ratio is normal → no user impact, confirms false alarm.

---

## If Fast RCA Was Provided

If a Fast RCA result was injected at the top:
1. **Don't repeat metric checks** — fast RCA already checked pod_status, producer_metric, stream_jobs, and pod_logs.
2. **Focus on verification**: If Fast RCA says "false alarm", confirm by checking ride-to-search ratio and stream job consumption rate.
3. **If Fast RCA says pod crash**, get the full crash logs (`kubectl logs --previous`) and identify the exact error.
4. **Check what Fast RCA didn't**: ES logs, RDS health, recent deployments.

---

## RCA Report Requirements

Your final report MUST clearly distinguish between **verified facts** and **assumptions**:

- **VERIFIED**: "Producer metric is 67.4 (10min increase) across 5 pods — all actively producing" — you have the data
- **LIKELY**: "Alert fired due to a brief metric scrape gap — metric is now current" — reasonable inference
- **UNVERIFIED**: "No external dependency check done (Redis/DB)" — explicitly state what wasn't checked

### Structure:
1. **What happened** (facts: pod state, metric values, timestamps)
2. **Is the producer actually down?** (YES with evidence / NO with evidence)
3. **If down — why?** (verified cause or nearest hypothesis)
4. **If false alarm — why did the alert fire?** (metric gap, threshold, scrape timing)
5. **User impact** (verified: ride-to-search ratio, or "not checked")
