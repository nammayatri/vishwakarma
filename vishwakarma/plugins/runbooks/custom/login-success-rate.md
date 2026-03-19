# Login Success Rate Investigation Runbook

## Goal
Investigate LoginSuccessRate alert on BAP (`app-backend-production-pilot`). Determine:
1. Whether the success rate drop is real or metrics noise (low volume)
2. Where in the login flow it is breaking (OTP request, OTP delivery, OTP verify)
3. Whether the root cause is application, infrastructure, or external (SMS/WhatsApp provider)
4. Confidence level in the root cause
5. Whether an immediate fix is needed

**Agent Mandate:** Read-only. Do not modify any pods, configs, or database records.

## Alert Definition
```
Success Rate = (sum(rate(verify_2xx[5m])) / sum(rate(auth_total[5m]))) * 100
Threshold: < 10% for 15 minutes
Minimum volume: increase(auth_total[5m]) > 90
```
- Service: `app-backend-production-pilot` (BAP only — BPP has a separate auth flow)
- Handler (OTP request): `/v2/auth/`
- Handler (OTP verify): `/v2/auth/:authId/verify/`
- Handler (OTP resend): `/v2/auth/otp/:authId/resend/`
- 429 responses are excluded from the ratio (rate-limited bot traffic — baseline ~16/s on `/v2/auth/`)

## Login Flow
```
Customer → /v2/auth/ (200 = OTP sent) → [SMS/WhatsApp provider delivers OTP] → /v2/auth/:authId/verify/ (200 = login success)
```
Key: OTP delivery is external and NOT tracked in Prometheus. If `/v2/auth/` returns 200 but `/v2/auth/:authId/verify/` volume drops to near zero, the OTP provider is likely down. This can only be INFERRED — there is no `external_request_duration_count` for the OTP provider.

## Time Window
- Use `startsAt` from the alert as your investigation anchor
- Query window: `startsAt - 15 minutes` to `startsAt + 30 minutes`
- Day-over-day comparison: same window with `offset 1d`

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- Kubernetes namespace for BAP
- RDS instance identifiers (customer DB)
- ElastiCache cluster identifiers
- ALB ARN
- Elasticsearch endpoint + app log index name

---

## IMPORTANT: Tool Routing
- **Auth/verify metrics (success rate, volume, status codes)**: Use `prometheus_query_range` with `http_request_duration_seconds_count` — these ARE application metrics in Prometheus.
- **ALB metrics (5xx, response time)**: Use `aws cloudwatch get-metric-statistics` via bash.
- **RDS metrics (CPU, connections)**: Use `aws cloudwatch get-metric-statistics` via bash — RDS metrics are NOT in Prometheus.
- **ElastiCache/Redis metrics**: Use `aws cloudwatch get-metric-statistics` via bash.
- **Pod health, deploys, events**: Use `kubectl` via bash.
- **Application logs**: Use `elasticsearch_search`.

---

## Step 0: Is This Real or Metrics Noise?

Run all of these in parallel:

**0a — Current success rate (live value):**
- query: `(sum(rate(http_request_duration_seconds_count{status_code=~"2..",status_code!="429",handler="/v2/auth/:authId/verify/",service="app-backend-production-pilot"}[5m])) / sum(rate(http_request_duration_seconds_count{handler="/v2/auth/",status_code!="429",service="app-backend-production-pilot"}[5m]))) * 100`
- Use `prometheus_query` (instant, not range)

**0b — Auth request volume (is there enough traffic for the ratio to be meaningful?):**
- query: `sum(increase(http_request_duration_seconds_count{handler="/v2/auth/",status_code!="429",service="app-backend-production-pilot"}[5m]))`
- If result < 10 → LOW VOLUME, ratio is unreliable. Note this and proceed with caution.

**0c — Yesterday same-time success rate (baseline):**
- query: `(sum(rate(http_request_duration_seconds_count{status_code=~"2..",status_code!="429",handler="/v2/auth/:authId/verify/",service="app-backend-production-pilot"}[5m] offset 1d)) / sum(rate(http_request_duration_seconds_count{handler="/v2/auth/",status_code!="429",service="app-backend-production-pilot"}[5m] offset 1d))) * 100`

**0d — Success rate over the last hour (range query to see the trend):**
- query: same as 0a
- start: `<startsAt - 30m>`, end: `<now>`, step: `1m`

**Interpret:**
- **Current rate < 10% AND volume > 90** → GENUINE, ONGOING — investigate urgently
- **Current rate < 10% AND volume < 10** → LOW VOLUME NOISE — note "alert fired on low traffic, ratio unreliable" and do a lighter investigation
- **Current rate > 50%** → RESOLVED or TRANSIENT — still investigate but note self-recovery
- **Yesterday same-time rate was also low** → RECURRING PATTERN, not a new incident

**Include this assessment in your RCA under "Alert Assessment".**

---

## Step 1: Where in the Login Flow Is It Breaking?

Run all of these in parallel. This is the CRITICAL diagnostic step — the pattern of status codes tells you exactly what is failing.

**1a — Auth endpoint breakdown by status code (range):**
- query: `sum by(status_code)(rate(http_request_duration_seconds_count{handler="/v2/auth/",service="app-backend-production-pilot"}[1m]))`
- start: `<startsAt - 15m>`, end: `<startsAt + 30m>`, step: `1m`

**1b — Verify endpoint breakdown by status code (range):**
- query: `sum by(status_code)(rate(http_request_duration_seconds_count{handler="/v2/auth/:authId/verify/",service="app-backend-production-pilot"}[1m]))`
- start: `<startsAt - 15m>`, end: `<startsAt + 30m>`, step: `1m`

**1c — Resend endpoint volume (if users can't get OTP, they hit resend):**
- query: `sum by(status_code)(rate(http_request_duration_seconds_count{handler="/v2/auth/otp/:authId/resend/",service="app-backend-production-pilot"}[1m]))`
- start: `<startsAt - 15m>`, end: `<startsAt + 30m>`, step: `1m`

**1d — 429 rate on auth endpoint (bot traffic / rate limiting baseline is ~16/s):**
- query: `sum(rate(http_request_duration_seconds_count{handler="/v2/auth/",status_code="429",service="app-backend-production-pilot"}[1m]))`
- start: `<startsAt - 15m>`, end: `<startsAt + 30m>`, step: `1m`

**1e — P99 latency for auth and verify endpoints:**
- query: `histogram_quantile(0.99, sum by(le,handler)(rate(http_request_duration_seconds_bucket{handler=~"/v2/auth/.*",service="app-backend-production-pilot"}[5m])))`
- start: `<startsAt - 15m>`, end: `<startsAt + 30m>`, step: `1m`

**After Step 1 — Diagnosis Matrix:**

| Pattern | Meaning | Next Steps |
|---------|---------|------------|
| `/v2/auth/` returning 500s | OTP request itself failing — backend issue | → Steps 2, 3, 4 |
| `/v2/auth/` returning 200 normally, but `/v2/auth/:authId/verify/` volume near zero | OTP sent but never received by user → **OTP delivery failure** (SMS/WhatsApp provider down) | → Step 4 (ES logs for OTP send errors), escalate to OTP provider |
| `/v2/auth/:authId/verify/` returning 500s | Verify endpoint broken — backend issue | → Steps 2, 3, 4 |
| `/v2/auth/:authId/verify/` returning 400 spike | Users entering wrong OTP (unlikely systemic) or authId expired | → Step 4 (ES logs for specific error message) |
| 429 rate >> 16/s baseline | Bot attack or rate limit misconfigured | → Step 5 |
| `/v2/auth/otp/:authId/resend/` volume spiked | Confirms users are not receiving OTP (they keep retrying) | Supports OTP delivery failure hypothesis |
| Auth latency > 5s | Backend slow, requests timing out on client side | → Steps 2, 3 |
| Everything looks normal | Low traffic noise or alert is stale | Re-check Step 0 volume |

---

## Step 2: BAP Pod Health

Run all of these in parallel:

**2a — Pod status:**
```
kubectl get pods -n <namespace> -l app=app-backend-production-pilot -o wide
```

**2b — Recent pod restarts:**
```
kubectl get pods -n <namespace> -l app=app-backend-production-pilot -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[0].restartCount}{"\t"}{.status.containerStatuses[0].state}{"\n"}{end}'
```

**2c — Recent deploy (did code change recently?):**
```
kubectl get replicasets -n <namespace> -l app=app-backend-production-pilot --sort-by=.metadata.creationTimestamp -o wide | tail -10
```

**2d — Kubernetes events for BAP pods:**
```
kubectl get events -n <namespace> --field-selector involvedObject.kind=Pod --sort-by=.lastTimestamp | grep -i app-backend | tail -20
```

**2e — Recent HPA activity (autoscaling):**
```
kubectl get hpa -n <namespace> | grep app-backend
```

---

## Step 3: Infrastructure Health

Run all of these in parallel:

**3a — Customer RDS CPU and connections:**
```
for metric in CPUUtilization DatabaseConnections; do
  echo "=== $metric ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name $metric \
    --dimensions Name=DBInstanceIdentifier,Value=<customer-rds-instance-from-knowledge-base> \
    --start-time <startsAt-15min ISO8601> --end-time <startsAt+30min ISO8601> \
    --period 60 --statistics Average Maximum --region <region> --output json \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average | floor) max=\(.Maximum | floor)"'
done
```

**3b — ElastiCache/Redis CPU and memory:**
```
for metric in CPUUtilization DatabaseMemoryUsagePercentage CurrConnections Evictions; do
  echo "=== $metric ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name $metric \
    --dimensions Name=CacheClusterId,Value=<redis-cluster-from-knowledge-base> \
    --start-time <startsAt-15min ISO8601> --end-time <startsAt+30min ISO8601> \
    --period 60 --statistics Average Maximum --region <region> --output json \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average | floor) max=\(.Maximum | floor)"'
done
```

**3c — ALB 5xx and response time:**
```
for metric in HTTPCode_Target_5XX_Count TargetResponseTime; do
  echo "=== $metric ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name $metric \
    --dimensions Name=LoadBalancer,Value=<alb-arn-from-knowledge-base> \
    --start-time <startsAt-15min ISO8601> --end-time <startsAt+30min ISO8601> \
    --period 60 --statistics Sum Average --region <region> --output json \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): sum=\(.Sum // "N/A") avg=\(.Average // "N/A")"'
done
```

---

## Step 4: Application Logs (Elasticsearch)

Run all of these in parallel:

**4a — Auth/verify error logs:**
Use `elasticsearch_search` tool:
```json
{
  "index": "<app-log-index-from-knowledge-base>",
  "size": 30,
  "sort": [{"@timestamp": "desc"}],
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "<startsAt-15min>", "lte": "<startsAt+30min>"}}},
        {"term": {"service": "app-backend-production-pilot"}},
        {"bool": {
          "should": [
            {"match_phrase": {"message": "auth"}},
            {"match_phrase": {"message": "verify"}},
            {"match_phrase": {"message": "OTP"}},
            {"match_phrase": {"message": "login"}},
            {"match_phrase": {"message": "SMS"}},
            {"match_phrase": {"message": "token"}}
          ],
          "minimum_should_match": 1
        }}
      ],
      "filter": [
        {"terms": {"level": ["error", "ERROR", "fatal", "FATAL"]}}
      ]
    }
  },
  "_source": ["message", "@timestamp", "level"]
}
```

**4b — OTP provider errors (look for external call failures):**
Use `elasticsearch_search` tool:
```json
{
  "index": "<app-log-index-from-knowledge-base>",
  "size": 20,
  "sort": [{"@timestamp": "desc"}],
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "<startsAt-15min>", "lte": "<startsAt+30min>"}}},
        {"term": {"service": "app-backend-production-pilot"}},
        {"bool": {
          "should": [
            {"match_phrase": {"message": "sms"}},
            {"match_phrase": {"message": "whatsapp"}},
            {"match_phrase": {"message": "msg91"}},
            {"match_phrase": {"message": "kaleyra"}},
            {"match_phrase": {"message": "gupshup"}},
            {"match_phrase": {"message": "twilio"}},
            {"match_phrase": {"message": "otp provider"}},
            {"match_phrase": {"message": "external request"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "_source": ["message", "@timestamp", "level"]
}
```

**4c — Compare with yesterday (are these errors NEW or pre-existing?):**
Run the same query as 4a but with time range shifted by 1 day. If the same errors appear yesterday → pre-existing, NOT caused by this incident.

**4d — 500-status handler logs (if Step 1 showed 500s):**
Use `elasticsearch_search` tool — search for HTTP 500 responses specifically on auth/verify handlers.

---

## Step 5: Rate Limit / Bot Attack Analysis

**Only run this if Step 1d showed 429 rate significantly above 16/s baseline.**

**5a — 429 rate trend over 6 hours:**
- query: `sum(rate(http_request_duration_seconds_count{handler="/v2/auth/",status_code="429",service="app-backend-production-pilot"}[5m]))`
- start: `<startsAt - 3h>`, end: `<startsAt + 30m>`, step: `5m`

**5b — Check if 429 spike is correlated with the success rate drop:**
If 429 spiked at the same time success rate dropped, and non-429 auth volume also dropped → rate limiter may be incorrectly blocking legitimate users.

**5c — Auth requests by source IP (if available in logs):**
Use `elasticsearch_search` to look for top source IPs hitting `/v2/auth/` — if a few IPs dominate, it's bot traffic.

---

## Synthesis — Hypothesis Verification Matrix

**MANDATORY: Work through EVERY hypothesis below. For each one, state CONFIRMED / RULED OUT / INCONCLUSIVE with specific evidence.**

### Hypothesis 1: OTP Request Failing
**Check:** `/v2/auth/` returning 500 status codes (Step 1a)
**Verify:** 500 rate > 0.01/s at the time success rate dropped + ES logs show auth endpoint errors (Step 4a)
**Rule out:** If `/v2/auth/` has zero 500s and 200 rate is normal → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 2: OTP Delivery Failure (SMS/WhatsApp Provider Down)
**Check:** `/v2/auth/` returning 200 at normal rate BUT `/v2/auth/:authId/verify/` volume dropped to near zero (Step 1a vs 1b)
**Verify:** Resend endpoint (`/v2/auth/otp/:authId/resend/`) volume spiked (users retrying) + ES logs show OTP send errors or external call failures (Step 4b)
**Rule out:** If verify volume is proportional to auth volume → OTP is being delivered, NOT this
**Note:** There is NO direct metric for OTP delivery. This can only be INFERRED from the gap between auth 200s and verify attempts.
**Confidence if confirmed:** MEDIUM (inference-based, no direct telemetry)

### Hypothesis 3: Verify Endpoint Broken
**Check:** `/v2/auth/:authId/verify/` returning 500 status codes (Step 1b)
**Verify:** 500 rate > 0.01/s + ES logs show verify endpoint errors (Step 4a)
**Rule out:** If verify has zero 500s → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 4: Database Issue (Customer DB)
**Check:** RDS CPU > 80% or connections exhausted (Step 3a) + auth/verify endpoints returning 500 or timing out
**Verify:** Auth flow writes/reads tokens from customer DB. If DB is overwhelmed, auth tokens can't be created or validated.
**Rule out:** If RDS CPU < 50% and connections normal → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 5: Redis Issue (Session/Token Cache)
**Check:** ElastiCache CPU > 80% or evictions > 0 or memory > 90% (Step 3b)
**Verify:** Auth tokens may be cached in Redis. If Redis is down, token validation fails.
**Rule out:** If Redis metrics are normal → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 6: BAP Pod Crash / Restart
**Check:** Pods in CrashLoopBackOff or high restart count (Step 2a, 2b)
**Verify:** Pod restart timestamps correlate with success rate drop + ES logs show app crash errors
**Rule out:** If all pods are Running with 0 recent restarts → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 7: Bad Deploy
**Check:** New ReplicaSet created within 30 minutes of the success rate drop (Step 2c)
**Verify:** Success rate drop started exactly when new pods rolled out + old version was working fine
**Rule out:** If no deploy happened in the last 2 hours → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 8: Rate Limit Attack / Misconfiguration
**Check:** 429 rate on `/v2/auth/` significantly above 16/s baseline (Step 1d, Step 5)
**Verify:** If rate limiter is blocking legitimate users along with bots → auth 200 rate drops → fewer OTPs sent → fewer verifications
**Rule out:** If 429 rate is near baseline (~16/s) → NOT this
**Confidence if confirmed:** MEDIUM (need to confirm legitimate users are affected)

### Hypothesis 9: Low Traffic Noise
**Check:** Auth volume < 10 requests in 5 minutes (Step 0b)
**Verify:** At very low traffic, even 1-2 failed verifications cause the ratio to plummet. Compare with yesterday same-time volume.
**Rule out:** If volume > 90 (minimum threshold in alert) → NOT this
**Confidence if confirmed:** HIGH (no action needed — alert threshold needs a higher minimum volume)

### Hypothesis 10: Normal Baseline (Yesterday Was Similar)
**Check:** Yesterday same-time success rate was also low (Step 0c)
**Verify:** If yesterday's rate was similar and there was no incident → this may be a recurring low-traffic period
**Rule out:** If yesterday success rate was > 90% → this IS anomalous, NOT normal
**Confidence if confirmed:** HIGH (no action needed)

---

## Final Verdict

After verifying all hypotheses, state:

```
## Verified Hypotheses
| # | Hypothesis | Verdict | Key Evidence |
|---|-----------|---------|--------------|
| 1 | OTP request failing (/v2/auth/ 500s) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 2 | OTP delivery failure (provider down) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 3 | Verify endpoint broken (/v2/auth/:authId/verify/ 500s) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 4 | Database issue (customer DB) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 5 | Redis issue (session cache) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 6 | BAP pod crash/restart | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 7 | Bad deploy | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 8 | Rate limit attack/misconfiguration | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 9 | Low traffic noise | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 10 | Normal baseline | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |

## Root Cause
<Confirmed hypothesis with full evidence chain>

## Confidence: HIGH / MEDIUM / LOW
<Why this confidence level — what evidence supports it, what's missing>

## Business Impact
Login success rate: <current rate vs normal ~99%>
Users unable to log in: YES / NO — <estimated number based on auth volume>
Rides affected: YES / NO — users who can't log in can't book rides
Revenue impact: <if login is down, new bookings from logged-out users are blocked>

## Immediate Fix
- If OTP request failing (500): Check app logs for the specific error. If DB-related → fix DB first. If code bug → rollback deploy.
- If OTP delivery failure: Escalate to SMS/WhatsApp provider immediately. Check if there's a fallback provider configured. This is EXTERNAL — app-side fix is limited.
- If verify endpoint broken: Check app logs for the specific error. If auth token lookup is failing → check DB/Redis.
- If database issue: Check customer RDS for connection exhaustion, CPU spike, or locks. May need DBA intervention.
- If Redis issue: Check for evictions, memory pressure, or connectivity loss. Restart Redis client connections in app if needed.
- If pod crash: "kubectl rollout restart deployment/<bap-deployment-name> -n <namespace>" or rollback if new deploy caused it.
- If bad deploy: "kubectl rollout undo deployment/<bap-deployment-name> -n <namespace>"
- If rate limit attack: Tighten rate limit rules or block offending IPs at WAF/ALB level.
- If low traffic noise: No action needed — adjust alert minimum volume threshold.
- If normal baseline: No action needed — review alert threshold.

## Prevention
<What change prevents recurrence — be specific>

## Needs More Investigation
YES / NO — <if YES, what specifically needs checking and by whom>
- If OTP delivery failure confirmed: Need OTP provider dashboard access or API status check (not available via current tooling)
- If inconclusive: Request adding external_request_duration_count metrics for OTP provider calls to enable direct monitoring
```

---

## Extended Investigation

If ALL hypotheses are INCONCLUSIVE after the above steps:
- Check if there's a regional SMS outage (search news/status pages for the OTP provider)
- Check if the customer DB has schema changes or migration running
- Check if Redis key TTLs changed (auth tokens expiring faster than expected)
- Correlate timestamps across ALL sources: metrics, logs, pod events, deploys
- Check upstream dependencies — is the OTP provider API returning errors that the app is swallowing silently?
- Look at `/v2/auth/:authId/verify/` 400 responses in detail — are they "OTP expired" (timeout issue) or "invalid OTP" (wrong code)?
- Check if there's a network policy or security group change blocking outbound SMS/WhatsApp API calls
