# General Alert Investigation Runbook

## Goal
- **Primary Objective:** Deep investigation for alerts without a dedicated fast-RCA path. Find root cause with evidence, check all dependencies, compare against baseline, and quantify business impact.
- **Covers:** TrinetraTriggered, Multimodal5xx, Protocol5xxErrors, ProtocolExternalAPI5xx, AcmeGateway5xx, OpenNetworkGateway5xx, GRPCDown, GIMS5xx, CMRLAPIGivingErrors, CrisAPIGivingErrors, PTExternalAPIErrorsIncreased, Refunds increased, and any other alert not handled by a dedicated runbook.
- **Expected Outcome:** Service identified, root cause with evidence, baseline comparison, business impact, verified vs assumed findings.

## Time Window Instructions
- Use `startsAt` as anchor. Queries: `start = startsAt - 10 min`, `end = startsAt + 1 hour`.
- For stern/kubectl logs: calculate `--since` from startsAt.
- **ALWAYS compare with yesterday's same time window** for error counts and metrics.

## Infrastructure Reference
Refer to the **Site Knowledge Base** for:
- All service names, namespaces, pod patterns
- RDS instance IDs, Redis cluster IDs
- Elasticsearch indices: `protocol-lp-logs-YYYY-MM-DD` (app), `istio-proxy-YYYY.MM.DD` (access)
- Prometheus/VictoriaMetrics URL
- Service→DB/Redis mapping

## IMPORTANT: Tool Routing
- **App metrics (5xx, latency, request rate)**: Use `prometheus_query` / `prometheus_query_range`
- **AWS metrics (RDS, Redis, ALB)**: Use `aws cloudwatch get-metric-statistics` via bash
- **Application logs**: Use `elasticsearch_search`
- **Pod logs**: Use `stern` or `kubectl logs` via bash
- **SQL diagnostics**: Use `db_query` tool

---

## Phase 1 — Identify What's Broken (run all in parallel)

### 1a. Find the affected service and metric
Based on the alert name, query the relevant metric to confirm it's real:

**For 5xx/error alerts:**
```
prometheus: topk(10, sum(increase(http_request_duration_seconds_count{status_code=~"5[0-9]{2}"}[5m])) by (handler, service, status_code))
prometheus: topk(5, sum(increase(istio_requests_total{response_code=~"5..",reporter="source"}[5m])) by (destination_service_name, response_code))
```

**For job/cron failure alerts (TrinetraTriggered):**
```
prometheus: sum(increase(job_status_total{status!="OK", job_name!~"Custom cpu .*"}[5m])) by (job_name, status)
```

**For external API alerts (CMRL, CRIS, PT, OpenNetwork, Acme):**
Search pod logs for the specific external API errors (see Phase 2).

### 1b. Check pod health of the affected service
```bash
kubectl get pods -n atlas --no-headers | grep -i <service-name>
```

### 1c. Check for non-running pods across namespace
```bash
kubectl get pods -n atlas --no-headers | grep -vE 'Running|Completed' | head -15
```

### 1d. Check recent deployments
```bash
kubectl get events -n atlas --sort-by=.lastTimestamp | grep -iE "pulled|deploy|image|scaled" | tail -15
```

---

## Phase 2 — Get the Error Details

### 2a. Pod logs for errors
```bash
stern -n atlas <service-name> --since=30m --no-follow | grep -i 'ERROR' | head -30
```

### 2b. Categorize error types
```bash
stern -n atlas <service-name> --since=30m --no-follow | grep -i 'ERROR' | sed 's/.*ERROR.*|> //' | cut -d: -f1 | sort | uniq -c | sort -rn | head -15
```

### 2c. Search Elasticsearch for detailed error logs
```json
{
  "index": "protocol-lp-logs-<YYYY-MM-DD>",
  "size": 20,
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

### 2d. For 5xx alerts — get request IDs from istio access logs
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
**Note:** Verify the status code is after `HTTP/1.1"` — numbers in response time fields are false positives.

Then search app logs by request ID for the actual error.

---

## Phase 3 — Baseline Comparison (MANDATORY)

**This is the most important step.** Many errors are baseline noise. You MUST compare today's error count with yesterday.

### 3a. Error count comparison
Run the SAME ES query from Phase 2c on **yesterday's index** (`protocol-lp-logs-<yesterday-date>`), same time window. Compare:
- If today's error count is within 2x of yesterday → **baseline noise**, NOT a new issue
- If today's error count is > 3x yesterday → **new or significantly worse**, investigate further

### 3b. Metric comparison with yesterday
```
prometheus_query_range: <the-alert-metric>, start=<yesterday-same-time-30m>, end=<yesterday-same-time+1h>, step=1m
```
Compare the shape and magnitude. Same pattern = recurring/baseline.

### 3c. For external API alerts
Check if the external API was also erroring yesterday:
```json
{
  "index": "protocol-lp-logs-<yesterday-date>",
  "size": 5,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "<external-api-name>"}},
        {"match": {"message": "ERROR"}},
        {"range": {"@timestamp": {"gte": "<yesterday-same-start>", "lte": "<yesterday-same-end>"}}}
      ]
    }
  }
}
```

---

## Phase 4 — Check Dependencies

Based on the errors found in Phase 2, pivot to the relevant dependency:

### If errors mention Redis (timeout, CLUSTERDOWN, MOVED, connection refused)
```bash
for cluster in app-redis-cluster-001 location-redis utils-redis; do
  echo "=== $cluster ===" &&
  aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization \
    --dimensions Name=ReplicationGroupId,Value=$cluster \
    --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
    --region ap-south-1 --output json |
  jq -r '.Datapoints|sort_by(.Timestamp)[-3:][]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
done
```

### If errors mention DB (connection refused, too many connections, timeout, deadlock)
```bash
for i in provider-db-w3 driver-r1 customer-w1 app-db-r1; do
  echo "=== $i ===" &&
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$i \
    --start-time <start> --end-time <end> --period 60 --statistics Average Maximum \
    --region ap-south-1 --output json |
  jq -r '.Datapoints|sort_by(.Timestamp)[-3:][]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
done
```

### If errors mention external API (Acme, OpenNetwork, CMRL, CRIS, Google, OSRM)
- Determine: is the external API returning errors (their outage) or are we sending bad requests (our bug)?
- Check the HTTP status and response body in logs
- Check if same external API errors existed yesterday (Phase 3c)

### If pod crash (OOMKilled, CrashLoopBackOff)
```bash
kubectl describe pod -n atlas <pod-name> | grep -A10 "Last State\|OOMKilled\|Reason\|Exit Code"
kubectl logs -n atlas <pod-name> --previous --tail=50 2>/dev/null
```

---

## Phase 5 — Business Impact Assessment

**Always quantify impact** — even if the alert seems minor:

### 5a. 5xx error rate
```
prometheus: sum(increase(istio_requests_total{response_code=~"5..",reporter="source"}[5m])) by (destination_service_name)
```

### 5b. Ride-to-search ratio (overall platform health)
```
prometheus: rate(ride_created_count[5m]) / rate(search_request_count[5m])
```

### 5c. P99 latency
```
prometheus: histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))
```

### 5d. ALB 5xx (if applicable)
```bash
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name HTTPCode_Target_5XX_Count \
  --dimensions Name=LoadBalancer,Value=app/k8s-eksclusterist-4e612188d3/943d0588b4834c23 \
  --start-time <start> --end-time <end> --period 60 --statistics Sum \
  --region ap-south-1 --output json |
jq -r '.Datapoints|sort_by(.Timestamp)[-5:][]|"\(.Timestamp): \(.Sum) 5xx"'
```

---

## Phase 6 — Correlate Signals

### 6a. Timeline construction
Build a timeline of events:
- When did the alert fire? (startsAt)
- When did errors start? (first error in ES/logs)
- Was there a deployment? (Phase 1d)
- Was there a scaling event? (`kubectl get events -n atlas | grep -i scaled`)
- Was there a cron job? (check knowledge base for cron schedules at this time)
- Did a dependency degrade? (Phase 4)

### 6b. Scope assessment
- Is it one pod or all pods of the service?
- Is it one service or multiple services?
- If multiple services → shared dependency (DB, Redis, network)
- If one service + recent deploy → bad deployment

---

## Alert-Specific Investigation Notes

### TrinetraTriggered
- Find which job: `sum(increase(job_status_total{status!="OK"}[5m])) by (job_name, status)`
- Check CronJob status: `kubectl get jobs -n atlas | grep -i <job-name>`
- Common causes: external dependency timeout, data inconsistency, OOM on batch job

### Multimodal5xx
- Find which handler: `sum(increase(http_request_duration_seconds_count{handler=~".*multimodal.*",status_code=~"5.."}[5m])) by (handler)`
- Multimodal depends on multiple transit APIs — check which one is failing
- Common causes: CMRL/CRIS API outage, GTFS data stale, transit provider timeout

### Protocol5xxErrors / ProtocolExternalAPI5xx
- Same investigation as ALB 5xx but focused on protocol protocol endpoints
- Check: are inbound requests from external parties failing, or are our outbound calls failing?
- Check gateway service health: `kubectl get pods -n atlas | grep gateway`

### AcmeGateway5xx / OpenNetworkGateway5xx
- External dependency — check if it's their outage or our request issue
- Look for HTTP response body in logs — their error message tells the story
- Check if API credentials are valid/expired

### GRPCDown
- Check notification service pods: `kubectl get pods -n atlas | grep -i notif`
- Port 50051 (gRPC) — check if istio sidecar is interfering
- Common cause: pod restart during rolling update (self-resolves in 2-3 min)

### GIMS5xx (GTFS In-Memory Server)
- Memory-intensive service — check for OOMKilled: `kubectl describe pod -n atlas <gims-pod>`
- Check memory: `kubectl top pods -n atlas | grep -i nandi`
- Common cause: GTFS dataset grew, pod OOMed

### CMRLAPIGivingErrors / CrisAPIGivingErrors / PTExternalAPIErrorsIncreased
- External metro/railway API failures
- Determine: external outage (all requests failing) vs our code issue (after deploy)
- Check if same errors existed yesterday (usually they do — these APIs are flaky)

### Refunds increased
- Search logs for: refund, payment, cancel, FAILED
- Check if ride cancellation rate spiked
- Check payment gateway health (Acme)
- Common cause: payment gateway transient failures → auto-refund triggered

---

## RCA Report Requirements

Your final report MUST clearly distinguish between **verified facts** and **assumptions**:

### For every claim, state your evidence:
- **VERIFIED**: "Multimodal 5xx rate is 15/min on handler /v2/multimodalSearch — ES logs show CMRL API returning 503" — you have the data
- **VERIFIED (baseline)**: "Same CMRL 503 errors appear yesterday at 12/min vs today 15/min — this is within baseline range" — compared with data
- **LIKELY**: "CMRL API appears to be having an outage based on all requests failing simultaneously" — strong inference but not conclusive
- **UNVERIFIED**: "Did not check if CMRL has a status page or if other consumers are also affected"

### Structure your conclusion as:
1. **What happened** (facts: which service, which API, what error, timestamps)
2. **Is this new or baseline?** (today vs yesterday comparison — MANDATORY)
3. **Root cause** (verified with evidence, OR "likely cause" with reasoning)
4. **Is it our side or external?** (for external API alerts)
5. **Business impact** (verified: 5xx rate, ride-to-search ratio, latency. Or "not measured")
6. **What was NOT checked** (explicitly list)
7. **Recommended action** (with confidence level)

Never state a root cause without evidence. If you can't determine root cause with data, say "root cause undetermined — nearest hypothesis is X based on Y, but Z was not checked."

---

## Extended Investigation

If the above phases don't identify root cause:
- Correlate timestamps across ALL sources: metrics, logs, pod events, deploys
- Check if a cron job runs at this time (knowledge base has cron schedules)
- Check if traffic spiked: `sum(rate(http_request_duration_seconds_count[1m])) by (service)`
- Check node health: `kubectl get nodes`, `kubectl top nodes`
- Search ES broadly: just `ERROR` + time window, scan for patterns
- Use `db_query` for application data if needed (ride/booking status)
- Check if this alert fired before — `learnings_list` + `learnings_read`
