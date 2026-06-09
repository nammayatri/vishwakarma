# Vishwakarma → God-Level RCA + Fix Agent — Strategic Roadmap

> **⚡ WORKING STATUS — keep this section updated as we build (compaction-proof anchor)**
>
> - **Branch:** `argus` (created from `ny-vishwakarma` @ 07f5fe9, v1.1.23 in prod)
> - **Current milestone:** Milestone 1 = Phase 0 (Postgres+pgvector, Memorystore dedup, durable `investigations` table, SQLite history migration) + Phase 1 (Tier-1 code-analyst toolset)
> - **Status:** GAP-CLOSURE COMPLETE (gaps 1-9 from the planning-history audit) + Phase 6a + fast-RCA removed. 89 tests passing. Roadmap code-complete except items genuinely blocked on externals (see "Deferred / v2" below).
> - **Next step:** shadow deployment (build image, provision Cloud SQL + Memorystore, apply k8s/argus/ manifests → test channel) OR wire pr_create when the GitHub App exists. Plain YAML in k8s/argus/ — NO Helm. Pending externals: **Argus Slack app registration**; GitHub App; Acme embeddings model; ~/.config/opencode old key; Cloud SQL + Memorystore (GCP).
>
> **Deferred / v2 (NOT built — don't assume these exist):**
>   - (curated tool-subset — DONE, see gap-closure #10)
>   - (`both`-cloud SYNTHESIZER — DONE: core/cross_cloud.py; each half writes findings, second finisher atomically claims + merges + posts one unified RCA; executor suffixes tracking id per cloud)
>   - Code RAG (`code_semantic_search` + repo embedding index) — pointless until the gateway has an embeddings model.
>   - Run-until-verified — engine still uses fixed step budgets, not evidence-based stop.
>   - Incident correlation (alert-storm grouping into one investigation) — only fingerprint dedup today.
>   - LSP/HLS via MCP bridge — code_analyst uses git+ast-grep+rg, no semantic LSP.
>   - pr_create + CI-result reading + golden-set eval — blocked on GitHub App (the fix-confidence SCORER + gate ARE built, core/fix_scorer.py).
>   - NY-data scrub for public OSS (agents.json/runbooks still NY-specific — fine internally).
> - **Done so far:**
>   - Phase 0 ✅ dual-backend storage (`storage/db.py` — SQLite default + Postgres via `VK_PG_DSN`/`storage.dsn`, PGConnection adapter translates `?`→`%s`, conditional pgvector)
>   - Phase 0 ✅ durable investigations (`storage/investigations.py` — create/claim/checkpoint/resume/orphan-reap/attempt-budget; engine checkpoints each step via `stream_investigate(incident_id=…)`)
>   - Phase 0 ✅ Redis dedup (`storage/dedup.py` — SETNX+TTL via `VK_REDIS_URL`, in-memory fallback; replaced `_active_fingerprints` in server.py)
>   - Phase 0 ✅ migration (`storage/migrate.py` + `vk migrate-db --from … --to …`, idempotent)
>   - Phase 0 ✅ tests (`tests/test_storage_phase0.py` — 9 passing: parity, lifecycle, races, TTL, migration)
>   - Phase 1 ✅ code_analyst toolset (`plugins/toolsets/code_analyst/` — repo_sync clone-once/ff-pull, git_blame, git_log_around, deploy_diff, code_search (ast-grep→rg fallback), stacktrace_to_source w/ suffix matching + blame; read-only git, repo allow-list, path-traversal + ref-injection guards)
>   - Phase 1 ✅ tests (`tests/test_code_analyst.py` — 10 passing against a fixture repo simulating a breaking deploy)
>   - Phase 2 ✅ embeddings client (`core/embeddings.py` — configurable provider, None-degrade; gateway has NO embeddings model yet)
>   - Phase 2 ✅ vector store (`storage/vectors.py` — pgvector when available, JSON+cosine fallback for SQLite/PG14)
>   - Phase 2 ✅ incident RAG (index on save + semantic leg in `_build_prior_context`, best-effort)
>   - Phase 2 ✅ runbook tables + CRUD (`storage/runbooks.py` — normalize_alert_key, map upsert, hit/miss auto-demote, seed_from_files: 13 prod runbooks import cleanly)
>   - Phase 2 ✅ hybrid matcher (`core/runbook_match.py` — exact-map/keyword/vector → RRF → LLM rerank >3 candidates; wired into `load_matching_runbooks` with file fallback) + `runbook_search` tool
>   - Phase 2 ✅ tests (`tests/test_rag_phase2.py` — 12 passing; full suite 31)
>   - Phase 3 ✅ OpenCode probed (1.4.3: POST /session, blocking /session/{id}/message → {info,parts}; agent 'plan'=read-only, 'build'=edit; cwd-scoped servers; provider key via {env:VK_GATEWAY_KEY}, never on disk)
>   - Phase 3 ✅ CodeAgent adapter (`core/code_agent.py` — start(repo,mode)/send/end; edit mode = isolated worktree on argus/fix-* branch, diff collected, worktree cleaned; per-send timeout + session wall-clock budget; transcript checkpointable)
>   - Phase 3 ✅ code_session toolset (`plugins/toolsets/code_session/` — start/send/end tools; read always, edit behind allow_edit config; repo allow-list)
>   - Phase 3 ✅ tests (`tests/test_code_agent.py` — 9 fake-backend + 1 REAL OpenCode integration passing via Acme gateway; full suite 40)
>   - Phase 4a ✅ cloud router (`core/cloud_router.py` — explicit label > single-cloud signals > both > default)
>   - Phase 4a ✅ job stream (`core/jobstream.py` — Redis Streams vk:jobs:{aws,gcp}, group 'executors', enqueue/consume/ack, XAUTOCLAIM stale recovery, both fan-out, depth/pending metrics)
>   - Phase 4a ✅ topology entrypoints (`vk serve-orchestrator` — webhook→dedup→route→enqueue, owns Slack bot; `vk serve-executor --cloud aws|gcp` — consume→reuse _do_investigation→ack, idempotent re-delivery via investigations table, SIGTERM-graceful; `vk serve` unchanged all-in-one)
>   - Phase 4a ✅ tests (`tests/test_phase4_dispatch.py` — 12 passing: router variants, stream isolation/fan-out/stale-claim, executor job handling + duplicate-drop + bad-payload-drop; full suite 52)
>   - Phase 4b ✅ Argus bot (`bot/argus.py` — @mre subteam-mention + direct-mention triggers, fail-open noise filter w/ strong issue prior, debug override, screenshot URL capture, message dedup, dispatcher: orchestrator→enqueue / all-in-one→in-process; wired into `vk serve` + `vk serve-orchestrator`; Sage untouched; config argus.{bot_token,app_token,mre_group_id} / ARGUS_* env)
>   - Phase 4b ✅ tests (`tests/test_argus_bot.py` — 13 passing; full suite 65)
>   - Phase 5a ✅ event bus (`core/eventbus.py` — in-process + Redis pub/sub vk:events, no double-delivery, slow-subscriber drop; published from server stream loop + executor)
>   - Phase 5a ✅ console API (`ui/console_api.py` @ /api/console — overview, investigations list/detail w/ checkpointed transcript, incidents search (LIKE+semantic), runbook studio CRUD/mappings/dry-run, feedback → evidence+runbook counters, fixes (awaiting_fix_review), fleet (queue depth/pending, executor heartbeats, orphans), SSE /events w/ keepalive + incident filter; RBAC admin/reader via X-VK-Token, auth_disabled dev default)
>   - Phase 5a ✅ tests (`tests/test_console_api.py` — 12 passing; full suite 77)
>   - Phase 6 ✅ fast-RCA REMOVED (core/fast_rca.py deleted; server auto-resolve path gone; patterns.replay_pattern cleaned)
>   - Phase 6a ✅ Dockerfile node build stage (web → /app/web/dist); SPA mount resolves repo/container/env layouts
>   - Phase 6a ✅ k8s/argus/ plain-YAML manifests (orchestrator Recreate-strategy, executor-{aws,gcp} pools, configmap+secrets examples, shadow-mode README) — NO Helm per user
>   - Phase 6a ✅ docs (docs/ARCHITECTURE.md — topology+flow+durability; docs/SECURITY.md — threat model; LICENSE Apache-2.0; README Argus section)
>   - Gap-closure ✅ (9 gaps from planning-history audit, tests/test_gap_closure.py 12 passing):
>     1. Argus RCAs post to the report channel/thread (not the env alert channel)
>     2. multimodal — Slack screenshots fetched (bot-token auth) → data URLs → images= into the engine
>     3. Slack ✅/❌ now credits runbooks (hit/miss + map self-populate) via meta.matched_runbook_ids
>     4. code_session transcript checkpointed to investigations.code_session (ContextVar via core/toolcontext.py)
>     5. LLM key pool (core/keypool.py) — round-robin + 429 bench; llm.py uses it; config llm.api_keys / VK_API_KEYS
>     6. fix-confidence scorer + gate (core/fix_scorer.py) — never PRs unvalidated/broad-diff/test-failed
>     7. per-cloud knowledge (knowledge-<cloud>.md with fallback)
>     8. Prometheus self-metrics (core/metrics.py) at GET /metrics
>     9. audit_log table + storage/audit.py; console mutations audited; GET /api/console/audit (admin)
>     10. curated tool-subset (core/tool_selection.py) — orchestrator picks relevant toolsets by alert domain+cloud, stable across the run (prompt-cache + accuracy); engine.stream_investigate(tool_subset=…); falls back to all when uncertain/empty. Closes the last buildable item from the planning discussion.
>   - Phase 5b ✅ console frontend (`web/` — Vite+React+TS+Tailwind dark theme, base /console/; pages: Dashboard, Investigations + live SSE detail w/ checkpointed transcript, Incidents search/detail w/ ✅/❌ feedback, Runbook studio (editor/dry-run/mappings), Fixes queue, Fleet (queues/executors/orphans), Settings (token). Typed api client w/ X-VK-Token. Served by server.py `_mount_console_spa` at /console with SPA fallback + traversal-safe assets; builds clean: 74KB gzip)
> - **Key context to re-load after compaction:** read this whole file; prod deployment is untouched single-pod v1.1.23 on EKS `monitoring` ns; all architectural decisions are final (see "Confirmed decisions" + "Resolved in discussion"); never touch prod infra without asking the user.

## Context

Vishwakarma today is a single-pod, in-process SRE RCA agent: an alert/Slack-mention arrives, a hand-rolled agentic loop (`core/engine.py`) runs read-only tools (kubectl/prometheus/DB/ES), and posts an RCA + PDF to Slack. It already has more than expected — domain-parallel **sub-agents** (`core/sub_agents.py`), a **confidence framework** (fast-RCA HIGH/MED/LOW), a **pattern DB + statistical baselines** (`storage/patterns.py`, `evidence.py`), and a `code_search` toolset. Gaps: no distributed orchestration, no semantic RAG, no code deep-dive→fix, no PR creation, all state in single-writer SQLite.

The goal: an open-source, horizontally-scaled, "god-level" agent. One orchestrator receives alerts/mentions; a pool of worker pods runs investigations and code analysis with no compute constraint. It deep-dives into repos, correlates incidents to commits/lines, and — **gated by confidence/score** — either opens a **draft PR with the fix** (human always merges) or posts "here's the issue + exactly what code to change." Anyone can author runbooks; read access to ClickHouse/DBs/cloud is broad; investigations run until a verified root cause (higher checkpoint threshold), not a fixed step count.

**Confirmed decisions:** Full phased roadmap · Postgres + pgvector (Cloud SQL, GCP) · Draft-PR-only (human merges, never auto-merge) · Redis Streams via **GCP Memorystore** · Orchestrator + control plane in GCP, executors per cloud over existing peering · **OpenCode locked as the v1 coding agent** (Aider/Claude Code optional behind the same adapter) · **Argus posts ALL investigation output** (alert- and @mre-triggered); Sage becomes purely chat/commands · Gateway scaling via API-key pool · Fix validation via GitHub Actions · UI: all 7 pages v1, admin/reader roles, internal-only.

**Milestone 1 (new branch): Phase 0 + Phase 1 together** — Postgres+pgvector migration (with SQLite history import), Memorystore dedup + durable `investigations` table under the *existing* single-pod topology, plus the Tier-1 code-analyst toolset (repo cache w/ clone-once-pull-fresh, git blame/log/deploy_diff, ast-grep, stacktrace_to_source). Reviewable as one PR series; system keeps working throughout.

---

## Target Architecture (multi-cloud: AWS + GCP)

**Key constraint (from the NY multi-cloud architecture):** each cloud's DB/Redis/cluster is VPC-internal. A worker can only reach AWS RDS/Redis/EKS from *inside* AWS, and GCP AlloyDB/Redis/GKE from *inside* GCP (the existing "GCP pgshell" runbook exists precisely because GCP DB is VPC-internal). DB replicates AWS→GCP via logical replication; **Redis does NOT replicate**. Therefore: one global orchestrator, but **executor pods per cloud**, each reading cloud-tagged jobs off a shared Redis Stream.

```
   Alert webhook / Slack mention / Jira / GitHub issue
                      │
            ┌─────────▼──────────┐
            │   ORCHESTRATOR pod  │  stateless: dedup, classify, enrich,
            │   (one global; runs │  decide target cloud(s), enqueue.
            │    where convenient)│  Owns Slack ack + routing.
            └─────────┬──────────┘
                      │ XADD to Redis Stream, job tagged cloud=aws|gcp|both
                      ▼
            ┌────────────────────┐
            │  Redis Stream +     │  consumer groups: "aws", "gcp"
            │  consumer groups    │  cross-cloud job (BAP↔BPP) → fan out to both
            └────┬───────────┬───┘
        cloud=aws│           │cloud=gcp
        ┌────────▼──┐    ┌───▼────────┐
        │ AWS EXEC  │    │ GCP EXEC   │   ← worker pools, one per cloud,
        │ pool      │    │ pool       │     scaled independently. Each reaches
        │ (in EKS)  │    │ (in GKE)   │     ONLY its own cloud's RDS/Redis/k8s.
        │ engine +  │    │ engine +   │
        │ sub-agents│    │ sub-agents │
        └────────┬──┘    └───┬────────┘
                 └─────┬─────┘ findings written back to shared Postgres
                       ▼
            ┌────────────────────┐
            │  SYNTHESIZER +      │  merge per-cloud findings → evidence
            │  FIX-PLANNER        │  chain → RCA + confidence + fix plan.
            │  (orchestrator side)│  (code-analyst is cloud-agnostic — repos
            └─────────┬──────────┘   are on GitHub, runs anywhere)
                      ▼
         confidence/score gate (existing fast_rca + new fix score)
            ├── high + code fix → DRAFT PR (human merges)
            └── else → Slack: issue + "change these files/lines"
                      ▼
   Shared cross-cloud: Postgres+pgvector (incidents, patterns, dedup, RAG)
                       Redis Stream (job transport + ephemeral dedup locks)
                       Object store (PDFs, repo cache)
   Note: shared state DB sits in one cloud; the OTHER cloud's workers reach it
   over the existing cross-cloud path (same as BAP→BPP calls today).
```

**Routing logic (orchestrator):** classify each alert to a target cloud from its labels — `aws_account`, `region` (ap-south-1 = AWS, asia-south1 = GCP), cluster name (eks-* vs gke-*), or explicit `cloud` label. CloudWatch alarms → AWS; GCP Monitoring 5xx alerts → GCP. Cross-cloud incidents (a BAP issue whose root cause is in the BPP on the other cloud) are tagged `both` and fan out to both consumer groups; the synthesizer merges the two findings.

---

## Phase 0 — Foundation: shared state (unblocks everything)

**Why first:** multi-pod is impossible while dedup fingerprints live in an in-memory `set` (`server.py:29`) and incidents in single-writer SQLite.

- Migrate `storage/` from SQLite → **Postgres**. Keep the existing function API (`save_incident`, `search_incidents`, `check_dedup`, `set_dedup`, pattern/evidence CRUD) so callers don't change — swap the connection layer in `storage/db.py`. The shared Postgres lives in one cloud; workers in the other cloud reach it over the existing cross-cloud network path (same as today's BAP→BPP calls). Incidents/patterns are control-plane data (small, not latency-critical), so cross-cloud reads are fine.
- Move dedup (`_active_fingerprints`) and the concurrency semaphore from in-process (`server.py:29-44`) to **Redis** (atomic `SETNX` with TTL = dedup window). This makes dedup correct across pods AND across clouds. Note: this is a dedicated **control-plane Redis for the agent** (job transport + dedup), NOT the production app Redis — and unlike the app Redis it is shared cross-cloud on purpose.
- Add **pgvector** extension; new tables `incident_embeddings`, `code_embeddings`, `runbook_embeddings`.
- Keep `evidence.py` baselines + `patterns.py` as-is on Postgres (they're already statistical/keyword and pod-safe once the DB is shared).

Critical files: `storage/db.py`, `storage/queries.py`, `storage/evidence.py`, `storage/patterns.py`, `server.py:26-44`.

---

## Phase 1 — Code deep-dive (the real differentiator) — TWO TIERS

**Why early:** this is what turns "CPU is high" into "commit `c4ddaff` broke OpenNetwork forwarding, line 142." Build before fix/PR — the fix step depends on it. Split into two tiers by cost/depth; **don't hand-roll a code-edit engine — delegate the heavy part to a real coding agent.**

**Tier 1 — lightweight code tools, inside the RCA loop (fast, cheap, in-context).**
New toolset `plugins/toolsets/code_analyst/code_analyst.py` (subclass `Toolset`, `@register_toolset`, follow the `bash`/`code_search` pattern). For *correlation during investigation* — the agent forms a hypothesis without spinning a full coding agent:
- **Repo cache strategy: clone once to persistent storage, pull-latest before every RCA.** In-scope repos are cloned once onto a PVC (`repo_dir`); at investigation start the executor runs `repo_sync` (fetch + fast-forward pull) on the mapped repo(s) as one of the parallel pre-enrichment tasks, so the agent always reads current code with no full-reclone cost. Sparse-checkout for the big Haskell monorepo. OpenCode edit sessions take a cheap worktree off this cache. (Phase 2's code-RAG index refreshes incrementally on pull — re-embed only changed files.)
- `git_blame` / `git_log_around` — blame a file, list commits in a time window (correlate to incident `startsAt`).
- `deploy_diff` — given a service + timestamp, find the deployed image tag → resolve to commit → diff against previous.
- `ast_grep` / `find_definition` — structural search via **ast-grep** + symbol lookup. Optionally wrap **LSP language servers** (HLS for the Haskell backend) for go-to-def / find-references / types — IDE-grade navigation, far better than regex for tracing a Haskell codepath. Lowest-effort path: point the existing **MCP bridge** (`plugins/toolsets/mcp/`) at an off-the-shelf LSP-MCP or Sourcegraph-MCP server instead of building from scratch.
- `stacktrace_to_source` — map a stack frame (file:line from ES logs) to source.

Guardrails: read-only git ops only (clone/fetch/log/blame/show — never push; PR creation is the gated Tier-2/Phase-3 path). Repo allow-list in config. Reuse `bash` `_is_allowed` block philosophy. Config: `toolsets.code_analyst.{enabled, repos, repo_dir, default_branch, lsp: {...}}`.

**Tier 2 — a conversational coding-agent session the RCA agent drives (Phase 3).**
For deep understanding and any code change, the RCA agent opens a persistent OpenCode session and converses with it as a human-proxy (ask → read → follow up → change). Tier-1 tools are for *quick cheap* lookups (git blame, deploy_diff) that don't warrant a full session; the Tier-2 session is for deep read + fix. read mode always available, edit mode gated. See Phase 3.

Optional later: **Sourcegraph/Zoekt** for cross-repo search at scale — heavier infra, defer unless many-repo search becomes the bottleneck.

---

## Phase 2 — Targeted RAG (incidents + code + runbooks)

Three retrievers (runbook RAG matters now because "anyone authors anything" grows the corpus from 13 → hundreds — see the Runbooks section):
- **Incident RAG**: embed each resolved incident's (title + analysis + root_cause) into `incident_embeddings`. On new alert, semantic "have we seen this?" replaces/augments the `LIKE` search in `_build_prior_context` (`server.py`). Far better recall than substring match.
- **Code RAG**: embed repo symbols/functions into `code_embeddings`; a `code_semantic_search` tool for "where is offer-draining handled?" Hybrid with grep.
- **Runbook RAG**: embed runbook descriptions/keywords into `runbook_embeddings` as the middle matching stage (see Runbooks section).

Add an embeddings helper in `core/` (model via the existing LLM gateway if it exposes embeddings, else a small local model). Wire incident-RAG retrieval into the orchestrator's pre-enrichment task list alongside the existing 4 parallel tasks.

---

## Phase 3 — Confidence-gated fix → Draft PR (via a real coding agent)

**Builds on the existing confidence framework** (`fast_rca.py` HIGH/MED/LOW, `prompt.py` RCA_OUTPUT_FORMAT `## Confidence`). Add a **fix-confidence score** distinct from RCA confidence.

- **Delegate to a pluggable coding-agent backend, driven as a CONVERSATIONAL SESSION — not a one-shot call.** The RCA agent acts as a **human-proxy**: it decides *when* it needs code work, opens an OpenCode session, sends a request, **waits for and reads the response, then decides the next message** — iterating with OpenCode the way a developer would ("explain this codepath" → read → "check how X is called" → read → "now make this change"). Used for code **read / understand / help AND change**, not just a final diff.
- New `core/code_agent.py` defines a session-based `CodeAgent` adapter:
  - `start(repo, mode=read|edit) → session_id` — spins a headless OpenCode session in an isolated git worktree (`isolation: worktree`).
  - `send(session_id, message) → response` — **blocking**: the RCA agent sends an instruction/question, OpenCode runs its own internal steps, returns; the RCA agent reads it and decides whether to continue the session or stop.
  - `end(session_id) → {diff, test_results, pr_url}` — in `edit` mode, finalizes (tests + draft PR).
  - These are exposed to the RCA loop as tools (`code_session_start` / `code_session_send` / `code_session_end`) so the agent invokes them by its own judgment, mid-investigation.
- **read mode is always available** (Tier-1 style — understand code, trace the bug); **edit mode is gated** by the confidence score below (Tier-2 — write the fix + PR). Same session backend, different permission.
- **Default backend: OSS, model-agnostic** — OpenCode or Aider on the existing Acme LLM gateway (no Anthropic dependency, fits the OSS goal). **Claude Code is an optional backend** behind the same adapter. The coding agent already handles multi-file edits, running tests, and PR creation — we don't reimplement any of it.
- **Session state is part of the durable investigation** (state machine below): the `session_id` + transcript are checkpointed so a resumed investigation can reattach to or restart the OpenCode session instead of losing the code-work context.
- **Scoring inputs (the gate):** RCA confidence (existing), whether the exact line was identified, whether a deterministic pattern matched (`patterns.py`), blast radius (localized diff vs cross-cutting), whether the coding agent's generated tests pass, and diff size.
- **Gate:**
  - `fix_confidence >= HIGH` **and** localized diff **and** tests pass → coding agent opens a **DRAFT PR** (body = RCA + evidence chain + rollback, labels). Never auto-merge; human reviews/merges (in Slack or the Phase-5 UI).
  - else → Slack/UI: the issue + "change these files/lines" + the proposed diff inline, **no PR**.
- **Safety:** coding agent runs in a throwaway worktree (`isolation: worktree`); never pushes to default branch; PR always draft; every PR body has the rollback plan; idempotent per incident (don't open a 2nd PR on resume — see state machine); full audit row in Postgres. The whole Tier-2 path is behind the approval flag (reuse `require_approval` in `models.py`). Extend the existing read-only GitHub plugin (`plugins/channels/github/plugin.py`, currently comment-only) for the draft-PR call, or let the coding agent's own PR mechanism handle it.

---

## Phase 4 — Orchestrator + per-cloud worker pools (horizontal, multi-cloud scale)

- Split the current `server.py` monolith into **orchestrator** (webhook, dedup, classify, **target-cloud routing**, enrich, enqueue, Slack ack) and **worker/executor** (consume cloud-tagged job → `engine.stream_investigate` → sub-agents → write findings to Postgres). Entrypoints off one image: `vk serve-orchestrator` / `vk serve-executor --cloud=aws|gcp`.
- Transport: **Redis Streams** (`XADD`/`XREADGROUP`) with two consumer groups, `aws` and `gcp`. AWS executor pods (deployed in EKS) consume `cloud=aws` jobs; GCP executor pods (in GKE) consume `cloud=gcp`. A `both`-tagged job is XADD'd to each group's stream and the synthesizer waits for both findings. (Arq is fine for the single-cloud case but Streams' consumer groups model the cloud partitioning natively, so prefer raw Streams here.)
- **Why per-cloud, not a generic pool:** each executor only has network reach + IAM into its own cloud (EKS executor uses `AWS_PROFILE`, GKE executor uses the in-cluster GCP creds). This is non-negotiable given VPC-internal DBs/Redis. It also means the `database`/`bash`/cloud toolsets are configured per-cloud per pool.
- Job payload = enriched alert context + incident-RAG hits + target cloud + assigned investigation type. Findings (not full transcripts) are written back to shared Postgres keyed by incident_id; the orchestrator-side synthesizer reads them.
- Sub-agents (`sub_agents.py`) stay **in-process threads within each executor** (hybrid model) — short-lived domain probes, not worth cross-pod scheduling.
- Raise the checkpoint threshold: `CHECKPOINT_STEP` (`engine.py:43`) and `_severity_steps` (`server.py:349`) become "run until verified root cause," with periodic evidence checkpoints instead of a hard step wall. Add a confidence-based early-exit so cheap cases still finish fast.
- Liveness: executor writes heartbeat + current-step to Postgres; orchestrator shows "investigation N at step K (cloud=gcp)" and reaps dead jobs (Redis Streams `XPENDING`/`XCLAIM` for re-delivery if an executor dies mid-job).
- Cross-cloud RCA example: a rider-app (BAP) alert whose tickets aren't draining because OnConfirm lands in AWS but polling runs in GCP → orchestrator tags `both`, AWS executor inspects AWS KV/drainer, GCP executor inspects GCP KV/drainer, synthesizer correlates and names the cross-cloud forwarding gap. This is exactly the class of incident seen in the drainer-lag logs earlier.

---

## Phase 5 — Control-plane UI (configure + observe everything)

A proper web UI is the human surface over the whole system — runbook authoring/editing, live investigation viewing, RCA history, and the fix/PR approval gate. It fits naturally because Phase 0 puts all state in Postgres (directly queryable) and the engine already emits a live event stream (`server.py` streaming events: `step_start`, `tool_calls`, `hypothesis`, `compaction`, `complete` — today consumed only by `bot/slack.py`). The UI mostly *surfaces data that already exists*.

- **Stack:** **React + Vite SPA served statically by the orchestrator** (FastAPI static mount) — no separate frontend deployment, `helm install` ships the UI for free (OSS-friendly). shadcn/ui + Tailwind, dark ops theme. Lives in `web/` in the same repo.
- **Real-time:** **SSE** (one-way stream is all we need; actions over REST). Executors emit progress events → Redis pub/sub → orchestrator fans out via SSE. The engine's events (`step_start`, `tool_calls`, `hypothesis`, `compaction`, `complete`) are already produced — just add the transport.
- **v1 scope: ALL 7 pages** (full console in one go):
  1. **Dashboard** — live investigation cards (alert, cloud, step, confidence), today's stats, fleet health strip.
  2. **Live investigations** — flagship: real-time timeline per RCA — todo list updating, each step's tool calls + outputs (collapsible), evidence chain building, confidence evolving, OpenCode session transcript, final RCA + PDF + Slack-thread link, ✅/❌ feedback.
  3. **Incident history** — semantic search (incident-RAG), filters (cloud/service/severity/status), detail = same view as live + "similar past incidents" panel.
  4. **Runbook studio** — CRUD over `runbooks` + `alert_runbook_map`: markdown editor + metadata (cloud_type/keywords/services), mapping manager, **dry-run** a runbook against a sample alert, version history, live hit/miss stats.
  5. **Fixes & PRs** — proposed-fix queue: diff viewer, CI (GitHub Actions) status, confidence breakdown; **admin approves in the UI → PR flips draft→ready; actual merge stays on GitHub** with normal review (two-step, audited in both places). Past fixes with outcomes.
  6. **Fleet** — executors per cloud (heartbeat, current job), Redis Stream depth, key-pool health (429 rate/key), token/cost per day, dead-man status.
  7. **Knowledge & Settings** — edit per-cloud knowledge files + component→repo map; watched channels, confidence thresholds, model config, user/role management.
- **Security posture: internal-only** — served behind the private ingress with Google SSO; OSS docs state it's designed for internal/VPN exposure (not internet-hardened). CSRF/rate-limiting hardening deferred unless OSS demand pushes it.
- **Auth/RBAC (required — it's a write surface):** Google SSO (`@example.com` Workspace) + **two roles: `admin` (edit runbooks/knowledge/settings, approve fixes/PRs) and `reader` (view everything, no writes)**. Role assignment via a simple in-UI user table managed by admins. Every runbook edit and fix approval is audit-logged in Postgres. Same security tier as the `pr_create` mutation tool — ships with the UI, not after.
- **Reuse:** incidents/patterns/evidence already in Postgres post-Phase-0; events already emitted by the engine; PDFs already generated by `bot/pdf.py`.

Critical files: extend `server.py` with REST + WS endpoints, new `web/` Next.js app, reuse the event stream from `core/engine.py` and the storage layer from `storage/`.

---

## Phase 6 — Open-source hardening

- Strip all NY-specific data: `knowledge.md`, `agents.json` runbooks, instance IDs, the hardcoded `fast_rca.py` `_REGISTRY` + decision trees → move to config/PVC, ship generic examples.
- Pluggable everything already exists (toolset registry, YAML toolsets, channels). Document: "bring your own runbook," "bring your own toolset," "bring your own LLM gateway."
- Secrets: nothing in repo; all via env/k8s secrets (already the pattern).
- Helm chart for the 2-deployment + Postgres + Redis topology. README quickstart, architecture doc, CONTRIBUTING.
- License + threat model doc (the write-capable `pr_create` tool needs an explicit security section).

---

## Runbooks in the new architecture (how authoring + matching + execution change)

Today: `.md` files in `plugins/runbooks/` + registration in `plugins/agents/agents.json`, matched by keyword then LLM fallback (`config.py:load_matching_runbooks`), injected into the system prompt, runbook-takes-precedence, `<placeholder>` resolved from the single Site Knowledge Base. That works for 13 first-party runbooks but breaks the "anyone authors anything, multi-cloud" goal. Changes:

**1. Storage → Postgres, not the repo.**
Move runbooks out of the image into a `runbooks` table (id, title, body_md, keywords[], target_cloud `aws|gcp|both|any`, applicable_services[], author, version, status, hit_count, miss_count). Authoring no longer needs a code deploy. Seed the table from the existing `.md` files on first boot (back-compat). File-based runbooks still load as a fallback so OSS users can ship defaults in-repo.

**2. Authoring → open to anyone, no deploy.**
Three author paths, same table: (a) Slack command `@bot runbook add <name>` → guided capture; (b) a thin web/API form; (c) a git PR to a `runbooks/` repo that syncs into Postgres (keeps review for first-party ones). A runbook is just markdown + the metadata above — no code. Validation on save: required sections, placeholder references resolve against *some* knowledge base, no hardcoded instance IDs (lint rule).

**3. Matching → three stages (keyword → RAG → LLM).**
Generalize `load_matching_runbooks` into: (a) keyword match (existing, zero-cost, wins for known patterns); (b) **semantic match via `runbook_embeddings`** (pgvector) — this is the new middle stage that makes a large community corpus usable, returning top-k by cosine similarity above a threshold; (c) LLM classification fallback (existing) only if the first two miss. Cloud filter applied throughout: a `target_cloud=gcp` runbook is only eligible for a GCP-routed investigation (or `any`/`both`).

**4. Cloud-aware placeholders.**
`<placeholder>` resolution now reads the **executor's cloud-specific knowledge base** (`knowledge-aws.md` / `knowledge-gcp.md` on each cloud's PVC) instead of one global file. A `both` runbook executes once per cloud, each resolving its own values. This is what lets one "RDS/AlloyDB CPU" runbook serve both clouds.

**5. Execution → unchanged contract, richer steps.**
Runbook content still injects into the system prompt and still drives the `todo_write` plan (the todo mirrors runbook steps — existing behavior in `prompt.py`). New: runbook steps can now reference **code-analyst** tools ("git-blame the handler in the stack trace", "deploy_diff this service") and **DB/ClickHouse** reads, so a runbook can prescribe a full metrics→logs→code path.

**6. Feedback loop → runbooks learn like patterns.**
Tie runbook `hit_count`/`miss_count` into the existing confirmation flow (the ✅/❌ Slack buttons that already feed `patterns.py`/`evidence.py`). A runbook that repeatedly leads to wrong RCAs gets auto-demoted (drops below LLM-fallback, or flagged `status=needs-review`); high-hit runbooks rank first. This makes the community corpus self-curating instead of accumulating cruft.

**Schema — two tables (many-to-many):**

```sql
-- Table 1: the runbook itself
CREATE TABLE runbooks (
  id            TEXT PRIMARY KEY,         -- slug, e.g. "rds-cpu-high"
  title         TEXT NOT NULL,
  content_md    TEXT NOT NULL,            -- the markdown body injected into the prompt
  cloud_type    TEXT NOT NULL,            -- 'aws' | 'gcp' | 'both' | 'any'
  author        TEXT,
  version       INT  DEFAULT 1,
  status        TEXT DEFAULT 'active',    -- 'active' | 'needs-review' | 'demoted'
  hit_count     INT  DEFAULT 0,
  miss_count    INT  DEFAULT 0,
  keywords      TEXT[] DEFAULT '{}',      -- for the keyword stage + fuzzy fallback
  embedding     VECTOR(1536),             -- pgvector, for the semantic stage
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);

-- Table 2: which runbooks fire for which alert (the explicit, fast path)
CREATE TABLE alert_runbook_map (
  alert_pattern TEXT NOT NULL,            -- normalized alert key or pattern
  runbook_id    TEXT NOT NULL REFERENCES runbooks(id) ON DELETE CASCADE,
  priority      INT DEFAULT 100,          -- lower = injected first when several match
  PRIMARY KEY (alert_pattern, runbook_id)
);
CREATE INDEX ON alert_runbook_map (alert_pattern);
```

**Matching — parallel hybrid recall → single rerank (NOT a short-circuit cascade).** A strict "first non-empty stage wins" cascade is rejected because an exact-map hit would hide a better semantic match. Instead, run recall mechanisms in parallel, merge, and do one rerank pass. Example incoming alert: `RDS-CPU-Production-High`, cloud=aws.

1. **Facet pre-filter:** use the alert's existing structured labels (`service`, `namespace`, `metric`, `cloud`) to shrink the candidate pool first — `WHERE cloud_type ∈ {aws,both,any} AND (service = ANY(applicable_services) OR applicable_services = '{}')`. Kills keyword-soup false positives before any fuzzy matching.
2. **Parallel recall over the filtered pool** (all three at once, not in sequence):
   - **Exact-map** — normalized alert key (`rdscpuhigh`) → `alert_runbook_map`. A hit here is a strong signal but not the final answer.
   - **Keyword/full-text** — `runbooks.keywords[]` overlap + Postgres full-text on `content_md` (today's `any(kw in alert)` logic, upgraded to FTS).
   - **Vector** — embed alert → `embedding <=> alert_vec` cosine top-k.
3. **Merge with Reciprocal Rank Fusion (RRF):** combine the three ranked lists into one candidate set (~10–15). RRF needs no score calibration across the different signals and is robust.
4. **Single LLM rerank:** one fast_model call scores the ~15 candidates against the alert (and any recon already done) → top 1–3 runbooks injected. Bounded cost: exactly one LLM call, always — never a fallback chain, never zero, never many.

**Agentic re-retrieval (the precision multiplier):** also expose `runbook_search(query)` as a **tool**. The orchestrator injects the best upfront pick, but after 2–3 recon steps the agent forms a far richer query ("RDS CPU high + deploy at 10:34 + seqscan on driver_offers") and re-searches. A query built *after* recon beats matching on the bare alert name — this is where most of the real precision comes from, because one alert name can map to many root causes.

**Self-improving:** when an injected/retrieved runbook leads to a ✅-confirmed RCA, upsert `(normalized_alert, runbook_id)` into `alert_runbook_map` and bump `hit_count` (strengthens its exact-map + RRF weight next time); ❌ bumps `miss_count` → enough misses flips `status='demoted'` and it drops out of recall. The corpus self-curates and the hot paths get cheaper.

**Why this beats the cascade:** considers all signals jointly (no early short-circuit), bounded predictable cost (one rerank call), faceted pre-filter for precision at scale, and — most importantly — the agentic tool lets retrieval use *post-recon* context instead of just the alert name. The exact-map still exists, but as one input to RRF + a confidence signal, not a short-circuit.

**One refinement on the mapping key:** don't key on the raw alert string — names vary (`RDSCpuHigh` vs `RDS-CPU-High` vs CloudWatch variants). Normalize to a stable key (lowercase, strip separators) or store a pattern, so one map row catches the variants. When a semantic/LLM match succeeds for an unmapped alert and the RCA is later confirmed ✅, **auto-insert the (normalized-alert, runbook_id) row** — the explicit map self-populates over time, so repeat alerts hit stage 1 instead of paying for stage 2/3 again.

Critical files: `config.py:load_matching_runbooks` (→ 3-stage over the two tables), `config.py:_load_runbooks` (→ Postgres-backed with file fallback), new `storage/runbooks.py` (CRUD for both tables + embedding sync + map auto-insert), `core/prompt.py` (placeholder resolution → per-cloud KB), `bot/slack.py` (authoring command + reuse of the ✅/❌ feedback handler that bumps hit/miss + inserts map rows).

---

## Two-bot model (explicit routing, not a fragile classifier)

Two Slack bots, and crucially the RCA bot triggers off the **existing escalation convention** so the team changes nothing about how they work:

- **`@Argus` — the RCA bot, triggered by the existing `@mre` escalation convention.** The team already tags `@mre` when there's an issue (the example "@mre @Arjun kindly check… iOS OA screen" did exactly this). *Mechanics note:* Slack user groups can't contain bot users, but that doesn't matter — the bot already receives every channel message (Socket Mode), so Argus simply matches the `<!subteam^…>` mention ID for `@mre` in message events. Same UX: tag `@mre` → Argus fires. **Zero new behavior for users.** Requires: the `@mre` group ID in config + Argus invited to the watched channels. A direct `@Argus` mention also works for explicit invocation.
  - **Light issue-vs-noise filter (not a full classifier):** `@mre` is occasionally tagged for FYI/thanks, not only issues. So when Argus fires via group membership it runs a cheap, high-precision check — "is this an actual problem report?" — with a strong prior toward *yes* (escalation-group context already implies an issue). Issue → investigate directly (metrics or code/config path, cloud-routed, with any screenshot/thread context). Clear non-issue (thanks/FYI) → stay quiet. This is far easier and higher-precision than a general-channel classifier because the `@mre` tag is itself a strong signal.
- **`@Sage` — unchanged.** Works exactly as today: chat + commands (`_simple_chat` persona), help, `answer_from_memory` ("what was yesterday's 5xx RCA?" → incident-RAG). Never auto-investigates. No behavior change.

Details: thread-aware — a message in an Argus investigation thread continues that session (oracle-style); 30-min cutoff (v1.1.21) guards stale CloudWatch threads. Argus honors explicit subcommands (`debug`, `oracle`). Critical files: a **second Slack app registration + token for Argus** (added to the `@mre` user group in Slack admin); `bot/slack.py` split into the Argus handler (group-triggered investigate + noise filter) and the unchanged Sage handler; reuse `_simple_chat`, the incident-RAG retriever, and the orchestrator enqueue path.

---

## Issue class: product/behavior bugs (no metric signature → code+config first)

A large fraction of real reports are not infra alerts but **product/behavior bugs** reported by a human in a channel — e.g. "iOS drivers get an Order-Acceptance screen they must manually click to accept" (often with a screenshot). These have **no Prometheus/RDS signature**; the truth lives in app code + backend config/flags. The infra-centric recon path (kubectl/prom/RDS) finds nothing. The system must handle these as a first-class path:

- **Multimodal intake.** Pass Slack image attachments into the investigation (the engine already accepts `images=` in `engine.investigate`). The agent reads the screenshot to understand the reported behavior.
- **Component → repo/area mapping.** A human report ("OA screen on iOS") names no target. Add a **component map** to the per-cloud/site knowledge base (feature/flow → repo + module + relevant config table/flag) and/or a runbook ("order-acceptance behavior → these repos, check this flag, review recent commits to this flow"). Without this, the agent can't find the right code from a vague description — this is the make-or-break for this class.
- **Code-first (and config-first) investigation.** When there's no metric signature, the agent skips recon and goes straight to: open an OpenCode read session on the mapped repo ("how is the OA screen triggered? is there an auto-accept vs manual-click flag? what changed recently?") + `git log` around onset. **Check config/feature-flags too, not just source** — a big share of these are "should be config-driven but isn't" or "a flag flipped" (exactly the pattern in this team's own logs: *"this access-token API is not based on config, so even when disabled it's still called"*). The RCA + fix may be a **config change, not a code PR** — the gate must support "the fix is: set flag X for iOS build Y" as a valid outcome, routed to the config owner rather than a draft PR.
- **Classifier nuance.** The intent router must send these to `investigate` even though they read like prose, not an alert — and route them to the code/config path, not the metrics path, based on the absence of an infra entity + presence of an app/flow/behavior description.

This is why code-deep-dive (Phase 1) + the conversational OpenCode session (Phase 3) are the differentiator: they're the *only* tools that resolve this whole class, which metrics-based RCA tools can't touch at all.

---

## Context management: what gets passed to the agent (and when)

Do NOT dump all tools + all runbooks into every request. At god-level scale (code-analyst + many DB connections → 50+ tools; hundreds of community runbooks) that wrecks both tool-selection accuracy and prompt-cache economics. Rule: **select once per investigation → keep stable across turns → allow agentic expansion on demand.**

- **Tools — curated subset, stable per investigation.** The orchestrator picks relevant toolsets from alert type + cloud (RDS alert → bash, database, prometheus, code-analyst; not mongodb/kafka/servicenow). Reuse the pattern that already exists in `sub_agents.py:_filter_tools()` — lift it into the main engine path. The subset is passed on every request (API requires it) but is **identical turn-to-turn**, so the whole tool+prompt prefix is a prompt-cache hit after turn 1. This matters given the 5-min cache TTL — swapping tools per turn would bust the cache every step.
- **Runbooks — matched top 1–3 only, injected once.** From the hybrid-recall+rerank above; placed in the system prompt at start, then cached. Never the full corpus.
- **Knowledge base — per-cloud, injected once** (`knowledge-aws.md`/`knowledge-gcp.md` by routed cloud).
- **On-demand expansion (not pre-loaded):**
  - `runbook_search(query)` tool — agent pulls more runbooks mid-investigation with a recon-enriched query.
  - `list_tools(category)` / tool-expansion request — if the agent hits a wall needing a toolset outside its curated set, it can request the category be added. Rare; keeps the default set small.
- **Net effect:** small accurate prompts, warm prompt cache across the whole investigation, and the agent can still reach anything in the catalog when it actually needs it.

---

## What NOT to do (explicit non-goals)

- **No LangChain / LangGraph.** The hand-rolled engine is lean, fully understood, and just had 6 deep bugs fixed in it. Steal LangGraph's *concept* (typed state, explicit phase transitions) as ~100 lines in `engine.py` if wanted — do not import the framework and bury control flow.
- **No auto-merge, ever.** Draft PR + human merge is the ceiling.
- **No "RAG everything."** Only incidents, code, and runbooks get vectors. Logs/metrics/transcripts do not — they're queried live, not retrieved.
- **No autonomous infra writes.** `pr_create` is the only mutation, and it touches a git branch, not prod.

---

## Verification (per phase)

- **Phase 0:** run 2 worker replicas locally (docker-compose: app×2 + Postgres + Redis); fire the same alert twice rapidly → exactly one investigation runs (Redis dedup). Existing incidents readable by both pods.
- **Phase 1:** unit-test `code_analyst` tools against a fixture repo (clone, blame a known line, deploy_diff a tagged commit). Reuse the local test pattern from `/Users/vijaygupta/misc/tmp/test_bash_select.py`.
- **Phase 2:** seed 50 past incidents, query a paraphrased new alert → incident-RAG returns the right prior (semantic) where `LIKE` misses.
- **Phase 3:** golden-set of past code-caused incidents → assert high-confidence ones produce a draft PR with the correct file in the diff; low-confidence ones produce a Slack message, no PR. Verify PR is draft and targets a feature branch.
- **Phase 4:** load-test N concurrent alerts of mixed cloud → `aws`-tagged jobs only consumed by AWS executors, `gcp` only by GCP executors, `both` fans out and the synthesizer merges; queue drains, no dedup races across clouds, heartbeats show cloud per job; kill an executor mid-job → another in the same cloud `XCLAIM`s and finishes it.
- **Phase 5 (UI):** edit a runbook in the studio → next matching alert uses the new version (no deploy); watch a live investigation stream step-by-step in the browser matching the Slack output; approve a draft PR from the UI; verify RBAC blocks a viewer from editing/approving; confirm every edit/approval is audit-logged.
- **Phase 6 (OSS):** fresh clone + Helm install on a clean cluster with example config → end-to-end on a synthetic alert with zero NY data present.

---

## Investigation state machine + retry/resume (durable jobs)

Every investigation is a **durable, resumable job** in Postgres — not a fire-and-forget in-memory coroutine (which is what `_do_investigation` is today: if the pod dies or an exception escapes, the work is lost and the alert may never re-fire). State per incident:

```sql
CREATE TABLE investigations (
  id            TEXT PRIMARY KEY,        -- = incident_id
  alert_key     TEXT, cloud TEXT,
  status        TEXT,                    -- queued|running|awaiting_fix_review|done|failed
  phase         TEXT,                    -- enrich|recon|hypothesize|verify|synthesize|fix
  step          INT,                     -- current agent step
  messages      JSONB,                   -- conversation so far (the resumable state)
  findings      JSONB,                   -- per-cloud sub-agent results
  code_session  JSONB,                   -- OpenCode session_id + transcript (reattach/restart on resume)
  attempt       INT DEFAULT 0,
  worker_id     TEXT, heartbeat_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ
);
```

- **Checkpointing:** the executor persists `messages` + `phase` + `step` at each step boundary (cheap — it's already building the messages list in `engine.py`). On crash, the new owner reloads `messages` and continues from the last step instead of restarting the whole investigation.
- **Retry via the queue, not a loop:** Redis Streams already give at-least-once delivery. If an executor dies mid-job, its messages stay in the consumer group's pending list; another same-cloud executor `XCLAIM`s the job after the visibility timeout, reads the checkpoint, and resumes. `attempt` increments; after N attempts → `status=failed` + Slack/UI alert (don't retry forever).
- **Idempotency:** keyed by `incident_id`; a re-delivered job that's already `done` is a no-op. Tool side-effects are read-only except `pr_create`, which checks "did I already open a PR for this incident?" before creating another.
- **Distinguish failure types:** transient (LLM timeout, tool hang → resume/retry) vs terminal (no root cause found, ambiguous → mark `failed`, surface to human, don't burn retries). The engine's existing retry loops handle within-step transients; this layer handles whole-job crash recovery.
- **Resumability is also what powers the UI** (Phase 5 live view reads `phase`/`step`/`messages`) and dead-job reaping (orchestrator scans stale `heartbeat_at`).

This makes "it failed → it retries" true at the job level, not just the per-LLM-call level we fixed in v1.1.22.

## Open questions, risks & gaps (resolve before/within the relevant phase)

**Blocking / decisions:**
1. **LLM gateway capacity — RESOLVED: API-key pool.** The `429 max_parallel_requests=5` limit is *per key*, and keys can be created on demand. Provision a **pool of gateway keys** sized to the fleet: simplest assignment = one key per executor pod (mounted as its secret); OpenCode sessions and sub-agents use their executor's key. A small `KeyPool` helper tracks 429s per key and rotates/backs off on saturation. Capacity scales linearly with key count; still keep per-investigation LLM-concurrency caps for sanity.
2. **Config-fix mechanism — default: tiered like code.** High confidence + the flag lives in the config repo/S3-bundle source → **draft PR to the config repo**; otherwise **propose-only** routed to the config owner ("set flag X = Y for iOS build Z"). Never a direct config-DB/S3 write. Confirm the config-repo path during Phase 3.
3. **Repo scope + GitHub access — default: GitHub App.** Scoped read on in-scope repos + PR-write for the bot; revocable + auditable. Repo list (backend monorepo, iOS/Android, config repo) finalized at Phase 1 start; sparse-checkout for the monorepo.
4. **Multimodal/vision model.** Verify a vision-capable model on the gateway early in Phase 1 (quick test like this session's model benchmark). If none, screenshot intake degrades to OCR or asking the reporter to describe — flag to Acme as a model request.
5. **Coding-model quality.** Benchmark gateway models on real past code-fixes (golden set, below) before trusting the fix path — RCA-summarization quality ≠ Haskell-fix quality. Budget for a stronger coding model (more keys / a dedicated model) if needed.

**Resolved in discussion:**
13. **Orchestrator placement — GCP, single for v1** + an external dead-man's-switch alert. Control plane (Postgres, Redis) lives in GCP; AWS runs executors only. Promote to active-passive later.
14. **Fix validation — CI validates (GitHub Actions).** Executor pods never build the Haskell monorepo (too heavy). OpenCode pushes the fix branch → GitHub Actions builds/tests → Argus reads check-run results via the GitHub App → PR flips draft→ready-for-review only on CI green. Local validation = cheap checks only (hlint / changed-module type-check).
15. **GCP observability — mirrored Prometheus/ES exists.** Same toolsets work for the GCP executor; only the endpoints differ, resolved from the per-cloud knowledge base (`knowledge-gcp.md`).
16. **Control-plane Postgres — Cloud SQL for PostgreSQL (pgvector supported) in GCP**, separate from all prod DBs (Argus must not depend on a DB it investigates).
17. **Long investigations OK** — code-deep-dive RCAs may run 20–30 min; fast-RCA (~15s) remains the early signal with progressive thread updates.
18. **Cross-cloud network — VPC peering/VPN exists.** AWS executors reach the GCP control-plane Redis/Postgres privately; Redis Streams cross-cloud as designed, no public exposure.
19. **Repo scope v1 — backend monorepo + shared-kernel + config repo + frontend/mobile repos** (iOS/Android), so product-bug classes like the OA-screen are covered from day one.
20. **History migration — migrate SQLite incidents to Postgres** (seeds incident-RAG, patterns, baselines — the learning loops keep their memory).
21. **Agent self-observability** — Argus exports its own Prometheus metrics (`investigations_{started,completed,failed}`, step latency, queue depth, token/cost per key, executor heartbeats) + Grafana dashboard; pairs with the dead-man's switch.
22. **Rollout — shadow mode.** Each phase deploys alongside the live single-pod v1.1.x; the Phase-4 topology consumes the same alerts but posts to a test channel until parity is proven, then webhook/tokens flip. No big-bang cutover.

**Secrets & security (Phase 4 + Phase 3):**
6. **Cross-cloud secrets model.** Executors need broad read creds per cloud (ClickHouse, RDS, AlloyDB, EKS/GKE, cloud APIs) + GitHub-write for PRs. Manage via k8s secrets + IRSA (AWS) / Workload Identity (GCP), not shell env vars. PR creds scoped to a bot account with draft-only + branch protection.
7. **Prompt-injection threat model.** A coding agent that edits code + opens PRs is an attack surface — a crafted log line or alert could try to steer it. Mitigations: sandboxed worktree (have), draft-only + human merge (have), branch protection, no secrets in repo context, input provenance limits. Needs an explicit threat-model doc (ties to the Phase 6 security section).
8. **PII / data retention.** Incidents store logs + DB rows + tool outputs that contain customer/driver PII (the session logs already showed `person_id`, phone-shaped data). For an OSS tool persisting this to Postgres + embeddings, need a retention policy + PII scrubbing before storage/embedding.

**Quality & correctness (cross-cutting):**
9. **Eval / confidence calibration — default: RCA calibrates live, fix path gated on a golden set.** RCA investigations ship immediately and calibrate from the ✅/❌ loop (human merge is already the safety net). The code/config **fix path specifically** requires a golden-set pass first: assemble past incidents with known root causes + known fixes, measure fix precision and confidence calibration (is "HIGH" right ≥90%?) before enabling `edit` mode. Track confidence-vs-correctness continuously thereafter.
10. **Incident correlation, not just dedup.** A big outage fires many *different* alerts at once (cascade). Dedup only catches identical ones. Need to **group related alerts into one investigation** (by time + service graph + shared entities) so the fleet doesn't run 50 competing investigations of one incident — also saves gateway tokens.
11. **"Verified root cause" definition.** Raising the checkpoint threshold to "run until verified" needs a concrete verification signal (evidence chain reproduces / explains all symptoms), else investigations run unbounded. Define it, with a hard ceiling as backstop.
12. **Component→repo/ownership map.** The product-bug path and fix routing both depend on a feature/service → repo + owning-team map (CODEOWNERS / service catalog). Without it, Argus can't find the right code or route the fix to the right human.

## Sequencing rationale

0 (shared state + durable investigation jobs) unblocks all scale and retry → 1 (code deep-dive) is the differentiator and prereq for fix → 2 (RAG) sharpens recall → 3 (gated draft-PR) is the headline feature, depends on 1 → 4 (orchestrator/workers) turns it horizontal and adds crash-resume → 5 (UI) makes it observable + configurable → 6 (OSS) ships it. Each phase is independently shippable and leaves the system working.
