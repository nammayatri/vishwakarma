# ALB 5xx Investigation Runbook

## Goal
- **Primary Objective:** When an ALB 5xx alert fires, identify which service and API endpoint is returning 5xx, find the actual error message, trace the full root cause chain, and provide actionable remediation.
- **Scope:** AWS ALB for EKS cluster, all backend services in namespace `atlas`.
- **Covers:** ALB5xxErrors, HTTPCode_Target_5XX_Count, HTTPCode_ELB_5XX_Count, HTTP_ELB_CODE_5XX, Protocol API ALB 5xx.
- **Expected Outcome:** Service → API → Error message → Root cause → Why it happened → Fix.

## Context
ALB 5xx alerts fire when backend services return HTTP 500/502/503/504 errors. The investigation must answer the **Five Whys**: not just "which API returned 500" but "why did that API return 500, what dependency failed, and what triggered that failure."

**A Fast RCA (preliminary analysis) may already be injected above.** If present, it already identified the top failing service/API from Prometheus metrics and may include request IDs and error messages from ES logs. **Don't repeat those checks.** Instead, use the Fast RCA findings as your starting point and go deeper — find the full stack trace, check the dependency that caused the error, trace it to the root trigger (deployment, traffic spike, DB issue, etc).

## Time Window Instructions
- Use the alert's `startsAt` as your investigation anchor.
- For all queries: `start = startsAt - 10 min`, `end = startsAt + 1 hour`
- For stern/kubectl logs: calculate `--since` from startsAt.
- Always state the time window in your findings.

## Infrastructure Reference
Refer to the **Site Knowledge Base** for:
- ALB ARN suffix
- Elasticsearch endpoint + index names (istio-proxy-YYYY.MM.DD, protocol-lp-logs-YYYY-MM-DD)
- RDS instance identifiers and DbiResourceIds
- Redis cluster names
- Service→RDS mapping (BAP→customer cluster, BPP→driver cluster)
- Service ports and HPA ranges

---

## Step 1 — Identify the Failing Service (parallel, skip if Fast RCA provided this)

Run these in parallel:

**Istio-level 5xx by service:**
```
prometheus: topk(5, sum(increase(istio_requests_total{response_code=~"5..",reporter="source"}[5m])) by (destination_service_name, response_code))
```

**API-level 5xx by handler:**
```
prometheus: topk(10, sum(increase(http_request_duration_seconds_count{status_code=~"5[0-9]{2}"}[5m])) by (handler, service, status_code))
```

**ALB CloudWatch — target 5xx count + response time:**
```bash
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name HTTPCode_Target_5XX_Count \
  --dimensions Name=LoadBalancer,Value=<alb-arn-suffix-from-knowledge-base> \
  --start-time <start> --end-time <end> --period 60 --statistics Sum --region ap-south-1 --output json |
jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Sum) 5xx"'
```
```bash
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name TargetResponseTime \
  --dimensions Name=LoadBalancer,Value=<alb-arn-suffix-from-knowledge-base> \
  --start-time <start> --end-time <end> --period 60 --statistics Average --region ap-south-1 --output json |
jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average)s"'
```

From these, determine:
- **Which service** is the top 5xx contributor
- **Which API handler** is failing
- **500 (app crash, low latency) vs 504 (timeout, high latency) vs 502/503 (pod down)?**

---

## Step 2 — Get Request IDs and Error Messages from ES Logs

**Search istio-proxy for 5xx responses:**
```json
{
  "index": "istio-proxy-<YYYY.MM.DD>",
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"log": "HTTP"}},
        {"range": {"@timestamp": {"gte": "<start>", "lte": "<end>"}}}
      ],
      "should": [
        {"match": {"log": "\" 500 "}},
        {"match": {"log": "\" 502 "}},
        {"match": {"log": "\" 503 "}},
        {"match": {"log": "\" 504 "}}
      ],
      "minimum_should_match": 1
    }
  },
  "_source": ["log", "@timestamp"]
}
```

**IMPORTANT:** ES match query returns false positives — numbers 500/503/504 appear in response time fields too. The real status code comes after `HTTP/1.1"` in the log line: `"METHOD /path HTTP/1.1" STATUS_CODE ...`. Verify each result.

Extract 3-5 **request IDs** (UUIDs) from confirmed 5xx lines.

**Search app logs by request ID for the actual error:**
```json
{
  "index": "protocol-lp-logs-<YYYY-MM-DD>",
  "size": 10,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "<request-id>"}},
        {"range": {"@timestamp": {"gte": "<start>", "lte": "<end>"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

**Also search for ERROR logs from the failing service broadly:**
```json
{
  "index": "protocol-lp-logs-<YYYY-MM-DD>",
  "size": 30,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "ERROR"}},
        {"range": {"@timestamp": {"gte": "<start>", "lte": "<end>"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

The `message` field contains JSON — look inside for the actual error: exception type, stack trace, dependency errors.

---

## Step 3 — Determine Scenario

| Condition | Scenario |
|-----------|----------|
| Low 5xx (< 20/min), matches 7-day baseline | **A: Noise** |
| High target_5xx + response_time > 3s + 504 | **B: Timeout** |
| High target_5xx + response_time < 1s + one handler | **C: App bug** |
| High elb_5xx + pods down/CrashLoop | **D: Pods down** |
| Error logs show Redis timeout/CLUSTERDOWN | **E: Redis** |
| Error logs show DB connection refused/timeout | **F: Database** |
| 5xx across multiple services simultaneously | **G: Shared dependency** |

---

## Step 4 — Deep Dive by Scenario

### Scenario A: Baseline Noise
1. **7-day baseline comparison** (must do this to confirm):
   ```bash
   aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name HTTPCode_Target_5XX_Count \
     --dimensions Name=LoadBalancer,Value=<alb-arn> \
     --start-time <7-days-ago> --end-time <now> --period 3600 --statistics Sum --region ap-south-1
   ```
2. If today matches average → report as baseline noise with the data.
3. If today is 2x+ higher than average → it's NOT noise, re-classify.

### Scenario B: Timeout (504)
1. Identify the slow upstream from istio logs (`outbound|...|service` field)
2. Check P99 latency of the failing service:
   ```
   prometheus: histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service="<failing-service>"}[5m])) by (le))
   ```
3. **Check if DB is the bottleneck** — this is the most common cause of timeouts:
   - RDS CPU for the relevant cluster (use knowledge base for service→RDS mapping)
   - RDS connections count
   - Performance Insights for top SQL queries:
     ```bash
     aws pi describe-dimension-keys --service-type RDS --identifier <DbiResourceId> \
       --start-time <start> --end-time <end> --metric db.load.avg \
       --group-by '{"Group":"db.sql_tokenized","Limit":5}' --region ap-south-1
     ```
   - PI wait events (IO vs CPU vs lock):
     ```bash
     aws pi describe-dimension-keys --service-type RDS --identifier <DbiResourceId> \
       --start-time <start> --end-time <end> --metric db.load.avg \
       --group-by '{"Group":"db.wait_event","Limit":5}' --region ap-south-1
     ```
4. **Check Redis latency** if the service uses Redis
5. **Check if external API is slow** — search logs for external call timeouts (acme, OpenNetwork, Google)

### Scenario C: Application Bug (500)
1. You have the handler + error from Steps 1-2. Now find the **full stack trace**:
   - Search ES with the exact error message/exception class
   - Check if this error existed yesterday (same handler, previous day's index) — new vs existing bug
2. **Check recent deployments**:
   ```bash
   kubectl get events -n atlas --sort-by=.lastTimestamp | grep -iE "pulled|deploy|image" | tail -15
   ```
   ```bash
   kubectl get replicasets -n atlas --sort-by=.metadata.creationTimestamp -o wide | tail -10
   ```
3. **Correlate deploy time with 5xx start** — if a new image was deployed within 30min of alert → likely a bad deployment
4. Check if the error is in one pod or all pods:
   ```bash
   stern -n atlas <failing-service> --since=30m --no-follow | grep -i 'ERROR' | head -50
   ```

### Scenario D: Pods Down
1. List non-running pods:
   ```bash
   kubectl get pods -n atlas --no-headers | grep -vE 'Running|Completed' | head -15
   ```
2. For each CrashLoopBackOff/OOMKilled pod:
   ```bash
   kubectl describe pod -n atlas <pod-name> | grep -A10 "Last State\|OOMKilled\|Reason\|Exit Code\|Events"
   ```
3. Check if HPA scaled down too aggressively:
   ```bash
   kubectl get hpa -n atlas | grep -i <failing-service>
   ```
4. Check node health:
   ```bash
   kubectl get nodes | head -10
   kubectl top nodes 2>/dev/null | head -10
   ```

### Scenario E: Redis Issue
1. Check **all** Redis clusters (the service might use any of them):
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
2. Check memory + evictions + connections for the hot cluster:
   ```bash
   for metric in DatabaseMemoryUsagePercentage Evictions CurrConnections; do
     echo "=== $metric ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name $metric \
       --dimensions Name=ReplicationGroupId,Value=<hot-cluster> \
       --start-time <start> --end-time <end> --period 60 --statistics Average Maximum Sum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Average // .Maximum // .Sum)"'
   done
   ```
3. Check bandwidth saturation:
   ```bash
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name NetworkBandwidthOutAllowanceExceeded \
     --dimensions Name=CacheClusterId,Value=<cluster-node-id> \
     --start-time <start> --end-time <end> --period 60 --statistics Sum --region ap-south-1
   ```

### Scenario F: Database Issue
1. Check RDS CPU + connections for the relevant cluster:
   ```bash
   for i in <writer> <reader1> <reader2>; do
     echo "=== $i ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
       --dimensions Name=DBInstanceIdentifier,Value=$i \
       --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
   done
   ```
2. Check connections:
   ```bash
   for i in <writer> <reader1>; do
     echo "=== $i ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections \
       --dimensions Name=DBInstanceIdentifier,Value=$i \
       --start-time <start> --end-time <end> --period 60 --statistics Maximum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Maximum) connections"'
   done
   ```
3. **Performance Insights** for the hot instance (identifies the exact slow query):
   ```bash
   aws pi describe-dimension-keys --service-type RDS --identifier <DbiResourceId> \
     --start-time <start> --end-time <end> --metric db.load.avg \
     --group-by '{"Group":"db.sql_tokenized","Limit":10}' --region ap-south-1
   ```
4. Check wait events (IO wait vs CPU vs lock contention):
   ```bash
   aws pi describe-dimension-keys --service-type RDS --identifier <DbiResourceId> \
     --start-time <start> --end-time <end> --metric db.load.avg \
     --group-by '{"Group":"db.wait_event","Limit":10}' --region ap-south-1
   ```
5. Check if a recent deployment introduced a bad query:
   - Correlate deploy timestamp with CPU spike start
   - Check if the slow query from PI matches a new handler

### Scenario G: Shared Dependency Failure
1. Check **all** RDS instances simultaneously (both BAP + BPP):
   ```bash
   for i in provider-db-w3 driver-r1 customer-w1 app-db-r1 customer-r3; do
     echo "=== $i ===" &&
     aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
       --dimensions Name=DBInstanceIdentifier,Value=$i \
       --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
       --region ap-south-1 --output json |
     jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
   done
   ```
2. Check all Redis clusters (see Scenario E)
3. Check node health and network:
   ```bash
   kubectl get nodes | head -10
   kubectl get pods -n atlas --no-headers | grep -vE 'Running|Completed'
   ```
4. Check if there's a common pattern in the failing services (all depend on same DB? same Redis?)

---

## Step 5 — Correlate with Recent Changes

Always check this — many 5xx spikes are caused by deployments:

```bash
kubectl get events -n atlas --sort-by=.lastTimestamp | grep -iE "pulled|deploy|image|scaled" | tail -20
```

```bash
kubectl get replicasets -n atlas --sort-by=.metadata.creationTimestamp -o wide 2>/dev/null | tail -15
```

If a deployment happened within 30 min of the alert → check if that service is the one returning 5xx.

---

## Step 6 — Verify User Impact

Check business-level metrics to quantify impact:

```
prometheus: rate(ride_created_count[5m]) / rate(search_request_count[5m])
```

```
prometheus: histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))
```

Report: "X% of requests were affected" or "ride-to-search ratio dropped from Y to Z".

---

## If Fast RCA Was Provided

If a Fast RCA result was injected at the top:
1. **Don't repeat Phase 1 checks** — fast RCA already ran CloudWatch metrics, Prometheus 5xx breakdown, ES istio logs, ES app logs, and pod health.
2. **Start from the identified service/handler** — jump straight to the scenario that matches.
3. **Go deeper than fast RCA can:**
   - Full stack trace from ES (fast RCA only got snippets)
   - Performance Insights for slow queries (fast RCA doesn't run PI)
   - 7-day baseline comparison
   - Deploy correlation
   - Business impact metrics
4. **Verify or refute** — if your evidence contradicts the Fast RCA, explain why with data.

---

## RCA Report Requirements

Your final report MUST clearly distinguish between **verified facts** and **assumptions**:

### For every claim, state your evidence:
- **VERIFIED**: "provider-app /search handler returned 500 — ES log shows NullPointerException at SearchHandler.java:142" — you have the log/metric data
- **LIKELY**: "This was triggered by the 14:20 UTC deployment (new ReplicaSet created 2 min before first 5xx)" — strong correlation but not conclusive
- **UNVERIFIED ASSUMPTION**: "Redis may be involved — did not check Redis metrics" — inferring without data

### Structure your conclusion as:
1. **What happened** (facts only — which service, which API, what error, timestamps)
2. **Why it happened** (verified root cause with evidence, OR "likely cause" with reasoning)
3. **What was NOT checked** (explicitly list things you couldn't verify)
4. **Impact** (verified: 5xx count, affected endpoints, business metrics. Or "impact not measured")
5. **Recommended fix** (with confidence level)

Never state a root cause without evidence. If you can't determine the root cause with data, say "root cause undetermined — nearest hypothesis is X based on Y, but Z was not checked."

---

## Extended Investigation

If the above steps don't identify root cause, use your judgment:
- Correlate timestamps across all signals — metrics spike, log errors, pod restarts, deployments
- Check upstream/downstream dependencies — the failing service may be a victim, not the cause
- Is it one pod or all? One namespace or cluster-wide?
- Check cron jobs — some run on schedules and cause CPU spikes (see knowledge base for cron schedules)
- Search ES broadly: `"ERROR"` + time window, look for patterns
- Check if it's an external dependency failure (Acme, OpenNetwork, Google Maps)
- Use `db_query` tool if you need to check application data (ride counts, payment states)
