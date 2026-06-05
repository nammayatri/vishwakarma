# Architecture — the Argus topology

Vishwakarma runs in two shapes:

1. **All-in-one** (`vk serve`) — one pod does everything. SQLite, in-memory
   dedup, in-process investigations. Zero external dependencies; the OSS
   quickstart and the original production mode.
2. **Multi-pod / multi-cloud** (`vk serve-orchestrator` + `vk serve-executor`)
   — for environments whose data planes span clouds with VPC-internal
   databases. One orchestrator routes; per-cloud executor pools investigate.

```
  Alert webhook / @mre Slack mention / @Argus mention
                     │
           ┌─────────▼──────────┐
           │   ORCHESTRATOR      │ 1 replica. Owns the Slack bots
           │ (vk serve-          │ (Sage chat + Argus RCA trigger),
           │  orchestrator)      │ the webhook, the console UI, dedup,
           └─────────┬──────────┘ cloud routing, enqueue.
                     │ XADD vk:jobs:{aws|gcp}   ('both' fans out)
           ┌─────────▼──────────┐
           │  Redis Streams      │ consumer group 'executors' per stream;
           │  (control plane)    │ at-least-once, XAUTOCLAIM recovery
           └────┬───────────┬───┘
       cloud=aws│           │cloud=gcp
       ┌────────▼──┐    ┌───▼────────┐
       │ AWS EXEC  │    │ GCP EXEC   │  vk serve-executor --cloud …
       │ pool(EKS) │    │ pool (GKE) │  Each runs the full investigation
       └────────┬──┘    └───┬────────┘  flow and posts the RCA.
                └─────┬─────┘
           ┌──────────▼─────────┐
           │ Postgres (pgvector) │ incidents · durable investigations ·
           │   control plane     │ runbooks · patterns · embeddings
           └────────────────────┘
```

## The investigation flow (per alert)

1. **Ingress** — AlertManager webhook, CloudWatch-via-Slack, or a human
   tagging `@mre`/`@Argus` (the Argus bot matches the subteam mention and
   runs a fail-open issue-vs-noise filter).
2. **Dedup** — Redis `SETNX`+TTL by alert fingerprint (in-memory fallback in
   all-in-one mode). One investigation per alert at a time.
3. **Routing** — `core/cloud_router.py` classifies the alert to the cloud
   whose executors can reach its data plane (labels: account, region,
   cluster, source). Cross-cloud signals fan out to both.
4. **Pre-enrichment (parallel)** — kubectl prefetch, prior-incident lookup
   (text + semantic via incident embeddings), entity extraction, hybrid
   runbook matching (exact-map + keyword + vector → RRF → LLM rerank).
5. **Pattern replay** — if a HUMAN-CONFIRMED pattern exists for this alert,
   replay its stored tool calls and verify with keyword checks; on match,
   instant RCA. (The old "fast RCA" preliminary classifier was removed —
   it was wrong too often.)
6. **Agentic loop** — the engine streams LLM steps; up to 16 tools execute
   in parallel per step; tool outputs > 8KB are compressed; context compacts
   at 80% of the window. The conversation is **checkpointed to the
   investigations table at every step**.
7. **Code intelligence** — Tier 1: `code_analyst` tools (repo cache with
   clone-once/pull-fresh, blame, log-around-incident, deploy diff,
   stacktrace→source). Tier 2: conversational **OpenCode sessions**
   (`code_session_*` tools) — read mode for tracing, edit mode (gated) in an
   isolated git worktree for writing a fix.
8. **Output** — RCA → Slack (mrkdwn) + PDF + Postgres; live events stream to
   the console over SSE via the Redis event bus.

## Durability model

| Failure | Recovery |
|---|---|
| LLM/tool timeout within a step | engine retries (bounded) |
| Executor pod dies mid-investigation | stream message stays pending → another executor `XAUTOCLAIM`s it → reloads the checkpointed conversation → resumes from the last step |
| Same job delivered twice | investigations table: `done`/`failed` → ack and skip |
| Investigation keeps crashing | attempt budget (3) → `failed`, surfaced in console |
| Dedup lock leaked (pod crash) | TTL self-expiry |

## Learning loops

- **✅/❌ feedback** (Slack buttons + console) → evidence outcomes, pattern
  hit/miss, runbook hit/miss (auto-demote on repeated misses), and
  alert→runbook map self-population.
- **Runbooks live in Postgres** (seeded from `plugins/agents/agents.json` +
  `plugins/runbooks/*.md`) — author via console studio, no deploy needed.
- **Incident embeddings** (when an embeddings provider is configured) make
  recurrence lookup semantic; everything degrades to keyword matching
  without one.

## Key modules

| Module | Role |
|---|---|
| `core/engine.py` | agentic loop (streaming, overlap tool dispatch, checkpointing) |
| `core/cloud_router.py` / `core/jobstream.py` | multi-cloud dispatch |
| `core/runbook_match.py` | hybrid recall → RRF → rerank |
| `core/code_agent.py` | OpenCode session adapter (read/edit, worktrees) |
| `core/eventbus.py` | live events → console SSE (in-process + Redis) |
| `executor.py` | per-cloud worker loop |
| `storage/` | dual-backend (SQLite/Postgres) incidents, investigations, runbooks, vectors, dedup, patterns, evidence |
| `bot/slack.py` / `bot/argus.py` | Sage (chat) / Argus (RCA trigger) |
| `ui/console_api.py` + `web/` | console REST/SSE + React SPA at `/console` |
