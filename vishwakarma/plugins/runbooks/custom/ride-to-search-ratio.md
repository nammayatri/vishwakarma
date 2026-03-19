# RideToSearchRatioDown Investigation Runbook

## Goal
Investigate why ride-to-search ratio dropped below 15% for a city. Determine:
1. Whether this is a real business issue or a metrics pipeline problem
2. Which cities are affected and how severely
3. Whether searches are up or rides are down (or both)
4. The specific component in the search-to-ride pipeline that is failing
5. Confidence level in the root cause
6. Whether an immediate fix is needed or it will self-resolve

**Agent Mandate:** Read-only. Do not modify any deployments, configs, or databases.

## Alert Details
- **Alert expression:** `(sum by (merchantOperatingCityId)(rate(ride_created_count[10m])) * 100) / (sum by (merchantOperatingCityId)(rate(search_request_count[10m]))) < 15`
- **Fires only during:** UTC hours 1-17 (IST 6:30 AM - 10:30 PM)
- **Minimum traffic filter:** search volume > 1500/10min (low-traffic cities excluded)
- **Breakdown dimension:** `merchantOperatingCityId`

## Time Window
- Use `startsAt` from the alert as your investigation anchor
- Query window: `startsAt - 30 minutes` to `startsAt + 30 minutes`
- Baseline comparison: same time window shifted back 24 hours (`startsAt - 24h`)
- If `startsAt` not available, use `now - 30 minutes`

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- ALB ARN (for CloudWatch 5xx queries)
- Elasticsearch endpoint + app log index name
- Namespace mappings (BAP/BPP)
- RDS/Redis instance identifiers
- merchantOperatingCityId to city name mapping (if available)

---

## IMPORTANT: Tool Routing
- **Business metrics** (search_request_count, ride_created_count, ratios): `prometheus_query_range`
- **API 5xx rates** (http_request_duration_seconds_count): `prometheus_query_range`
- **Istio mesh 5xx** (istio_requests_total): `prometheus_query_range`
- **External API health** (external_request_duration_count): `prometheus_query_range`
- **Allocator health** (stream_jobs_counter): `prometheus_query` or `prometheus_query_range`
- **ALB 5xx**: `aws cloudwatch get-metric-statistics` via bash
- **RDS metrics** (CPU, connections): `aws cloudwatch get-metric-statistics` via bash
- **ElastiCache/Redis metrics**: `aws cloudwatch get-metric-statistics` via bash
- **Pod health**: `kubectl` via bash
- **Application logs**: `elasticsearch_search`
- **Recent deploys**: `kubectl` via bash

---

## Step 0: Verify Metrics Are Flowing (MOST IMPORTANT — DO THIS FIRST)

**If metrics stopped flowing, the ratio calculation is meaningless. This is not a business issue, it is a metrics pipeline issue.**

Run these in parallel:

**0a — Check search metric is incrementing:**
- query: `sum(increase(search_request_count[5m]))`
- instant query at `startsAt`

**0b — Check ride metric is incrementing:**
- query: `sum(increase(ride_created_count[5m]))`
- instant query at `startsAt`

**0c — Check metric scrape targets are up:**
- query: `up{job=~".*protocol.*|.*driver.*"}`
- instant query at `startsAt`

**Interpret:**
- **Both metrics incrementing normally (search > 0, ride > 0)** -> REAL RATIO DROP, continue investigation
- **search_request_count increment is 0 or near 0** -> METRICS PIPELINE ISSUE on search side. Check Prometheus targets, pod health of the service exposing this metric.
- **ride_created_count increment is 0 or near 0** -> METRICS PIPELINE ISSUE on ride side. Same checks.
- **Both near 0** -> Either full outage or metrics pipeline is broken. Check pod health of ALL services + Prometheus itself.

**If this is a metrics pipeline issue, STOP HERE. Report it as a metrics pipeline problem, not a ratio problem.**

---

## Step 1: Identify Affected Cities and Quantify the Drop

Run these in parallel:

**1a — Current ratio by city:**
- query: `(sum by (merchantOperatingCityId)(rate(ride_created_count[10m])) * 100) / (sum by (merchantOperatingCityId)(rate(search_request_count[10m])))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `5m`

**1b — Search volume by city:**
- query: `sum by (merchantOperatingCityId)(rate(search_request_count[10m])) * 600`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `5m`
(multiply by 600 to get count per 10 minutes)

**1c — Ride volume by city:**
- query: `sum by (merchantOperatingCityId)(rate(ride_created_count[10m])) * 600`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `5m`

**1d — Yesterday's ratio at same time (MANDATORY BASELINE):**
- query: `(sum by (merchantOperatingCityId)(rate(ride_created_count[10m])) * 100) / (sum by (merchantOperatingCityId)(rate(search_request_count[10m])))`
- range query: `startsAt - 24h - 30m` to `startsAt - 24h + 30m`, step `5m`

**After Step 1, fill in:**
```
Affected cities:       <list of merchantOperatingCityIds with ratio < 15%>
Worst affected city:   <cityId> at <ratio>%
Yesterday same time:   <ratio>% for the same cities
Search volume trend:   INCREASING / STABLE / DECREASING
Ride volume trend:     INCREASING / STABLE / DECREASING
```

**Critical decision point:**
- If yesterday also showed similar ratio -> This may be NORMAL BEHAVIOR for this time/city. Note it and continue with lighter investigation.
- If ratio was 20%+ yesterday and is now <15% -> GENUINE DROP, investigate urgently.

---

## Step 2: Diagnose — Searches Up or Rides Down?

Based on Step 1 data, determine the pattern:

| Pattern | Search Trend | Ride Trend | Likely Cause |
|---------|-------------|------------|--------------|
| **Blocked pipeline** | Normal or up | Dropped | Ride creation is failing — allocator, 5xx on ride flow, external API |
| **Partial outage** | Both down, rides more | Both down | Something breaking both, but rides hit harder |
| **Traffic surge** | Spiked up | Stayed flat | Sudden demand spike without matching driver supply |
| **Full outage** | Both dropped equally | Both dropped equally | Major outage or traffic drop (external event) |
| **Supply crunch** | Normal | Dropped | Drivers not available — check driver-side services |

**Record which pattern you see — this guides which steps to prioritize.**

---

## Step 3: Check the Ride Creation Pipeline

Run these in parallel:

**3a — Allocator health (CRITICAL — allocator assigns drivers to rides):**
- query: `sum(increase(stream_jobs_counter[5m]))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `5m`
- If delta is 0 or near 0 -> ALLOCATOR IS DEAD. This is likely the root cause.

**3b — Search handler 5xx:**
- query: `sum by (handler)(rate(http_request_duration_seconds_count{handler=~".*search.*",status_code=~"^5.."}[5m]))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `1m`

**3c — Ride-related handler 5xx:**
- query: `sum by (handler)(rate(http_request_duration_seconds_count{handler=~".*ride.*|.*confirm.*|.*init.*|.*select.*",status_code=~"^5.."}[5m]))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `1m`

**3d — External API failures (OpenNetwork, Acme, Google Maps):**
- query: `sum by (service)(rate(external_request_duration_count{status_code=~"^5.."}[5m]))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `1m`

**3e — Driver availability (nearbyDrivers handler health):**
- query: `sum(rate(http_request_duration_seconds_count{handler=~".*nearbyDrivers.*|.*nearby.*driver.*"}[5m])) by (status_code)`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `1m`

**3f — Istio service mesh 5xx between services:**
- query: `sum by (destination_service, response_code)(rate(istio_requests_total{response_code=~"5.."}[5m]))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `1m`

---

## Step 4: Check for 5xx Errors — Multi-Source Correlation

Run these in parallel:

**4a — Overall 5xx rate from Prometheus:**
- query: `sum by (service, handler)(rate(http_request_duration_seconds_count{status_code=~"^5.."}[1m]))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `1m`

**4b — ALB 5xx from CloudWatch:**
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB \
  --metric-name HTTPCode_Target_5XX_Count \
  --dimensions Name=LoadBalancer,Value=<alb-arn-from-knowledge-base> \
  --start-time <startsAt-30min ISO8601> --end-time <startsAt+30min ISO8601> \
  --period 60 --statistics Sum --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Sum) 5xx"'
```

**4c — ALB response time:**
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB \
  --metric-name TargetResponseTime \
  --dimensions Name=LoadBalancer,Value=<alb-arn-from-knowledge-base> \
  --start-time <startsAt-30min ISO8601> --end-time <startsAt+30min ISO8601> \
  --period 60 --statistics Average p99 --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average)s"'
```

**4d — Elasticsearch search for application errors:**
Use `elasticsearch_search` tool:
```json
{
  "index": "<app-log-index-from-knowledge-base>",
  "size": 30,
  "sort": [{"@timestamp": "desc"}],
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "<startsAt-15min>", "lte": "<startsAt+15min>"}}},
        {"bool": {
          "should": [
            {"match": {"message": "500"}},
            {"match": {"message": "error"}},
            {"match": {"message": "timeout"}},
            {"match": {"message": "allocator"}},
            {"match": {"message": "ride creation failed"}},
            {"match": {"message": "search failed"}},
            {"match": {"message": "driver not found"}},
            {"match": {"message": "connection refused"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "_source": ["message", "@timestamp", "service", "level"]
}
```

---

## Step 5: Check Infrastructure Health

Run these in parallel:

**5a — RDS CPU:**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
  --dimensions Name=DBInstanceIdentifier,Value=<rds-instance-from-knowledge-base> \
  --start-time <startsAt-30min ISO8601> --end-time <startsAt+30min ISO8601> \
  --period 60 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
```

**5b — RDS connections:**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections \
  --dimensions Name=DBInstanceIdentifier,Value=<rds-instance-from-knowledge-base> \
  --start-time <startsAt-30min ISO8601> --end-time <startsAt+30min ISO8601> \
  --period 60 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor) max=\(.Maximum|floor)"'
```

**5c — Redis (ElastiCache) CPU:**
```
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name CPUUtilization \
  --dimensions Name=CacheClusterId,Value=<redis-cluster-from-knowledge-base> \
  --start-time <startsAt-30min ISO8601> --end-time <startsAt+30min ISO8601> \
  --period 60 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
```

**5d — Redis evictions and memory:**
```
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions \
  --dimensions Name=CacheClusterId,Value=<redis-cluster-from-knowledge-base> \
  --start-time <startsAt-30min ISO8601> --end-time <startsAt+30min ISO8601> \
  --period 60 --statistics Sum --region <region> --output json
```

**5e — Pod health (CrashLoopBackOff, OOMKilled, restarts):**
```
kubectl get pods -n <namespace> --sort-by='.status.containerStatuses[0].restartCount' -o wide | tail -20
```

**5f — Pod events (OOM, eviction, scheduling failures):**
```
kubectl get events -n <namespace> --sort-by='.lastTimestamp' --field-selector type=Warning | tail -30
```

**5g — Recent deploys:**
```
kubectl get replicasets -n <namespace> --sort-by=.metadata.creationTimestamp -o wide | tail -15
```

---

## Step 6: Baseline Comparison (MANDATORY)

**This step is NON-NEGOTIABLE. Every metric checked above must be compared with yesterday's same time.**

Run these in parallel (all use time window shifted back 24h):

**6a — Yesterday's ratio:**
Already done in Step 1d.

**6b — Yesterday's 5xx rate:**
- query: `sum(rate(http_request_duration_seconds_count{status_code=~"^5.."}[1m]))`
- range query: `startsAt - 24h - 30m` to `startsAt - 24h + 30m`, step `1m`

**6c — Yesterday's allocator health:**
- query: `sum(increase(stream_jobs_counter[5m]))`
- range query: `startsAt - 24h - 30m` to `startsAt - 24h + 30m`, step `5m`

**6d — Yesterday's external API errors:**
- query: `sum by (service)(rate(external_request_duration_count{status_code=~"^5.."}[5m]))`
- range query: `startsAt - 24h - 30m` to `startsAt - 24h + 30m`, step `1m`

**Comparison matrix (fill in):**
```
                         Today           Yesterday
Ratio (worst city):      <value>%        <value>%
Search volume:           <value>/10m     <value>/10m
Ride volume:             <value>/10m     <value>/10m
5xx rate:                <value>/min     <value>/min
Allocator jobs (5m):     <value>         <value>
External API 5xx:        <value>/min     <value>/min
```

If today's values are within 20% of yesterday -> This is likely **normal behavior**, not an incident.

---

## Step 7: External Factors Check

**7a — Recent deploy correlation:**
From Step 5g, check if any deployment happened within 30 minutes before `startsAt`. If yes, it's a strong candidate for root cause.

**7b — Google Maps API health:**
- query: `sum(rate(external_request_duration_count{service=~".*google.*|.*maps.*",status_code=~"^5.."}[5m]))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `1m`

**7c — OpenNetwork gateway health:**
- query: `sum(rate(external_request_duration_count{service=~".*opennetwork.*|.*protocol.*gateway.*",status_code=~"^5.."}[5m]))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `1m`

**7d — Acme payment gateway health:**
- query: `sum(rate(external_request_duration_count{service=~".*acme.*|.*payment.*",status_code=~"^5.."}[5m]))`
- range query: `startsAt - 30m` to `startsAt + 30m`, step `1m`

---

## Synthesis — Hypothesis Verification Matrix

**MANDATORY: Work through EVERY hypothesis below. For each one, state CONFIRMED / RULED OUT / INCONCLUSIVE with specific evidence.**

### Hypothesis 1: Metrics Pipeline Broken
**Check:** `increase(search_request_count[5m])` or `increase(ride_created_count[5m])` is 0 or near 0
**Verify:** Prometheus scrape targets are down, or the service exposing the metric has crashed
**Rule out:** If both metrics are incrementing at expected rates -> NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 2: Search 5xx — Searches Failing Before Rides Can Be Created
**Check:** `/protocol/:merchantId/search/` or similar search handlers returning 5xx
**Verify:** Search volume dropped AND 5xx count correlates with the drop
**Rule out:** If search handler 5xx rate is 0 or negligible -> NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 3: Allocator Dead — Rides Cannot Be Assigned to Drivers
**Check:** `stream_jobs_counter` stopped incrementing (delta = 0 over 5 minutes)
**Verify:** Allocator pods are in CrashLoopBackOff or not running, OR allocator logs show errors
**Rule out:** If `stream_jobs_counter` is incrementing normally -> NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 4: External API Failure (OpenNetwork/Acme/Google Maps)
**Check:** `external_request_duration_count` with `status_code=~"^5.."` shows spike for OpenNetwork, Acme, or Google Maps
**Verify:** The failing external API is in the critical path between search and ride creation
**Rule out:** If external API 5xx rates are 0 or at baseline levels -> NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 5: Driver Supply Issue — No Drivers Available
**Check:** Searches succeed but nearbyDrivers returns empty results or errors
**Verify:** Driver-side services (BPP) are healthy, driver location updates are flowing
**Rule out:** If nearbyDrivers handler is healthy and returning results -> NOT this
**Confidence if confirmed:** MEDIUM (may need driver-side data to confirm)

### Hypothesis 6: RDS/Redis Infrastructure Issue
**Check:** RDS CPU > 90% or connections maxed out, OR Redis evictions spiking or CPU > 80%
**Verify:** Application errors show "connection refused", "timeout", "too many connections"
**Rule out:** If RDS CPU < 70% and connections normal, Redis healthy -> NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 7: Bad Deploy — New Code Broke Ride Creation
**Check:** A deployment happened within 30 minutes before `startsAt`
**Verify:** The ratio drop correlates exactly with deploy timestamp, and the deployed service is in the ride creation path
**Rule out:** If no deploy happened in the last 2 hours -> NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 8: Google Maps API Failure — Distance/ETA Broken
**Check:** `external_request_duration_count` for Google Maps shows 5xx or timeouts
**Verify:** If Google Maps is down, distance/ETA calculation fails, blocking ride creation
**Rule out:** If Google Maps API 5xx rate is 0 -> NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 9: Traffic Pattern Change — Natural Low Period or External Event
**Check:** Both searches AND rides dropped proportionally, OR searches spiked unnaturally (e.g., bot traffic)
**Verify:** Compare with same day-of-week last week. Check if there's a known event (rain, holiday, strike).
**Rule out:** If the ratio change is asymmetric (rides dropped but searches didn't) -> NOT just traffic
**Confidence if confirmed:** MEDIUM

### Hypothesis 10: Normal Baseline — Ratio Was Similar Yesterday
**Check:** Yesterday at the same time, the ratio was also below or near 15% for the same cities
**Verify:** 7-day trend shows this is a recurring pattern for this time window
**Rule out:** If yesterday's ratio was 20%+ and today is <15% -> NOT normal
**Confidence if confirmed:** HIGH (no action needed, tune alert threshold)

---

## Final Verdict

After verifying all hypotheses, state:

```
## Verified Hypotheses
| # | Hypothesis | Verdict | Key Evidence |
|---|-----------|---------|--------------|
| 1 | Metrics pipeline broken | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 2 | Search 5xx | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 3 | Allocator dead | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 4 | External API failure (OpenNetwork/Acme/Maps) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 5 | Driver supply issue | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 6 | RDS/Redis infrastructure issue | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 7 | Bad deploy | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 8 | Google Maps failure | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 9 | Traffic pattern change | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 10 | Normal baseline | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |

## Root Cause
<Confirmed hypothesis with full evidence chain — include specific metric values, timestamps, and affected components>

## Confidence: HIGH / MEDIUM / LOW
<Why this confidence level — what evidence supports it, what's missing>

## Business Impact
Affected cities: <list with city IDs and current ratios>
Search volume: <current vs baseline>
Ride volume: <current vs baseline — this is the LOST RIDES number>
Estimated ride loss: <rides/hour that are not being created vs baseline>
User impact: <riders seeing "no drivers available" / searches timing out / rides not confirming>

## Immediate Fix
- If metrics pipeline broken: "Check Prometheus targets and restart metric-exporting pods"
- If search 5xx: "Identify failing search handler, check logs, rollback if deploy-related"
- If allocator dead: "Restart allocator pods: kubectl rollout restart deployment/<allocator-deployment> -n <ns>" — CRITICAL, rides cannot be created without allocator
- If external API failure: "OpenNetwork/Acme/Google Maps is down — external dependency, monitor for recovery. Enable fallback if available."
- If driver supply issue: "Check driver-side BPP services, driver app connectivity, location update pipeline"
- If RDS/Redis issue: "Follow RDS/Redis investigation runbook for detailed diagnosis"
- If bad deploy: "Rollback deployment: kubectl rollout undo deployment/<name> -n <ns>"
- If Google Maps failure: "External dependency — monitor for recovery. Check if cached distance/ETA can be used as fallback."
- If traffic pattern change: "No action needed — external event. Document for future reference."
- If normal baseline: "No action needed — tune alert threshold for this city/time window."

## Prevention
<What change prevents recurrence — be specific: alert threshold tuning, circuit breaker for external APIs, allocator health check, etc.>

## Needs More Investigation
YES / NO — <if YES, what specifically needs checking and by whom>
```

---

## Extended Investigation

If ALL hypotheses are INCONCLUSIVE after the above steps:
- Check the full ride creation flow end-to-end: search -> estimate -> select -> init -> confirm
- Query each handler in the flow for 5xx or latency spikes
- Check if the issue is city-specific (different infrastructure, different driver pool)
- Look at driver-side metrics: driver app heartbeats, location updates, offer acceptance rates
- Check Kafka/message queue health if ride events flow through a queue
- Correlate with any ongoing maintenance windows or cloud provider incidents
- Check `kubectl get events --all-namespaces --field-selector type=Warning` for cluster-wide issues
- Look at node-level metrics: node CPU, memory, disk pressure
