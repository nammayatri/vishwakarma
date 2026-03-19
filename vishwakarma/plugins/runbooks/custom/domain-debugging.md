# Domain-Specific Deep Debugging Runbook

## Goal
Deep investigation of application-level issues. Trace through **data → logs → metrics → code** in that order. Only go to source code after you have concrete findings (error messages, anomalous states, specific function names) from the data and logs.

## When This Applies
- User reports a specific ride/booking/payment issue with an ID
- User asks "why did this happen" for a domain-level issue
- Debugging requires understanding the application logic, not just infra health

## Prerequisites
- `database` toolset enabled (ClickHouse + PostgreSQL connections)
- `learnings_read(database)` — ALWAYS do this first for schema + ID resolution chains

## Investigation Order
**DO NOT jump to code first.** Follow this order:
1. **Database** — find the entity, trace the chain, identify the anomaly
2. **Logs** — find the error message, exception, or failure reason
3. **Metrics** — check if infra was degraded at that time (DB, Redis, external APIs)
4. **Code** — ONLY after you have a specific error/function/state to investigate

---

## Phase 1 — Database Investigation

### 1a. Load schema knowledge
```
learnings_read(database)
```

### 1b. Resolve the entity
Follow the ID resolution chain from learnings. Given any UUID, resolve to the full ride/booking/payment chain. Fetch the FULL row — every column matters.

### 1c. Trace the complete chain
For a ride issue, fetch ALL of:
- BPP ride (driver side) — status, trip_start/end, fare, driver_id, booking_id
- BPP booking — status, from/to locations, vehicle_variant, provider_id, quote_id
- BAP booking (rider side) — status, payment_method, bpp_ride_booking_id
- BAP ride — rider-side ride status, bpp_ride_id
- Payment — `payment_order` by booking_id
- Fare details — `fare_parameters` by ID
- Driver — person record (active, enabled, mode)
- Rider — person record

### 1d. Find the anomaly
- Is `status` unexpected? (e.g., booking CONFIRMED but ride NEW for > 30 min)
- Are timestamps out of order?
- Are amounts wrong? (fare vs fare_parameters mismatch)
- Is data missing? (driver_id NULL on assigned ride)
- Is there a cascade? (payment failed → booking stuck → ride not started)

### 1e. Check if pattern or one-off
```sql
SELECT status, count(*) FROM provider_db.ride
WHERE merchant_operating_city_id = '<same_city>'
AND created_at >= '<1h_ago>' GROUP BY status ORDER BY count DESC
```

---

## Phase 2 — Log Investigation

### 2a. Search Elasticsearch for errors around the incident time
```json
{
  "index": "protocol-lp-logs-<YYYY-MM-DD>",
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "<entity-id-from-phase-1>"}},
        {"range": {"@timestamp": {"gte": "<created_at - 5min>", "lte": "<created_at + 30min>"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

### 2b. Search by request ID if available
If the DB row has a `request_id` or you found one in logs:
```json
{
  "index": "protocol-lp-logs-<YYYY-MM-DD>",
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "<request-id>"}},
        {"range": {"@timestamp": {"gte": "<time - 5min>", "lte": "<time + 5min>"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

### 2c. Search for ERROR logs from the relevant service
```json
{
  "index": "protocol-lp-logs-<YYYY-MM-DD>",
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "ERROR"}},
        {"match": {"message": "<service-name>"}},
        {"range": {"@timestamp": {"gte": "<incident-time - 10min>", "lte": "<incident-time + 30min>"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

### 2d. Check pod logs if ES doesn't have what you need
```bash
stern -n atlas <service-name> --since=30m --no-follow | grep -i '<error-keyword-from-db-or-es>' | head -20
```

### 2e. Extract the key finding
From logs, identify:
- The exact error message / exception
- The function or module that threw it
- The dependency that failed (Redis? DB? External API?)
- The specific condition that triggered the failure

---

## Phase 3 — Metrics Check (if logs point to infra)

Only if logs show dependency errors (Redis timeout, DB connection refused, external API 5xx):

### Redis health
```bash
for node in location-redis-001 app-redis-cluster-001-0001-001; do
  echo "=== $node ===" &&
  aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization \
    --dimensions Name=CacheClusterId,Value=$node \
    --start-time <incident-time-10m> --end-time <incident-time+30m> --period 60 --statistics Average Maximum \
    --region ap-south-1 --output json |
  jq -r '.Datapoints|sort_by(.Timestamp)[-3:][]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
done
```

### RDS health
```bash
for i in provider-db-w3 driver-r1 customer-w1 app-db-r1; do
  echo "=== $i ===" &&
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$i \
    --start-time <incident-time-10m> --end-time <incident-time+30m> --period 60 --statistics Average Maximum \
    --region ap-south-1 --output json |
  jq -r '.Datapoints|sort_by(.Timestamp)[-3:][]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
done
```

### 5xx rate at that time
```
prometheus_query_range: sum(increase(istio_requests_total{response_code=~"5..",reporter="source"}[1m])) by (destination_service_name), start=<incident-time-10m>, end=<incident-time+30m>, step=1m
```

---

## Phase 4 — Code Investigation (ONLY after Phases 1-3)

**Only enter this phase when you have:**
- A specific error message or exception name from logs
- A specific function name from a stack trace
- A specific state transition that doesn't make sense
- A question like "why does the code do X when Y happens"

### 4a. Pull latest code
Source repos are on PV. Pull latest before reading:

**Main ExampleApp backend (Haskell):**
```bash
cd /data/example-app-src && git pull 2>&1 | head -5
```

**If you need shared-kernel or euler-hs (for KV, Redis, DB layer code):**
```bash
# Clone only if not already present — these are needed for low-level DB/Redis/KV code
if [ ! -d /data/shared-kernel-src ]; then
  git clone --depth 1 https://github.com/your-org/shared-kernel.git /data/shared-kernel-src
else
  cd /data/shared-kernel-src && git pull 2>&1 | head -5
fi

if [ ! -d /data/euler-hs-src ]; then
  git clone --depth 1 https://github.com/example-app/euler-hs.git /data/euler-hs-src
else
  cd /data/euler-hs-src && git pull 2>&1 | head -5
fi
```

### 4b. Read architecture docs first
```bash
cat /data/example-app-src/.cursor/docs/06-ride-flow.md
```
This maps every ride phase to exact source files. Other docs:
```bash
cat /data/example-app-src/.cursor/docs/01-architecture-overview.md
cat /data/example-app-src/.cursor/docs/03-rider-app.md
cat /data/example-app-src/.cursor/docs/04-driver-app.md
cat /data/example-app-src/.cursor/docs/05-protocol-protocol-flow.md
```

### 4c. Search for the specific error/function from your findings
```bash
# Search for the exact error message from logs
grep -r "<error-message-keyword>" /data/example-app-src/Backend --include="*.hs" -l | head -10

# Search for a specific function name
grep -r "<function-name>" /data/example-app-src/Backend --include="*.hs" -l | head -10

# Search for the DB table/type involved
grep -r "<TableName>\|<TypeName>" /data/example-app-src/Backend --include="*.hs" -l | head -10
```

**Key repo paths:**
| Path | Contains |
|------|----------|
| `/data/example-app-src/Backend/rider-platform/rider-app/Main/src/` | BAP (rider) app logic |
| `/data/example-app-src/Backend/provider-platform/dynamic-offer-driver-app/Main/src/` | BPP (driver) app logic |
| `/data/example-app-src/Backend/provider-platform/dynamic-offer-driver-app/Main/src/SharedLogic/Allocator/` | Ride matching / allocation |
| `/data/example-app-src/Backend/lib/` | Shared libraries |
| `/data/shared-kernel-src/lib/mobility-core/src/` | KV queries, Redis, DB layer |
| `/data/euler-hs-src/src/EulerHS/KVConnector/` | KV connector implementation |

### 4d. Read the source code
```bash
cat /data/example-app-src/Backend/<path-to-file>
```

**Focus on:**
- The function that handles the state transition you found anomalous in Phase 1
- Error handling — does it catch the error or swallow it?
- Conditional logic — what conditions lead to the state you observed?
- External calls — which dependency does it call that may have failed?

### 4e. Check recent changes (if you suspect regression)
```bash
cd /data/example-app-src && git log --oneline -10 -- <path-to-file>
```
```bash
cd /data/example-app-src && git show <commit-hash> -- <path-to-file> | head -100
```

---

## Phase 5 — Root Cause Synthesis

Combine ALL findings:

### Data says:
- What state the entity is in (from DB)
- When it reached that state (timestamps)
- What's anomalous (compared to normal cases)

### Logs say:
- The exact error that occurred
- Which service/function threw it
- Which dependency failed

### Metrics say:
- Whether infra was degraded at that time (or not)

### Code says (if investigated):
- What conditions lead to that state
- What error handling exists (or doesn't)
- Whether a recent code change could have caused it

### Root cause:
Connect all the dots — "Ride abc123 is stuck in NEW status because the `assignDriver` function in `Allocation.hs` received Nothing from Redis location lookup (driver location stale > 5 min). The code logs the error but doesn't retry. location-redis CPU was at 51% at the time (normal). This is a code-level issue — no retry logic for stale location data."

---

## Phase 6 — Scope and Impact

### How many affected?
```sql
SELECT count(*) FROM provider_db.ride
WHERE status = '<anomalous_status>' AND created_at >= '<relevant_timeframe>'
```

### When did it start?
```sql
SELECT min(created_at) FROM provider_db.ride
WHERE status = '<anomalous_status>' AND created_at >= '<yesterday>'
```

### Is it still happening?
Check the latest few entries — if the anomaly continues, it's an ongoing issue.

---

## Common Investigation Patterns

### "Why did this ride get cancelled?"
1. DB: ride status, ride_ended_by, booking_cancellation_reason
2. Logs: search for ride_id in ES → find cancellation event
3. Code: only if cancellation reason is unclear from data

### "Why was the fare wrong?"
1. DB: ride.fare vs fare_parameters breakdown vs fare_breakup
2. DB: chargeableDistance vs traveledDistance, deviation flags
3. Code: only if fare calculation logic needs understanding → `SharedLogic/FareCalculator.hs`

### "Why is driver not getting rides?"
1. DB: driver person.active, person.enabled, person.mode
2. DB: driver_information — subscription, vehicle
3. DB: recent search_requests for this driver
4. Logs: search for driver_id in ES
5. Code: only if pool logic needs understanding → `SharedLogic/Allocator/`

### "Why did payment fail?"
1. DB: payment_order status, payment_transaction gateway response
2. Logs: search for booking_id + "payment" in ES
3. Metrics: only if gateway 5xx suspected
4. Code: only if payment flow logic needs understanding

### "Why did search return no results?"
1. DB: BAP search_request → BPP search_request (via transaction_id)
2. DB: driver_quote — were quotes generated?
3. Logs: search for search_request_id in ES
4. Code: only if search/pool logic needs understanding

---

## Report Requirements

### Structure:
1. **Data findings** — exact values from DB with table.column references
2. **Log findings** — error messages, timestamps, service names
3. **Metrics findings** — infra health at incident time (if checked)
4. **Code findings** — function, logic path, condition (if code was investigated)
5. **Root cause** — connecting data + logs + code
6. **Scope** — one entity or pattern? How many affected?
7. **Recommended action** — code fix, data fix, config change, or "no action needed"

### Evidence labels:
- **VERIFIED (data)**: "ride.status = NEW, created 45 min ago — stuck" (DB query result)
- **VERIFIED (logs)**: "ES shows 'Redis timeout on location lookup' at 14:32 UTC for this ride_id"
- **VERIFIED (code)**: "assignDriver at Allocation.hs:142 doesn't retry on Nothing"
- **INFERRED**: "Driver location was likely stale based on location-redis latency at that time"
- **UNVERIFIED**: "Did not check shared-kernel KV layer code"
