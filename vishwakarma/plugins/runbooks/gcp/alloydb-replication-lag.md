# AlloyDB Replication Lag Runbook (GCP)

## Goal
Investigate AlloyDB replication lag alerts on AlloyDB for PostgreSQL (region `asia-south1`). Determine:
1. **Which type of replication** is lagging — logical replication slots (cross-cloud, e.g. AWS→GCP / GCP→AWS) vs AlloyDB read-pool replica lag
2. Which specific instance/slot is affected
3. Root cause (stale slot, subscriber failure, write surge, WAL growth)
4. Whether user-facing impact exists
5. Confidence level in the root cause
6. Whether immediate action is needed (stale slot dropping requires DBA)

**Agent Mandate:** Read-only. Do not drop replication slots, kill queries, or modify any DB/AlloyDB settings.

## CRITICAL: Two Different Types of Replication Lag

This cluster uses TWO completely separate replication mechanisms. **Do not confuse them.**

| | Logical Replication Slots (cross-cloud) | AlloyDB Read-Pool Replica Lag |
|---|---|---|
| **What** | PostgreSQL logical replication slots sending WAL to a cross-cloud subscriber | AlloyDB's internal storage-level replication to READ_POOL instances |
| **Metric** | `pg_replication_slots` lag (on **PRIMARY**) | `replicas_byte_lag` (on **READ_POOL** instances) |
| **Unit** | **SECONDS** (when measured via `pg_last_xact_replay_timestamp`) / **BYTES** (slot lag) | **BYTES** (`replicas_byte_lag`) |
| **Normal range** | 0-300 seconds (stale slots can show 100+ days = 8,640,000+ seconds) | <few MB byte lag (sub-second) |
| **Alarming range** | >3600 seconds (1 hour) | >100 MB byte lag / >1s replay delay |
| **Cause of lag** | Cross-cloud subscriber disconnected/crashed, slot not dropped | Heavy writes on primary, long-running queries on read pool |
| **Fix** | Drop stale slot (DBA), fix subscriber | Kill long query on read pool, reduce primary load |
| **Where to check** | PRIMARY instance | READ_POOL instances |

## CRITICAL: Unit Conversions — Get This Right

| Metric | Raw Unit | To convert |
|---|---|---|
| `now() - pg_last_xact_replay_timestamp()` (replay delay) | **INTERVAL/SECONDS** | already human-readable; for seconds use `EXTRACT(EPOCH FROM ...)` |
| `pg_wal_lsn_diff(...)` slot lag | **BYTES** | ÷ 1048576 = MB, ÷ 1073741824 = GB |
| `replicas_byte_lag` (Prometheus mirror) | **BYTES** | ÷ 1048576 = MB, ÷ 1073741824 = GB |
| AlloyDB storage usage | **BYTES** | ÷ 1048576 = MB, ÷ 1073741824 = GB |

**WARNING:** Byte-lag metrics return BYTES, not MB. A value of 16,984 means 16,984 bytes (~16 KB), NOT 16,984 MB. Always convert explicitly. The SQL replication checks below are the **high-confidence backbone** — prefer them over Prometheus-mirrored AlloyDB metrics when both are available.

## Time Window
- Use `startsAt` from the alert as your investigation anchor
- Query window: `startsAt - 30 minutes` to `startsAt + 1 hour`
- For logical replication issues, also check a **7-day trend** — stale slots degrade gradually
- If `startsAt` not available, use `now - 30 minutes`

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- AlloyDB cluster identifiers (driver cluster, customer cluster) — use the value from the Site Knowledge Base
- Alert name → cluster mapping
- Elasticsearch endpoint + app log index name
- Business-critical Prometheus metrics
- AlloyDB region is `asia-south1`

---

## IMPORTANT: Tool Routing
- **AlloyDB metric trends (replication byte lag, WAL, CPU, write throughput, storage)**: Use the `prometheus_query_range` tool. GCP infra metrics are mirrored into Prometheus for the GCP executor. **Do NOT use `gcloud monitoring time-series` — that subcommand does not exist.** Use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`).
- **Instance discovery**: Use `gcloud alloydb instances list` to find ALL instances including read pools — NEVER rely on hardcoded instance lists.
- **Replication slot diagnostics**: Use `db_query` for `pg_replication_slots` and `pg_stat_replication`. **This is the high-confidence backbone of the investigation — prefer it over metric trends.**
- **Business impact (5xx, latency)**: Use `prometheus_query_range` — these ARE application metrics in Prometheus.
- **Application logs**: Use `elasticsearch_search`.

---

## Step 0: Alert Freshness Check + Replication Type Identification

Before investigating, determine: (a) is this real, (b) which replication type is involved.

**0a — Identify affected instance from alert:**
Extract the instance identifier from the alert. Determine if it is a **PRIMARY** or **READ_POOL** instance — this tells you which replication type to focus on:
- PRIMARY instance → likely **logical replication slot** issue (cross-cloud subscriber)
- READ_POOL instance → likely **read-pool replica lag** issue (`replicas_byte_lag`)

**0b — Discover all instances and their roles:**
```
gcloud alloydb instances list --cluster=<cluster-id> --region=asia-south1 \
  --format="table(name, instanceType, state)"
```
List only read pools:
```
gcloud alloydb instances list --cluster=<cluster-id> --region=asia-south1 \
  --filter="instanceType=READ_POOL" --format="table(name, instanceType, state)"
```
Describe a specific instance:
```
gcloud alloydb instances describe <instance> --cluster=<cluster-id> --region=asia-south1
```

**0c — Check current logical replication slot lag on the PRIMARY (direct SQL — HIGH confidence):**
```
db_query(<primary>, "SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag_size, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS lag_bytes FROM pg_replication_slots ORDER BY lag_bytes DESC NULLS LAST")
```

**0d — Check current read-pool replica lag (direct SQL on each READ_POOL — HIGH confidence):**
```
db_query(<replica>, "SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay, pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()")
```
Prometheus-mirror equivalent (byte lag) for cross-checking — use `prometheus_query_range`:
- query: `<alloydb_replicas_byte_lag_metric>` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 10m>`, end: `<now>`, step: `1m`

**Interpret:**
- **Slot lag replay delay > 8,640,000 seconds (100+ days)** → STALE LOGICAL REPLICATION SLOT — proceed to Steps 1-3
- **Slot lag replay delay > 3,600 seconds (1+ hours) but growing** → ACTIVE SLOT LAGGING — proceed to Steps 1-3
- **Slot lag replay delay < 300 seconds** → Logical replication is healthy, check read-pool replica lag
- **Read-pool `replication_delay` > 1s OR `replicas_byte_lag` > 100 MB** → READ-POOL REPLICA LAG — proceed to Steps 4-5
- **Both metrics normal** → FALSE ALARM — proceed to Step 6 (verification only)

---

## Step 1: Logical Replication Slot Deep Dive (PRIMARY Only)

**Run this step if logical replication slot lag is elevated.** Run all commands in parallel.

**1a — Discover all instances in the cluster:**
```
gcloud alloydb instances list --cluster=<cluster-id> --region=asia-south1 \
  --format="table(name, instanceType, state, machineConfig.cpuCount)"
```

**1b — Slot lag trend via direct SQL (sample repeatedly across the alert window — HIGH confidence):**
```
db_query(<primary>, "SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag_size, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS lag_bytes FROM pg_replication_slots ORDER BY lag_bytes DESC NULLS LAST")
```
Prometheus-mirror byte-lag trend (30-minute window around alert) — use `prometheus_query_range`:
- query: `<alloydb_replicas_byte_lag_metric>{instance_id="<primary-instance>"}` — use the exact GCP metric name + label from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `1m`

**NOTE on Min/Max spread:** If different slots show very different `lag_bytes` (e.g., one at 2 days, another at 200 days), this means there are **multiple replication slots with different lag levels**. The worst slot drives the alert. Proceed to Step 2 to identify individual slots.

**1c — Slot disk usage / WAL retained per slot (BYTES — convert to MB/GB):**
```
db_query(<primary>, "SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes FROM pg_replication_slots ORDER BY retained_bytes DESC NULLS LAST")
```

**1d — WAL / transaction-log disk usage on the primary (indicates WAL retention):** Use `prometheus_query_range`:
- query: `<alloydb_wal_or_log_disk_metric>{instance_id="<primary-instance>"}` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`). AlloyDB does not expose an RDS-style `TransactionLogsDiskUsage`; the closest signal is WAL/log disk growth — the knowledge base names the right metric.
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `1m`

**1e — 7-day trend for slot lag (is it growing or stable?):** Sample slot lag via `db_query` across the last 7 days if you have historical snapshots, OR use `prometheus_query_range`:
- query: `<alloydb_replicas_byte_lag_metric>{instance_id="<primary-instance>"}` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 7d>`, end: `<startsAt>`, step: `1h`

**1f — Primary health metrics (to rule out primary overload as contributing factor):** Use `prometheus_query_range` for each of CPU utilization and write throughput. Use the exact GCP metric names from the Site Knowledge Base (`knowledge-gcp.md`):
- CPU query: `<alloydb_cpu_utilization_metric>{instance_id="<primary-instance>"}`
- Write-throughput query: `<alloydb_blocks_written_metric>{instance_id="<primary-instance>"}`
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `1m`

**1g — WAL generation on primary (sanity check — HIGH confidence direct SQL, sample over time):**
```
db_query(<primary>, "SELECT pg_current_wal_lsn(), pg_walfile_name(pg_current_wal_lsn()), pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')) AS total_wal_written")
```

**1h — AlloyDB cluster operations (failover, maintenance, scaling):**
```
gcloud alloydb operations list --cluster=<cluster-id> --region=asia-south1 \
  --format="table(name, operationType, status, createTime, endTime)"
```

**1i — Check the alert/alarm definition (Cloud Monitoring alert policy):**
```
gcloud monitoring policies list \
  --filter='displayName:"<alarm-name-from-alert>"' --format=json
```

**After Step 1:** Fill out this characterization:
```
Primary instance: <name>
Logical slot lag: <value> bytes = <MB>/<GB> (and replay delay in seconds = <days> days)
  - Multiple-slot spread: <best>/<worst> (multiple slots if different)
  - 7-day trend: STABLE / GROWING / FLUCTUATING
Retained WAL per slot: <value> bytes = <MB> = <GB>
WAL/log disk usage: <value> bytes = <MB> = <GB>
Primary CPU: <value>%
Primary write throughput: <value>
Read-pool byte lag (from Prometheus mirror): <value> bytes
Pattern: STALE SLOT / ACTIVE LAGGING / WRITE SURGE / NORMAL
```

---

## Step 2: Replication Slot Diagnostics via Direct SQL

**Run this step if Step 1 shows elevated logical replication slot lag.** Use `db_query` on the PRIMARY's PostgreSQL connection (bap_pg for customer, bpp_pg for driver).

Run `learnings_read(database)` first to get connection details.

**2a — List all replication slots with individual lag:**
```sql
SELECT slot_name, plugin, slot_type, active, restart_lsn, confirmed_flush_lsn,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag_size,
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS lag_bytes
FROM pg_replication_slots ORDER BY lag_bytes DESC NULLS LAST;
```

**Interpret:**
- **`active = false`** → Subscriber is DISCONNECTED. This slot is stale and retaining WAL. This is the most common cause of 100+ day lag.
- **`active = true` but `lag_bytes` is large and growing** → Subscriber is connected but can't keep up. Check the cross-cloud subscriber health.
- **`active = true` and `lag_bytes` is small** → Slot is healthy. If the lag metric is still high, there's likely ANOTHER stale slot.
- **Multiple slots** → The lag metric reflects the WORST slot. Identify which specific slot(s) are the problem.

**2b — Check active replication connections (who is consuming the slots):**
```sql
SELECT pid, application_name, client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn,
       write_lag, flush_lag, replay_lag,
       pg_size_pretty(pg_wal_lsn_diff(sent_lsn, replay_lsn)) AS replay_lag_bytes
FROM pg_stat_replication ORDER BY replay_lag DESC NULLS LAST;
```

**Interpret:**
- **No rows for a logical slot** → Subscriber is not connected. Confirms stale slot.
- **`state = 'streaming'`** → Active, healthy connection.
- **`state = 'catchup'`** → Subscriber is reconnecting and catching up. May resolve on its own.
- **Large `replay_lag`** → Subscriber is connected but falling behind.
- **`client_addr` tells you which cross-cloud instance** is the subscriber — useful for debugging the subscriber side (e.g. the AWS subscriber).

**2c — WAL retention and settings:**
```sql
SHOW wal_level;
```
```sql
SHOW max_replication_slots;
```
```sql
SHOW max_slot_wal_keep_size;
```

**Interpret `max_slot_wal_keep_size`:**
- If set to `-1` (default) → NO LIMIT on WAL retention. A stale slot will retain WAL forever, growing WAL/log disk usage until storage is full.
- If set to a value → WAL will be truncated even if the slot needs it, causing the slot to become invalid (subscriber will need full resync).

**2d — Current WAL position and throughput:**
```sql
SELECT pg_current_wal_lsn(), pg_walfile_name(pg_current_wal_lsn()),
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')) AS total_wal_written;
```

---

## Step 3: Assess Storage Risk from Stale Slots

**Run this step if Step 2 identified stale/inactive slots.**

**3a — Current storage utilization:** Use `prometheus_query_range`:
- query: `<alloydb_disk_bytes_used_metric>{instance_id="<primary-instance>"}` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`). AlloyDB storage is auto-managed; there may be no "FreeLocalStorage" equivalent — the knowledge base names whichever free/used storage metric is available.
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `1m`

**3b — Storage / WAL trend (7-day):** Use `prometheus_query_range`:
- query: `<alloydb_disk_bytes_used_metric>{instance_id="<primary-instance>"}` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 7d>`, end: `<startsAt>`, step: `1h`

**Risk assessment:**
- **WAL/disk usage growing AND free storage shrinking** → URGENT: stale slot is consuming storage. If not dropped, primary will run out of storage.
- **WAL/disk usage stable** → Slot is stale but WAL has been truncated (`max_slot_wal_keep_size` is set) or write volume is low. Less urgent.
- **Free storage critically low** → CRITICAL: storage exhaustion imminent. Escalate to DBA immediately. (Note: AlloyDB storage auto-scales, but WAL retention from a stale slot can still cause cost/performance problems — confirm behaviour in Site Knowledge Base.)

---

## Step 4: Read-Pool Replica Lag Investigation (READ_POOL Only)

**Run this step if read-pool replica lag is elevated (replay delay > 1s or `replicas_byte_lag` > 100 MB).** This is separate from logical replication slots.

Run all commands in parallel:

**4a — Replica lag across all read-pool instances (direct SQL — HIGH confidence):**
```
db_query(<replica>, "SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay, pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()")
```
Prometheus-mirror byte-lag across read pools (cross-check) — use `prometheus_query_range`:
- query: `<alloydb_replicas_byte_lag_metric>` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `1m`

**4b — Primary write throughput (heavy writes cause read-pool lag):** Use `prometheus_query_range`:
- query: `<alloydb_blocks_written_metric>{instance_id="<primary-instance>"}` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `1m`

WAL generation on primary over time (direct SQL — HIGH confidence, sample repeatedly):
```
db_query(<primary>, "SELECT pg_current_wal_lsn()")
```

**4c — Read-pool CPU (long-running queries on a read pool hold read locks, blocking apply):** Use `prometheus_query_range`, one query per read-pool instance (or aggregate by instance label):
- query: `<alloydb_cpu_utilization_metric>{instance_id="<read-pool-instance>"}` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `1m`

**4d — Long-running queries on read pools (these can block replication apply):**
```sql
SELECT pid, now() - query_start AS duration, state, wait_event_type, wait_event, left(query, 200) as query
FROM pg_stat_activity
WHERE state = 'active' AND query NOT LIKE '%pg_stat_activity%'
ORDER BY duration DESC LIMIT 20;
```

**4e — 7-day baseline for read-pool lag:** Use `prometheus_query_range`:
- query: `<alloydb_replicas_byte_lag_metric>{instance_id="<read-pool-instance>"}` — use the exact GCP metric name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 7d - 15m>`, end: `<startsAt - 7d + 1h>`, step: `5m`

---

## Step 5: Check Business Impact (Run in Parallel with Steps 1-4)

**5a — 5xx error rate (Prometheus):**
- query: `sum by(service,handler)(rate(http_request_duration_seconds_count{handler!="/v1/",status_code=~"^5.."}[1m]))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5b — P99 latency (Prometheus):**
- query: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5c — GKE ingress / load balancer 5xx (Prometheus):** Use `prometheus_query_range`:
- query: `<gclb_or_ingress_5xx_metric>` — use the exact GCP load-balancer / GKE-ingress 5xx metric name from the Site Knowledge Base (`knowledge-gcp.md`)
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5d — Key business metrics (check Site Knowledge Base for deployment-specific metrics):**
- Use business-critical metrics from knowledge base (e.g., conversion rates, transaction counts)
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `5m`

**Impact assessment — fill this out:**
```
5xx rate:          NONE / LOW (<10/min) / MEDIUM (10-100/min) / HIGH (>100/min)
P99 latency:       NORMAL / DEGRADED (>3s) / SEVERE (>10s)
Business:          STABLE / DEGRADED — <which metrics affected>
User impact:       YES / NO — <describe affected user operations>
Replication impact: Is cross-cloud-served traffic using stale data? YES/NO
                    If logical slot lag is 100+ days, the subscriber's data is 100+ days stale for that slot.
```

---

## Step 6: Correlate — Recent Changes and External Factors

**Run this step to identify what changed that could have caused or contributed to the lag.**

**6a — Recent deploys (GKE):**
```
kubectl get replicasets -n <namespace> --sort-by=.metadata.creationTimestamp -o wide | tail -15
```

**6b — AlloyDB operations (failover, maintenance, scaling):**
```
gcloud alloydb operations list --cluster=<cluster-id> --region=asia-south1 \
  --format="table(name, operationType, status, createTime, endTime)"
```

**6c — Check if the cross-cloud subscriber service is running (if you have access):**
Look in ES or Kubernetes logs for the cross-cloud logical replication subscriber for errors:
```json
{
  "index": "<app-log-index-from-knowledge-base>",
  "size": 20,
  "sort": [{"@timestamp": "desc"}],
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "<startsAt-1h>", "lte": "<startsAt+1h>"}}},
        {"bool": {
          "should": [
            {"match": {"message": "replication"}},
            {"match": {"message": "logical"}},
            {"match": {"message": "subscriber"}},
            {"match": {"message": "slot"}},
            {"match": {"message": "wal_receiver"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "_source": ["message", "@timestamp", "service"]
}
```

---

## Synthesis — Hypothesis Verification Matrix

**MANDATORY: Work through EVERY hypothesis below. For each one, state CONFIRMED / RULED OUT / INCONCLUSIVE with specific evidence.**

### Hypothesis 1: Stale/Inactive Logical Replication Slot
**Check:** `pg_replication_slots` shows `active = false` for one or more slots + slot lag is 100+ days
**Verify:** No corresponding entry in `pg_stat_replication` for that slot. The cross-cloud subscriber is disconnected.
**Rule out:** If ALL slots show `active = true` → NOT this
**Severity:** LOW urgency (data is stale but not causing immediate outage) UNLESS WAL/disk usage is growing dangerously
**Confidence if confirmed:** HIGH
**Fix:** Drop the stale slot — **requires DBA approval**: `SELECT pg_drop_replication_slot('<slot_name>');`

### Hypothesis 2: Active Slot Lagging (Subscriber Can't Keep Up)
**Check:** `pg_replication_slots` shows `active = true` but `lag_bytes` is large and growing. `pg_stat_replication` shows `state = 'streaming'` but `replay_lag` is significant.
**Verify:** Primary write throughput is high, subscriber is connected but falling behind. Check if the cross-cloud subscriber host has CPU/memory/disk issues.
**Rule out:** If `lag_bytes` is small or stable → NOT this. If `active = false` → this is Hypothesis 1 instead.
**Confidence if confirmed:** HIGH
**Fix:** Investigate cross-cloud subscriber health. Increase `wal_sender` resources if applicable. Reduce write load on primary if possible.

### Hypothesis 3: Heavy Write Load on Primary
**Check:** Primary write throughput / WAL generation surged > 2x baseline at alert time. Both logical replication slot lag and read-pool replica lag increased simultaneously.
**Verify:** Query Insights / `pg_stat_statements` shows heavy write queries. All downstream replication (logical + read-pool) fell behind together.
**Rule out:** If write throughput is normal/baseline → NOT this
**Confidence if confirmed:** HIGH
**Fix:** Identify and optimize the heavy write query. If it's a batch job, schedule for off-peak.

### Hypothesis 4: Read-Pool Replica Lag Spike (Read-Pool-Specific)
**Check:** `replicas_byte_lag` / replay delay on one or more READ_POOL instances is elevated. Logical replication slot lag on the primary is normal (< 300 seconds).
**Verify:** Long-running query on the read pool in `pg_stat_activity` holding read locks that block replication apply. OR primary write throughput is high causing apply backlog.
**Rule out:** If replay delay < 1s / byte lag < few MB on all read pools → NOT this
**Confidence if confirmed:** HIGH
**Fix:** Kill the long-running query on the read pool (requires DBA). If caused by write surge, will self-resolve when writes decrease.

### Hypothesis 5: WAL Disk Growth (Storage Risk)
**Check:** WAL/log disk usage is growing steadily over 7-day trend. Retained WAL per slot is non-trivial (> 1 GB). Free storage is declining.
**Verify:** A stale slot with `active = false` is retaining WAL. `max_slot_wal_keep_size` is `-1` (no limit).
**Rule out:** If WAL/disk usage is stable and free storage is ample → storage is not at risk
**Severity:** Can escalate to CRITICAL if storage fills up — primary will become read-only
**Confidence if confirmed:** HIGH
**Fix:** Drop the stale slot (DBA). Set `max_slot_wal_keep_size` to a safe value to prevent unbounded WAL retention.

### Hypothesis 6: Network/Connectivity Issue (Fluctuating Lag)
**Check:** Slot lag across slots differs wildly (e.g., one slot at 2 days, another at 200 days). This indicates **multiple slots with different health**, NOT a single slot fluctuating.
**Verify:** `pg_replication_slots` shows multiple slots — some active and healthy, some stale. The best slot reflects healthy replication, the worst reflects the stale one.
**Rule out:** If only one replication slot exists → the fluctuation is real network instability (check the cross-cloud VPN/interconnect). If all slots have similar lag → NOT this.
**Confidence if confirmed:** MEDIUM
**Fix:** Identify which specific slots are stale vs healthy (Step 2a). Address stale slots individually.

### Hypothesis 7: Normal / False Alarm
**Check:** Logical replication slot lag is < 300 seconds AND read-pool replica lag is < 1s / few MB on all read pools. All metrics within normal range.
**Verify:** Alarm threshold may be too sensitive. Check alert policy — what threshold triggered it?
**Rule out:** If any lag metric is genuinely elevated → NOT a false alarm
**Confidence if confirmed:** HIGH (no action needed)
**Fix:** Adjust alert policy threshold to match actual operational requirements.

---

## Final Verdict

After verifying all hypotheses, state:

```
## Verified Hypotheses
| # | Hypothesis | Verdict | Key Evidence |
|---|-----------|---------|--------------|
| 1 | Stale/inactive logical replication slot | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 2 | Active slot lagging (subscriber can't keep up) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 3 | Heavy write load on primary | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 4 | Read-pool replica lag spike | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 5 | WAL disk growth (storage risk) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 6 | Network/connectivity (fluctuating lag) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 7 | Normal / false alarm | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |

## Replication Type
LOGICAL SLOT / READ-POOL REPLICA / BOTH / NEITHER (false alarm)

## Root Cause
<Confirmed hypothesis with full evidence chain>
<Identify the SPECIFIC slot name if logical replication, or SPECIFIC read-pool instance if read-pool replica>

## Confidence: HIGH / MEDIUM / LOW
<Why this confidence level — what evidence supports it, what's missing>

## Unit Verification (MANDATORY)
- Logical slot lag raw value: <X> bytes = <X/1048576> MB = <X/1073741824> GB (and replay delay <X> seconds = <X/86400> days)
- Retained WAL per slot raw value: <X> bytes = <X/1048576> MB = <X/1073741824> GB
- WAL/log disk usage raw value: <X> bytes = <X/1048576> MB = <X/1073741824> GB
- Read-pool replicas_byte_lag raw value: <X> bytes = <X/1048576> MB

## Business Impact
5xx: <rate and trend>
Latency: <p99 and trend>
Users affected: <yes/no, which operations>
Cross-cloud data staleness: <if logical slot lag, how stale is the subscriber's data>

## Immediate Fix
- If stale logical slot: "Drop replication slot '<slot_name>': SELECT pg_drop_replication_slot('<slot_name>');" — requires DBA approval. WARNING: after dropping, the cross-cloud subscriber will need full resync.
- If active slot lagging: "Check cross-cloud subscriber health. Ensure subscriber service is running and has resources. If subscriber is healthy, consider increasing wal_sender_timeout."
- If heavy write load: "Identify and optimize heavy write query from Query Insights / pg_stat_statements. If batch job, reschedule to off-peak."
- If read-pool replica lag: "Identify and kill long-running query on the read pool: SELECT pg_terminate_backend(<pid>);" — requires DBA approval.
- If WAL disk growth: "URGENT: Drop stale slot to stop WAL retention. Set max_slot_wal_keep_size to prevent recurrence."
- If network/connectivity: "Multiple slots with different health — address each stale slot individually. Check cross-cloud VPN/interconnect status."
- If false alarm: "Adjust alert policy threshold from <current> to <recommended>."

## Prevention
<What change prevents recurrence — be specific>
- For stale slots: Set up monitoring on pg_replication_slots active status, alert when active=false for >1 hour
- For WAL growth: Set max_slot_wal_keep_size to a safe limit (e.g., 100GB)
- For read-pool replica lag: Set up query timeout on read-pool connections to prevent long-running queries

## Needs More Investigation
YES / NO — <if YES, what specifically needs checking and by whom>
```

---

## Extended Investigation

If ALL hypotheses are INCONCLUSIVE after the above steps:
- Check if the replication slot was recently created or recreated (slot age)
- Check PostgreSQL error logs for replication-related errors (AlloyDB logs via Cloud Logging: `gcloud logging read 'resource.type="alloydb.googleapis.com/Instance"'`)  # VERIFY exact syntax against Site Knowledge Base
- Check if the cross-cloud subscriber has been recently restarted or reconfigured
- Verify network connectivity between AWS and GCP (VPN tunnel / interconnect status, peering health)
- Check if `wal_level` is set to `logical` (required for logical replication)
- Look for recent AlloyDB version upgrades or maintenance windows (see `gcloud alloydb operations list`) that could have interrupted replication
- Check `pg_stat_wal` for WAL generation rate — if WAL generation is extremely high, subscribers may never catch up
