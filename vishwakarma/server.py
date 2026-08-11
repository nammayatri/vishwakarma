"""
Vishwakarma FastAPI server.

Endpoints:
  POST /api/investigate      — main investigation endpoint
  POST /api/alertmanager     — AlertManager webhook (dedup + PDF + Slack)
  GET  /api/model            — list available LLM config
  GET  /api/incidents        — list incidents from storage
  GET  /api/incidents/{id}   — get incident details
  GET  /api/stats            — investigation statistics
  POST /api/checks/execute   — run a health check
  GET  /healthz              — liveness probe
  GET  /readyz               — readiness probe (toolset health)
"""
import asyncio
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

# Fingerprints currently being investigated — Redis-backed when storage.redis_url
# is configured (multi-pod safe), in-memory fallback otherwise.
# Skip only if investigation is RUNNING — released on completion; the Redis TTL
# self-clears leaked locks if a pod dies mid-investigation.
from vishwakarma.storage import dedup as _dedup

# Global concurrency limit — max simultaneous investigations.
# Alerts beyond this limit queue and wait rather than running in parallel.
# Prevents LLM rate limits, memory pressure, and tool contention under alert storms.
# Override via VK_MAX_CONCURRENT_INVESTIGATIONS env var.
MAX_CONCURRENT_INVESTIGATIONS = int(os.environ.get("VK_MAX_CONCURRENT_INVESTIGATIONS", "2"))
_investigation_semaphore: "asyncio.Semaphore | None" = None

# Shared app state (learnings, toolset_manager). MODULE-LEVEL so the Slack bots
# (Argus/Sage), which run in background threads, can import it:
#   from vishwakarma.server import _state
_state: "dict[str, Any]" = {}


def _get_semaphore():
    import asyncio
    global _investigation_semaphore
    if _investigation_semaphore is None:
        _investigation_semaphore = asyncio.Semaphore(MAX_CONCURRENT_INVESTIGATIONS)
    return _investigation_semaphore

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from vishwakarma.core.models import (
    ApprovalDecision,
    InvestigateRequest,
    InvestigationResult,
    LLMResult,
    ToolOutput,
    ToolStatus,
)
from vishwakarma.utils.log import suppress_probe_logs
from vishwakarma.utils.stream import sse_event, sse_done

log = logging.getLogger(__name__)


def _mount_console_spa(app: FastAPI) -> None:
    """
    Serve the built Argus console (web/dist) at /console with an SPA
    fallback (client-side routes → index.html). No-op when the bundle
    hasn't been built — the API still works without the UI.
    """
    import os
    from pathlib import Path
    from fastapi.responses import FileResponse

    candidates = [
        Path(os.environ.get("VK_CONSOLE_DIST", "")),       # explicit override
        Path(__file__).parent.parent / "web" / "dist",      # repo checkout
        Path("/app/web/dist"),                              # container layout
    ]
    dist = next((c for c in candidates if c and (c / "index.html").exists()), None)
    if dist is None:
        log.info("Console UI bundle not found (web/dist) — /console disabled")
        return
    index = dist / "index.html"

    @app.get("/console", include_in_schema=False)
    @app.get("/console/{path:path}", include_in_schema=False)
    async def console_spa(path: str = ""):
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and str(candidate).startswith(str(dist.resolve())):
            return FileResponse(candidate)
        return FileResponse(index)

    log.info("Console UI mounted at /console")


def create_app(config=None) -> FastAPI:
    """Create and configure the FastAPI application."""
    from vishwakarma.config import VishwakarmaConfig

    if config is None:
        config = VishwakarmaConfig.load()

    app = FastAPI(
        title="Vishwakarma",
        description="Autonomous SRE Investigation Agent",
        version="1.0.0",
        docs_url="/api/docs",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    suppress_probe_logs()

    # Use the MODULE-LEVEL _state so other threads (the Argus/Sage bots) can
    # `from vishwakarma.server import _state`. Clear it for a fresh app.
    _state.clear()

    from vishwakarma.ui.routes import create_ui_router
    app.include_router(create_ui_router(_state))
    from vishwakarma.ui.console_api import create_console_router
    app.include_router(create_console_router(config, _state))
    _mount_console_spa(app)

    @app.on_event("startup")
    async def startup():
        from vishwakarma.storage.db import init_db
        init_db(config.db_path, dsn=config.pg_dsn, embedding_dim=config.embeddings_dim)

        # Knowledge base + learnings are DB-ONLY now (the PVC keeps only the repo
        # cache). One-time migrate any PVC files into the DB, then load knowledge
        # from the DB (authoritative + shared across pods).
        try:
            from vishwakarma.storage import site_content
            if not site_content.has_knowledge() and config.knowledge:
                site_content.set_knowledge(config.knowledge, config.cloud or "")
                log.info("Migrated knowledge.md → DB (site_knowledge)")
            if not site_content.has_learnings():
                import glob as _glob
                ld = os.environ.get("VK_LEARNINGS_PATH", "/data/learnings")
                seeded = 0
                for fp in _glob.glob(os.path.join(ld, "*.md")):
                    try:
                        with open(fp) as f:
                            site_content.set_learning(os.path.basename(fp)[:-3], f.read())
                        seeded += 1
                    except Exception:
                        pass
                if seeded:
                    log.info(f"Migrated {seeded} learnings file(s) → DB (learnings)")
            db_know = site_content.get_knowledge(config.cloud or "")
            if db_know:
                config.knowledge = db_know   # DB is authoritative
                log.info(f"Knowledge loaded from DB ({len(db_know)} chars, cloud={config.cloud or 'default'})")
        except Exception as e:
            log.warning(f"Site-content DB migrate/load skipped (using file): {e}")

        _dedup.init_dedup(config.redis_url)
        from vishwakarma.core.embeddings import init_embeddings
        init_embeddings(config.embeddings_api_base, config.embeddings_api_key,
                        config.embeddings_model, config.embeddings_dim,
                        config.embeddings_provider, config.embeddings_local_model)
        from vishwakarma.core.eventbus import init_eventbus
        init_eventbus(config.redis_url)
        from vishwakarma.core.keypool import init_keypool
        init_keypool(config.llm.api_keys or ([config.llm.api_key] if config.llm.api_key else []))
        from vishwakarma.core.correlation import init_correlation
        init_correlation(config.redis_url)
        from vishwakarma.core.pr_creator import init_pr_creator
        init_pr_creator(config.github_enabled, config.github_token,
                        config.github_api_base, config.github_default_base)
        # Runbooks are DB-ONLY (authored in the DB / console). No code seeding —
        # seed_from_files no-ops gracefully when agents.json is absent. The DB
        # is the single source of truth for runbook content + match patterns,
        # so DB authoring is never overwritten on restart.
        try:
            from vishwakarma.storage.runbooks import seed_from_files
            seed_from_files()
        except Exception as seed_err:
            log.debug(f"Runbook seeding skipped (DB-only): {seed_err}")
        # Orchestrator topology: connect the job stream (requires Redis)
        if getattr(config, "role", "") == "orchestrator":
            from vishwakarma.core.jobstream import init_jobstream
            if not config.redis_url:
                raise RuntimeError("orchestrator role requires storage.redis_url")
            init_jobstream(config.redis_url)

        from vishwakarma.core.learnings import LearningsManager
        _state["learnings"] = LearningsManager()
        _state["toolset_manager"] = config.make_toolset_manager()
        _state["toolset_manager"].check_all()

        # All-in-one durable-job reaper: resume investigations orphaned by a pod
        # restart / node scale-down (the orchestrator/executor topology uses Redis
        # XAUTOCLAIM instead, so skip there).
        if getattr(config, "role", "") in ("", "all-in-one"):
            async def _reaper_loop():
                await asyncio.sleep(25)   # let startup settle
                # First sweep is aggressive: in all-in-one there's ONE pod, so any
                # 'running' not owned by THIS fresh pod is orphaned (deploy/crash)
                # — resume it within ~30s instead of waiting out the stale window.
                first = True
                while True:
                    try:
                        await _reap_orphans(config, _state, stale_seconds=15 if first else 120)
                        first = False
                    except Exception as e:
                        log.debug(f"Reaper loop: {e}")
                    await asyncio.sleep(120)   # sweep every 2 min
            asyncio.create_task(_reaper_loop())
            log.info("Durable-job reaper started (resumes orphaned investigations)")

        log.info(f"Vishwakarma server ready (role={getattr(config, 'role', '') or 'all-in-one'})")

    # ── /healthz ──────────────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    async def _root_redirect():
        # Bare URL → the console (so Pomerium's https://argus…/ lands on the UI).
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/console")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        from vishwakarma.core import metrics as m
        # Refresh queue gauges on scrape (cheap) when Redis is wired.
        try:
            from vishwakarma.core import jobstream
            for cloud in ("aws", "gcp"):
                m.set_gauge("vk_queue_depth", jobstream.depth(cloud), {"cloud": cloud})
                m.set_gauge("vk_queue_pending", jobstream.pending_count(cloud), {"cloud": cloud})
        except Exception:
            pass
        return Response(content=m.render(), media_type="text/plain; version=0.0.4")

    # ── /readyz ───────────────────────────────────────────────────────────────

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        tm = _state.get("toolset_manager")
        if not tm:
            return Response(status_code=503, content="not ready")
        return {"status": "ready", "toolsets": len(tm.active_toolsets())}

    # ── /api/investigate ──────────────────────────────────────────────────────

    @app.post("/api/investigate")
    async def investigate(request: InvestigateRequest):
        """Main investigation endpoint — runs in thread to avoid blocking event loop."""
        tm = _state.get("toolset_manager")
        if not tm:
            raise HTTPException(503, "Server not ready")

        llm = config.make_llm()
        engine = config.make_engine(llm=llm, toolset_manager=tm)

        def _run():
            return engine.investigate(
                question=request.question,
                history=request.history,
                extra_system_prompt=request.extra_system_prompt,
                images=request.images,
                files=request.files,
                runbooks=request.runbooks,
                require_approval=request.require_approval,
                approval_decisions=request.approval_decisions,
                bash_always_allow=request.bash_always_allow,
                bash_always_deny=request.bash_always_deny,
                sections_off=request.prompt_overrides,
                response_schema=request.response_schema,
            )

        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            log.error(f"Investigation failed: {e}", exc_info=True)
            raise HTTPException(500, str(e))

        return InvestigationResult(
            analysis=result.answer,
            tool_outputs=result.tool_outputs,
            history=result.messages,
            meta=result.meta,
            pending_approvals=result.pending_approvals,
        )

    # ── /api/investigate/stream ────────────────────────────────────────────────

    @app.post("/api/investigate/stream")
    async def investigate_stream(request: InvestigateRequest):
        """Streaming investigation endpoint — returns SSE.

        Runs the blocking generator in a thread and bridges events to the
        async world via a queue so the event loop stays unblocked.
        """
        tm = _state.get("toolset_manager")
        if not tm:
            raise HTTPException(503, "Server not ready")

        llm = config.make_llm()
        engine = config.make_engine(llm=llm, toolset_manager=tm)

        _SENTINEL = object()
        q: queue.Queue = queue.Queue()

        def _produce():
            try:
                for event in engine.stream_investigate(
                    question=request.question,
                    history=request.history,
                    extra_system_prompt=request.extra_system_prompt,
                    images=request.images,
                    runbooks=request.runbooks,
                    require_approval=request.require_approval,
                    approval_decisions=request.approval_decisions,
                    bash_always_allow=request.bash_always_allow,
                    bash_always_deny=request.bash_always_deny,
                ):
                    q.put(event)
            except Exception as e:
                q.put(e)
            finally:
                q.put(_SENTINEL)

        threading.Thread(target=_produce, daemon=True).start()

        async def event_stream() -> AsyncGenerator[str, None]:
            loop = asyncio.get_event_loop()
            while True:
                item = await loop.run_in_executor(None, q.get)
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    log.error(f"Stream investigation error: {item}", exc_info=True)
                    yield sse_event("error", {"message": str(item)})
                    break
                yield sse_event(item.get("type", "event"), item)
            yield sse_done()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── /api/alertmanager ─────────────────────────────────────────────────────

    @app.post("/api/alertmanager")
    async def alertmanager_webhook(request: Request):
        """
        AlertManager webhook receiver.
        Deduplicates, triggers background investigation, posts to Slack.
        """
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        from vishwakarma.plugins.channels.alertmanager.plugin import parse_alertmanager_webhook

        # Auto-resolve: AlertManager 'resolved' status → close the matching open
        # incident(s). parse_alertmanager_webhook drops resolved alerts (we don't
        # investigate them), so read them off the raw payload here.
        try:
            from vishwakarma.storage.queries import resolve_incidents_by_labels
            for a in (payload.get("alerts") or []):
                if a.get("status") == "resolved":
                    n = resolve_incidents_by_labels(a.get("labels") or {})
                    if n:
                        log.info(f"Auto-resolved {n} incident(s) — alert cleared: "
                                 f"{(a.get('labels') or {}).get('alertname')}")
        except Exception as e:
            log.debug(f"Auto-resolve on alert-clear failed: {e}")

        issues = parse_alertmanager_webhook(payload)
        if not issues:
            return {"status": "no_issues"}

        triggered = await _trigger_investigations_for_issues(config, issues)
        return {"status": "ok", "alerts": triggered}

    # ── /api/gcp-cloud-monitoring/webhook ───────────────────────────────────────

    @app.post("/api/gcp-cloud-monitoring/webhook")
    async def gcp_cloud_monitoring_webhook(request: Request, auth_token: str | None = None):
        """
        GCP Cloud Monitoring webhook receiver. Same dispatch as
        /api/alertmanager (dedup, correlation, background investigation),
        just a different alert source.

        Cloud Monitoring doesn't support IP-allowlisting for webhooks and
        requires a public endpoint, so this is gated by a shared secret
        instead — passed as ?auth_token=<secret> (the token-in-URL pattern
        Cloud Monitoring's own "webhook_tokenauth" channel type uses).
        Disabled entirely (404) unless gcp_cloud_monitoring.webhook_token is
        configured — never accepts unauthenticated traffic.
        """
        import hmac

        if not config.gcp_cm_webhook_token:
            raise HTTPException(404, "Not found")
        if not hmac.compare_digest(auth_token or "", config.gcp_cm_webhook_token):
            raise HTTPException(401, "Unauthorized")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        from vishwakarma.plugins.channels.gcp_cloud_monitoring.plugin import (
            parse_gcp_cloud_monitoring_webhook,
        )

        # Auto-resolve: a "closed" incident notification → close the matching
        # open incident(s), mirroring the AlertManager 'resolved' handling above.
        try:
            from vishwakarma.storage.queries import resolve_incidents_by_labels
            incident = payload.get("incident") or {}
            if incident.get("state") == "closed":
                labels = {"alertname": incident.get("policy_name", "")}
                n = resolve_incidents_by_labels(labels)
                if n:
                    log.info(f"Auto-resolved {n} incident(s) — GCP incident closed: "
                             f"{incident.get('policy_name')}")
        except Exception as e:
            log.debug(f"Auto-resolve on GCP incident close failed (non-fatal): {e}")

        issues = parse_gcp_cloud_monitoring_webhook(payload)
        if not issues:
            return {"status": "no_issues"}

        triggered = await _trigger_investigations_for_issues(config, issues)
        return {"status": "ok", "alerts": triggered}

    # ── /api/xyne/events ─────────────────────────────────────────────────────

    @app.post("/api/xyne/events")
    async def xyne_events(request: Request, auth_token: str | None = None):
        """
        Xyne event webhook (bot/xyne.py). Auth: HMAC-SHA256 of the raw body
        against xyne.signing_secret, sent as a single `x-xyne-signature`
        header — CONFIRMED from a live request (2026-08-11; see
        verify_xyne_signature's docstring for how this was discovered — it is
        NOT Slack's request-signing scheme, despite the naming). Falls back
        to the shared-secret-in-URL pattern (xyne.webhook_token) if no
        signing secret is configured. Disabled (404) unless at least one is set.

        Unlike the alert webhooks, this doesn't always investigate — it feeds
        the raw event into the Xyne-flavored ArgusBot, which applies the same
        trivial/noise filtering @mre/@Argus mentions get on Slack before
        deciding whether to dispatch.

        Payload shape is {"eventType": "...", "payload": {...}} — NOT Slack's
        Events API envelope. Other eventTypes (e.g. ADDITIONAL_FORM_FIELD_UPDATED
        from unrelated Xyne apps sharing this URL) are expected and ignored;
        only APP_MENTIONED matters here.
        """
        import hmac

        if not (config.xyne_signing_secret or config.xyne_webhook_token):
            raise HTTPException(404, "Not found")

        raw_body = await request.body()

        if config.xyne_signing_secret:
            from vishwakarma.plugins.relays.xyne.plugin import verify_xyne_signature
            signature = request.headers.get("x-xyne-signature", "")
            if not verify_xyne_signature(config.xyne_signing_secret, raw_body, signature):
                raise HTTPException(401, "Unauthorized")
        elif not hmac.compare_digest(auth_token or "", config.xyne_webhook_token):
            raise HTTPException(401, "Unauthorized")

        xyne_bot = _state.get("xyne_bot")
        if xyne_bot is None:
            raise HTTPException(404, "Not found")

        try:
            payload = json.loads(raw_body)
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        if isinstance(payload, dict) and payload.get("eventType") == "APP_MENTIONED":
            # TEMP DIAGNOSTIC (remove once parse_xyne_mention_event's field
            # mapping is confirmed against enough real traffic): full body
            # logged only for this event type — internal engineer mention
            # text, not customer PII like the other eventTypes hitting this
            # endpoint carry.
            log.warning(f"Xyne APP_MENTIONED raw payload: {payload!r}")

        from vishwakarma.bot.xyne import parse_xyne_mention_event
        event = parse_xyne_mention_event(payload) if isinstance(payload, dict) else {}

        # handle_message can call classify() (an LLM call) synchronously —
        # run off the event loop so a slow classification can't block it.
        loop = asyncio.get_event_loop()
        action = await loop.run_in_executor(None, xyne_bot.handle_message, event)
        return {"status": action}

    # ── /api/model ────────────────────────────────────────────────────────────

    @app.get("/api/model")
    async def get_model():
        return {
            "model": config.llm.model,
            "api_base": config.llm.api_base,
            "max_tokens": config.llm.max_tokens,
            "cluster": config.cluster_name,
        }

    # ── /api/incidents ────────────────────────────────────────────────────────

    @app.get("/api/incidents")
    async def list_incidents(
        source: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        if search:
            from vishwakarma.storage.queries import search_incidents
            return {"incidents": search_incidents(search, limit=limit)}
        from vishwakarma.storage.queries import list_incidents as _list
        return {"incidents": _list(source=source, status=status, limit=limit, offset=offset)}

    @app.get("/api/incidents/{incident_id}")
    async def get_incident(incident_id: str):
        from vishwakarma.storage.queries import get_incident as _get
        inc = _get(incident_id)
        if not inc:
            raise HTTPException(404, f"Incident {incident_id} not found")
        return inc

    @app.get("/api/stats")
    async def stats():
        from vishwakarma.storage.queries import get_stats
        return get_stats()

    # ── /api/toolsets ─────────────────────────────────────────────────────────

    @app.get("/api/toolsets")
    async def list_toolsets():
        tm = _state.get("toolset_manager")
        if not tm:
            raise HTTPException(503, "Server not ready")
        return {
            "toolsets": [
                {
                    "name": ts.name,
                    "description": getattr(ts, "description", ""),
                    "enabled": ts.enabled,
                    "health": ts.health.value if ts.health else "unknown",
                }
                for ts in tm.all_toolsets()
            ]
        }

    return app


# ── Reply destination (Slack or Xyne) ───────────────────────────────────────────

def _make_destination(config, labels: dict):
    """
    Pick SlackDestination or XyneDestination based on the issue's `platform`
    label — stamped by ArgusBot (bot/argus.py) only for mention-triggered
    issues. Alert-webhook-triggered issues (AlertManager/GCP Cloud Monitoring)
    carry no platform label and always default to Slack.
    """
    if (labels or {}).get("platform") == "xyne":
        from vishwakarma.plugins.relays.xyne.plugin import XyneDestination
        return XyneDestination({"base_url": config.xyne_base_url, "token": config.xyne_bot_token})
    from vishwakarma.plugins.relays.slack.plugin import SlackDestination
    return SlackDestination({"token": config.slack_bot_token})


def _destination_configured(config, labels: dict) -> bool:
    if (labels or {}).get("platform") == "xyne":
        return bool(config.xyne_base_url and config.xyne_bot_token)
    return config.is_slack_configured()


# ── Alert dispatch (shared by every alert-source webhook) ──────────────────────

async def _trigger_investigations_for_issues(config, issues: list) -> list[dict]:
    """
    Per-issue dispatch shared by /api/alertmanager and
    /api/gcp-cloud-monitoring/webhook: cloud filter, dedup, correlation, then
    either enqueue (orchestrator topology) or start an in-process
    investigation (all-in-one). Extracted from the original
    /api/alertmanager handler body verbatim so alert-source plumbing doesn't
    duplicate/drift between sources.
    """
    import asyncio
    from vishwakarma.storage.queries import alert_fingerprint

    triggered = []
    for issue in issues:
        fingerprint = alert_fingerprint(issue.labels)

        # Per-cloud hard filter: when this pod has its own cloud set (CLOUD=gcp
        # / aws), ONLY investigate alerts routed to that cloud — the other
        # cloud's pod handles the rest. Safety net even if an alertmanager
        # mis-points. (Empty config.cloud = all-in-one, handles everything.)
        if config.cloud:
            from vishwakarma.core.cloud_router import route_issue as _route
            alert_cloud = _route(issue, default_cloud=config.default_cloud)
            if alert_cloud != config.cloud:
                log.info(f"Skipping '{issue.title}' — routed to {alert_cloud}, "
                         f"this pod serves {config.cloud}")
                triggered.append({"title": issue.title, "status": "skipped-other-cloud",
                                  "cloud": alert_cloud})
                continue

        # Skip only if an investigation for this alert is currently running
        # (Holmes pattern). Atomic across pods when Redis is configured.
        if not _dedup.try_acquire(fingerprint):
            log.info(f"Alert deduplicated (investigation in progress): {issue.title}")
            triggered.append({"title": issue.title, "status": "deduplicated"})
            continue

        # Incident correlation: a DIFFERENT alert sharing a strong entity
        # with an active investigation (within the window) is part of the
        # same storm — group it instead of starting a competing one.
        from vishwakarma.core import correlation as _corr
        corr_key = _corr.correlation_key(issue.labels)
        parent = _corr.find_correlated(corr_key)
        if parent:
            _corr.record_correlated_alert(parent, issue.title, issue.labels)
            _corr.link(corr_key, parent)  # extend the window while the storm continues
            _dedup.release(fingerprint)   # not investigating this one
            log.info(f"Alert correlated into {parent} (key={corr_key}): {issue.title}")
            triggered.append({"title": issue.title, "status": "correlated",
                              "parent": parent})
            continue

        incident_id = str(uuid.uuid4())
        if corr_key:
            _corr.link(corr_key, incident_id)   # claim the entity window

        if getattr(config, "role", "") == "orchestrator":
            # Orchestrator topology: route to the cloud whose executors can
            # reach this alert's data plane, enqueue, done. Executors run
            # the investigation (including Slack ack) and release dedup.
            import json as _json
            from vishwakarma.core.cloud_router import route_issue
            from vishwakarma.core import jobstream
            cloud = route_issue(issue, default_cloud=config.default_cloud)
            jobstream.enqueue(cloud, {
                "incident_id": incident_id,
                "fingerprint": fingerprint,
                "cloud": cloud,
                "issue": _json.loads(issue.model_dump_json()),
            })
            triggered.append({"title": issue.title, "status": "queued",
                              "cloud": cloud, "incident_id": incident_id})
            continue

        # All-in-one topology (vk serve): investigate in-process
        asyncio.create_task(
            _run_alert_investigation(config, _state, issue, incident_id, fingerprint)
        )
        triggered.append({"title": issue.title, "status": "investigating", "incident_id": incident_id})

    return triggered


# ── Background investigation ───────────────────────────────────────────────────

async def _run_alert_investigation(config, state, issue, incident_id: str, fingerprint: str = ""):
    import asyncio

    semaphore = _get_semaphore()
    queue_pos = MAX_CONCURRENT_INVESTIGATIONS - semaphore._value
    if queue_pos >= MAX_CONCURRENT_INVESTIGATIONS:
        log.info(f"Alert queued (concurrency limit {MAX_CONCURRENT_INVESTIGATIONS} reached): {issue.title}")

    async with semaphore:
        await _do_investigation(config, state, issue, incident_id, fingerprint)


async def _resume_investigation(config, state, inv: dict) -> None:
    """Resume one orphaned investigation from its last checkpoint — continue the
    engine from the saved messages, then post + finish. Killed runs pick up where
    they stopped instead of being stranded at 'running' forever."""
    import socket
    from vishwakarma.storage.investigations import claim_investigation, finish_investigation
    from vishwakarma.storage.queries import get_incident, save_incident
    incident_id = inv["id"]
    messages = inv.get("messages")
    inc = get_incident(incident_id) or {}
    if not messages or not isinstance(messages, list):
        finish_investigation(incident_id, "failed")
        log.warning(f"Reaper: {incident_id[:8]} has no checkpoint — marked failed")
        return
    claim_investigation(incident_id, worker_id=socket.gethostname())
    log.info(f"Reaper: resuming {incident_id[:8]} from step {inv.get('step')} ({len(messages)} msgs)")
    tm = state.get("toolset_manager")
    llm = config.make_llm()
    engine = config.make_engine(llm=llm, toolset_manager=tm)
    engine.max_steps = max(int(inv.get("step") or 0) + 25, 30)
    question = inc.get("question") or inc.get("title") or "Resume investigation"
    final = ""
    from vishwakarma.core import eventbus
    try:
        for ev in engine.stream_investigate(question=question, incident_id=incident_id,
                                             resume_messages=messages):
            et = ev.get("type", "")
            # Stream resumed-investigation events to the console UI too (SSE).
            if et in ("tool_call_start", "tool_call_result", "hypothesis",
                      "compaction", "status", "done", "max_steps_reached"):
                try:
                    eventbus.publish(incident_id, {k: v for k, v in ev.items() if k != "messages"})
                except Exception:
                    pass
            if et in ("done", "max_steps_reached"):
                final = ev.get("content", "") or final
    except Exception as e:
        log.error(f"Reaper: resume failed for {incident_id[:8]}: {e}")
        finish_investigation(incident_id, "failed")
        return
    labels = inc.get("labels") or {}
    if final:
        try:
            save_incident(incident_id=incident_id, title=inc.get("title", "Resumed RCA"),
                          question=question, analysis=final, source=inc.get("source", ""),
                          severity=inc.get("severity", "high"), labels=labels, tool_outputs=[])
        except Exception as e:
            log.debug(f"Reaper: save failed: {e}")
        ch = labels.get("slack_channel")
        if _destination_configured(config, labels) and ch:
            # Resumed RCAs get a PDF too — without this the repost always
            # falls back to a wall of text in the thread.
            pdf_path = None
            try:
                from vishwakarma.bot.pdf import generate_pdf
                pdf_path = generate_pdf(
                    title=f"{inc.get('title', 'RCA')} (resumed)", analysis=final,
                    source=inc.get("source", ""), severity=inc.get("severity", "high"))
            except Exception as e:
                log.warning(f"Reaper: PDF generation failed, posting text: {e}")
            try:
                _make_destination(config, labels).post_investigation(
                    title=f"{inc.get('title', 'RCA')} (resumed)", analysis=final,
                    source=inc.get("source", ""), severity=inc.get("severity", "high"),
                    incident_id=incident_id, thread_ts=labels.get("slack_thread_ts"), channel=ch,
                    pdf_path=pdf_path)
            except Exception as e:
                log.debug(f"Reaper: slack post failed: {e}")
    finish_investigation(incident_id, "done")
    log.info(f"Reaper: {incident_id[:8]} resumed + completed")


async def _reap_orphans(config, state, stale_seconds: int = 120) -> None:
    """Find investigations whose worker stopped heartbeating (pod died / scaled down)
    and resume them. Skips ones the CURRENT pod is actively running."""
    import socket
    from vishwakarma.storage.investigations import find_orphaned
    try:
        # Off the event loop — this is a network DB call and the sweep runs
        # every 120s; blocking here stalls webhook handling.
        loop = asyncio.get_event_loop()
        orphans = await loop.run_in_executor(
            None, lambda: find_orphaned(stale_seconds=stale_seconds, limit=10))
    except Exception as e:
        log.debug(f"Reaper: scan failed: {e}")
        return
    me = socket.gethostname()
    import threading
    for inv in orphans:
        if inv.get("worker_id") == me:
            continue   # our own (possibly slow) live run — not an orphan
        log.info(f"Reaper: orphan {inv['id'][:8]} (worker={inv.get('worker_id')}, step={inv.get('step')})")
        # Run the resume in a BACKGROUND THREAD. The engine loop is synchronous;
        # awaiting it here would block the event loop and fail the /healthz
        # liveness probe (→ restart loop). claim_investigation re-owns it to this
        # pod immediately, so the next sweep won't double-resume it.
        def _bg(inv=inv):
            try:
                asyncio.run(_resume_investigation(config, state, inv))
            except Exception as e:
                log.error(f"Reaper: error resuming {inv['id'][:8]}: {e}")
        threading.Thread(target=_bg, daemon=True).start()


def _extract_pr_url(tool_outputs) -> str:
    """Find a github PR url in the propose_fix tool output (if a draft PR was opened)."""
    import re as _re
    pat = _re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
    for o in (tool_outputs or []):
        text = getattr(o, "output", "") or ""
        if getattr(o, "tool_name", "") == "propose_fix" or "/pull/" in text:
            m = pat.search(text)
            if m:
                return m.group(0)
    return ""


async def _do_investigation(config, state, issue, incident_id: str, fingerprint: str = "",
                            cross_cloud: str = "", cross_cloud_base: str = ""):
    """
    cross_cloud: when set ('aws'|'gcp'), this is one half of a `both`-cloud
    investigation. Individual Slack posting is suppressed; the RCA is written
    to cross_cloud_findings under `cross_cloud_base`, and the second half to
    finish synthesizes + posts ONE unified RCA.
    """
    import asyncio

    tm = state.get("toolset_manager")
    llm = config.make_llm()
    engine = config.make_engine(llm=llm, toolset_manager=tm)

    # Scale investigation depth by alert severity, capped at config.max_steps
    # (VK_MAX_STEPS / config max_steps — set to 100). Critical/high get the full
    # budget; lower severities a proportion so cheap cases still finish fast.
    _m = config.max_steps
    _severity_steps = {"critical": _m, "high": _m,
                       "warning": max(40, int(_m * 0.7)), "medium": max(40, int(_m * 0.7)),
                       "low": max(25, int(_m * 0.5)), "info": max(20, int(_m * 0.4))}
    engine.max_steps = _severity_steps.get((issue.severity or "").lower(), _m)

    try:
        question = issue.question()
        alert_name = issue.labels.get("alertname") or issue.title

        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        from vishwakarma.config import load_matching_runbooks

        # ── Post immediate acknowledgment to Slack ──
        ack_ts = None
        slack_channel_id = None
        slack_client = None
        # When the issue was reported in Slack (Argus @mre/@Argus), answer IN
        # that channel/thread instead of the default alert channel — the
        # reporter expects the RCA where they raised it.
        report_channel = issue.labels.get("slack_channel") or ""
        report_thread = issue.labels.get("slack_thread_ts") or ""
        # Cross-cloud halves don't post individually — the synthesizer posts
        # one unified RCA.
        if _destination_configured(config, issue.labels) and not cross_cloud:
            try:
                dest = _make_destination(config, issue.labels)
                slack_client = dest._get_client()
                if report_channel:
                    slack_channel_id = report_channel        # already a channel id from the event
                else:
                    slack_channel_id = dest._resolve_channel_id(
                        os.environ.get("SLACK_CHANNEL", "#sre-alerts")
                    )
                severity_color = "#FF0000" if (issue.severity or "").lower() in ("critical", "high") else "#FFA500"
                ack_text = f":rotating_light: Investigating: {issue.title}"
                ack_blocks = [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": f":rotating_light: {issue.title}"[:148], "emoji": True},
                    },
                    {"type": "divider"},
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": ":hourglass_flowing_sand: _Investigation in progress — full RCA with PDF will follow in this thread..._"}],
                    },
                ]
                # Thread the ack under the original report when known.
                ack_kwargs = {"channel": slack_channel_id, "text": ack_text,
                              "attachments": [{"color": severity_color, "blocks": ack_blocks}]}
                if report_thread:
                    ack_kwargs["thread_ts"] = report_thread
                resp = slack_client.chat_postMessage(**ack_kwargs)
                ack_ts = resp["ts"]
                # chat.postMessage accepts a #name and returns the real channel
                # ID — use it for everything downstream (files_upload_v2 needs
                # an ID and the token has no channels:read scope to resolve one).
                slack_channel_id = resp.get("channel") or slack_channel_id
            except Exception as e:
                log.warning(f"Slack ack failed (non-fatal): {e}")

        # Live phase status in the thread from second zero — updated at each
        # pre-investigation phase, then reused by the streaming loop. Without
        # this the thread is silent through enrichment + pattern check +
        # sub-agent scan, which can take minutes on broad alerts.
        phase_ts = None

        def _phase(text: str) -> None:
            nonlocal phase_ts
            if not (slack_client and slack_channel_id and ack_ts):
                return
            try:
                blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]
                if phase_ts:
                    slack_client.chat_update(channel=slack_channel_id, ts=phase_ts,
                                             text=text, blocks=blocks)
                else:
                    r = slack_client.chat_postMessage(channel=slack_channel_id, thread_ts=ack_ts,
                                                      text=text, blocks=blocks)
                    phase_ts = r["ts"]
            except Exception as e:
                log.debug(f"Phase status update failed: {e}")

        _phase(":mag: _Gathering context — cluster state, prior incidents, runbooks..._")

        triage_future = None
        if config.fast_triage_enabled:
            from vishwakarma.core.fast_triage import run_fast_triage_staged

            def _post_triage_stage(stage_name: str, summary_text: str) -> None:
                if not (slack_client and slack_channel_id and ack_ts):
                    return
                try:
                    slack_client.chat_postMessage(
                        channel=slack_channel_id, thread_ts=ack_ts,
                        text=summary_text,
                        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": summary_text}}],
                    )
                except Exception as e:
                    log.debug(f"Fast triage Slack post failed ({stage_name}, non-fatal): {e}")

            # Runs the 4-stage triage (Istio -> Release Monitoring -> DB/Redis
            # -> pod CPU/Mem) in a background thread, posting one Slack
            # message per stage as it completes. Not awaited here — Slack
            # narration is never delayed by the critical path below. Its
            # findings ARE awaited later, but only for a short bounded grace
            # window (config.fast_triage_evidence_wait_seconds), so they can
            # seed the deep investigation without the investigation start
            # itself being able to hang on a slow/stuck triage run.
            triage_future = loop.run_in_executor(
                None, run_fast_triage_staged, issue, tm, llm, _post_triage_stage,
                config.fast_triage_timeout_seconds, config.fast_triage_top_n,
                config.fast_triage_namespace_exclude,
            )

        # Run the 4 pre-enrichment tasks in parallel
        prefetch_future = loop.run_in_executor(None, _prefetch_alert_context, issue)
        prior_future = loop.run_in_executor(None, _build_prior_context, issue)
        entities_future = loop.run_in_executor(None, _extract_alert_entities, issue, llm)
        # Alert's cloud label, defaulting to THIS instance's cloud — each
        # deployment (VK_CLOUD=aws|gcp) is independent and investigates only
        # its own cloud, so unlabeled alerts get the instance's facet.
        _cloud = issue.labels.get("cloud", "") or config.cloud or ""

        def _match_runbooks_once() -> tuple[list[str], list[str]]:
            """One hybrid retrieval for both content and ids (was run twice),
            with the cloud facet applied."""
            try:
                from vishwakarma.core.runbook_match import match_runbooks
                matched = match_runbooks(alert_name, cloud=_cloud, llm=llm)
                if matched:
                    return ([f"# Runbook: {m['title']}\n\n{m['content_md']}" for m in matched],
                            [m["id"] for m in matched])
            except Exception as e:
                log.debug(f"Hybrid runbook match failed: {e}")
            try:
                return load_matching_runbooks(alert_name, llm), []
            except Exception:
                return [], []

        runbooks_future = loop.run_in_executor(None, _match_runbooks_once)

        prefetch_ctx, prior_ctx, entities_ctx, _rb_result = await _asyncio.gather(
            prefetch_future, prior_future, entities_future, runbooks_future
        )
        matched_runbooks, matched_runbook_ids = _rb_result

        # Pre-inject learnings relevant to this alert
        learnings_mgr = state.get("learnings")
        learnings_ctx = learnings_mgr.for_alert(alert_name) if learnings_mgr else ""

        # Merge all pre-investigation context into extra_system_prompt
        extra_parts = [p for p in [entities_ctx, prefetch_ctx, prior_ctx, learnings_ctx] if p]

        extra_parts.append(
            "## Learned Knowledge\n"
            "Relevant facts from past incidents are pre-injected above (if any). "
            "Use `learnings_list` + `learnings_read` only if you need categories not shown above."
        )
        extra_system_prompt = "\n\n".join(extra_parts) or None

        # Fast-RCA and its evidence-driven auto-resolve were removed —
        # preliminary classifications were too often wrong. Every alert now
        # gets the full investigation (pattern replay below still short-cuts
        # CONFIRMED patterns, which are human-validated).
        auto_resolved = False

        _phase(":repeat: _Context gathered — checking confirmed incident patterns..._")

        # ── Pattern replay: check if a confirmed pattern matches ──
        pattern_matched = False
        try:
            from vishwakarma.storage.patterns import get_patterns_for_alert, replay_pattern, mark_pattern_hit, mark_pattern_miss
            patterns = await loop.run_in_executor(
                None, lambda: get_patterns_for_alert(alert_name)
            )
            if patterns:
                # Try the most confirmed pattern first
                best = patterns[0]
                log.info(f"Found pattern for {alert_name}: {best['root_cause_type']} (hit_count={best['hit_count']})")

                # Post pattern replay status
                if slack_client and slack_channel_id and ack_ts:
                    try:
                        slack_client.chat_postMessage(
                            channel=slack_channel_id, thread_ts=ack_ts,
                            text=f":brain: Known pattern found: *{best['root_cause_type']}* (confirmed {best['hit_count']}x). Replaying investigation steps...",
                            blocks=[{"type": "context", "elements": [
                                {"type": "mrkdwn", "text": f":brain: _Known pattern: *{best['root_cause_type']}* (confirmed {best['hit_count']}x) — replaying {len(best['investigation_steps'])} steps..._"}
                            ]}],
                        )
                    except Exception:
                        pass

                validation = await loop.run_in_executor(
                    None, lambda: replay_pattern(best, engine.executor, llm, question)
                )
                if validation and validation.get("matched") and validation.get("confidence") in ("high", "medium"):
                    pattern_matched = True
                    mark_pattern_hit(best["id"], incident_id)
                    # Build instant RCA from pattern
                    analysis = (
                        f"## Root Cause\n{validation.get('root_cause', best['root_cause_detail'])}\n\n"
                        f"## Confidence: {validation.get('confidence', 'medium').upper()}\n"
                        f"Known pattern (confirmed {best['hit_count'] + 1}x). "
                        f"Root cause type: {best['root_cause_type']}\n\n"
                        f"## Evidence\n{validation.get('evidence', 'Pattern matched')}\n\n"
                        f"## Differences from Previous\n{validation.get('differences', 'None')}\n\n"
                        f"## Immediate Fix\n{best.get('fix', 'See previous incidents')}\n\n"
                        f"## Investigation Method\nPattern replay — {len(best['investigation_steps'])} targeted tool calls instead of full investigation.\n"
                        f"Previously confirmed on: {time.strftime('%Y-%m-%d', time.localtime(best['last_seen']))}"
                    )
                    log.info(f"Pattern matched for {alert_name}: {best['root_cause_type']} — skipping full investigation")
                    _phase(f":white_check_mark: _Matched confirmed pattern `{best['root_cause_type']}` — replayed targeted checks, RCA follows..._")

                    # Post match result
                    if slack_client and slack_channel_id and ack_ts:
                        try:
                            slack_client.chat_postMessage(
                                channel=slack_channel_id, thread_ts=ack_ts,
                                text=f":white_check_mark: Pattern matched! {validation.get('root_cause', '')}",
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": f":white_check_mark: _Pattern matched ({validation.get('confidence', '?')} confidence) — instant RCA generated_"}
                                ]}],
                            )
                        except Exception:
                            pass

                    # Create result object for PDF + Slack posting
                    from vishwakarma.core.models import LLMResult, InvestigationMeta
                    result = LLMResult(
                        answer=analysis,
                        tool_outputs=[],
                        messages=[],
                        meta=InvestigationMeta(steps=len(best["investigation_steps"])),
                    )
                else:
                    # Pattern didn't match current data
                    if validation:
                        mark_pattern_miss(best["id"])
                    log.info(f"Pattern did not match for {alert_name} — falling back to full investigation")
                    if slack_client and slack_channel_id and ack_ts:
                        try:
                            slack_client.chat_postMessage(
                                channel=slack_channel_id, thread_ts=ack_ts,
                                text=":x: Pattern didn't match current data — running full investigation",
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": ":x: _Pattern didn't match current data — different root cause. Running full investigation..._"}
                                ]}],
                            )
                        except Exception:
                            pass
        except Exception as e:
            log.debug(f"Pattern check failed (non-fatal): {e}")

        # ── Sub-agent parallel investigation for broad alerts ──
        # When no runbook matched and sub-agents are enabled, spawn parallel
        # domain-specific sub-agents to gather data before the main loop.
        sub_agent_findings_text = None
        if (not auto_resolved and not pattern_matched
                and not matched_runbooks and config.sub_agents_enabled):
            try:
                from vishwakarma.core.sub_agents import run_sub_agents, select_domains, format_sub_agent_findings
                from vishwakarma.core.tools import ToolExecutor

                labels = issue.labels or {}
                namespace = (
                    labels.get("namespace")
                    or labels.get("kubernetes_namespace")
                    or labels.get("exported_namespace")
                    or "atlas"
                )
                domains = select_domains(alert_name, labels)

                if domains:
                    log.info(f"Launching sub-agents for {alert_name}: {domains}")
                    _phase(f":brain: _Parallel domain scan running: {', '.join(d.upper() for d in domains)} — deep investigation starts when it completes..._")

                    # Build a ToolExecutor from the toolset manager for sub-agents
                    sub_executor = ToolExecutor(toolsets=tm.active_toolsets())

                    findings = await loop.run_in_executor(
                        None,
                        lambda: run_sub_agents(
                            alert_context=question,
                            namespace=namespace,
                            domains=domains,
                            llm_config=config.llm,
                            toolset_manager=sub_executor,
                        ),
                    )

                    if findings:
                        sub_agent_findings_text = format_sub_agent_findings(findings)
                        log.info(f"Sub-agents returned {len(findings)} domain findings for {alert_name}")

                        if slack_client and slack_channel_id and ack_ts:
                            try:
                                domain_statuses = []
                                for domain, summary in findings.items():
                                    # Extract STATUS line from findings
                                    status = "unknown"
                                    for line in summary.split("\n"):
                                        if line.strip().upper().startswith("STATUS:"):
                                            status = line.split(":", 1)[1].strip().lower()
                                            break
                                    emoji = ":white_check_mark:" if status == "healthy" else ":warning:" if status == "degraded" else ":x:" if status == "critical" else ":grey_question:"
                                    domain_statuses.append(f"{emoji} *{domain.upper()}*: {status}")
                                status_text = "\n".join(domain_statuses)
                                slack_client.chat_postMessage(
                                    channel=slack_channel_id, thread_ts=ack_ts,
                                    text=f"Sub-agent results:\n{status_text}",
                                    blocks=[{"type": "context", "elements": [
                                        {"type": "mrkdwn", "text": f":brain: _Sub-agent parallel scan complete:_\n{status_text}"}
                                    ]}],
                                )
                            except Exception:
                                pass
            except Exception as e:
                log.warning(f"Sub-agent investigation failed (non-fatal, continuing with main investigation): {e}")

        # ── Fold fast-triage findings into pre-investigation evidence ──
        # Give the staged triage pipeline a bounded grace window to finish so
        # its findings can seed the deep investigation (as pre-investigation
        # evidence alongside any matched runbook) — often already done by
        # this point since pattern-check + sub-agents just ran. Slack
        # narration from fast_triage keeps posting regardless of this wait,
        # via its own callback; only prompt-seeding is gated here.
        if triage_future is not None:
            try:
                done, _pending = await _asyncio.wait(
                    {triage_future}, timeout=config.fast_triage_evidence_wait_seconds
                )
                if triage_future in done:
                    fast_triage_evidence = await triage_future
                    if fast_triage_evidence:
                        evidence_block = (
                            f"## Automated Pre-Investigation Findings (Fast Triage)\n\n{fast_triage_evidence}"
                        )
                        sub_agent_findings_text = (
                            f"{evidence_block}\n\n{sub_agent_findings_text}"
                            if sub_agent_findings_text else evidence_block
                        )
                else:
                    async def _finish_triage_in_background(fut) -> None:
                        try:
                            await fut
                        except Exception as e:
                            log.warning(f"Fast triage failed (non-fatal): {e}")
                    _asyncio.ensure_future(_finish_triage_in_background(triage_future))
            except Exception as e:
                log.warning(f"Fast triage evidence wait failed (non-fatal): {e}")

        # ── Streaming investigation with real-time Slack updates ──
        # Same style as the Slack "debug" path: small context blocks,
        # real-time tool call start/result, yellow status message.

        def _run_streaming_investigation():
            """Run stream_investigate() with live Slack tool-by-tool updates."""
            status_ts = None
            tool_lines: list[str] = []
            analysis = ""

            def _short_params(params: dict) -> str:
                """Shorten params for display."""
                if not params:
                    return ""
                val = str(next(iter(params.values()), ""))
                return val[:50].replace("\n", " ")

            # Reuse the pre-investigation phase message as the live status line
            # (falls back to posting a fresh one if none was created).
            log.info(f"Streaming investigation: slack_client={bool(slack_client)} channel={slack_channel_id} ack_ts={ack_ts}")
            if slack_client and slack_channel_id and ack_ts:
                try:
                    txt = ":hourglass: Starting deep investigation..."
                    blocks = [{"type": "context", "elements": [
                        {"type": "mrkdwn", "text": ":hourglass: _Starting deep investigation..._"}
                    ]}]
                    if phase_ts:
                        slack_client.chat_update(channel=slack_channel_id, ts=phase_ts,
                                                 text=txt, blocks=blocks)
                        status_ts = phase_ts
                    else:
                        resp = slack_client.chat_postMessage(
                            channel=slack_channel_id, thread_ts=ack_ts,
                            text=txt, blocks=blocks,
                        )
                        status_ts = resp["ts"]
                    log.info(f"Status message posted: ts={status_ts}")
                except Exception as e:
                    log.warning(f"Status message failed: {e}")

            # Throttled, non-blocking Slack status updates. The old code did a
            # blocking chat_update per tool event (~2 per tool x ~5 tools x N
            # steps = hundreds of 200-500ms round-trips inside the event loop,
            # frequently 429'd). Latest-wins queue drained by one worker at
            # ~1 update/1.2s (chat.update allows ~1/s/channel).
            from concurrent.futures import ThreadPoolExecutor as _TPE
            _slack_pool = _TPE(max_workers=1)
            _slack_state: dict = {"pending": None, "inflight": False}

            def _drain_status() -> None:
                import time as _t
                while True:
                    text = _slack_state["pending"]
                    if text is None:
                        break
                    _slack_state["pending"] = None
                    try:
                        slack_client.chat_update(
                            channel=slack_channel_id, ts=status_ts, text=text,
                            blocks=[{"type": "context", "elements": [
                                {"type": "mrkdwn", "text": text}
                            ]}],
                        )
                    except Exception:
                        pass
                    _t.sleep(1.2)
                _slack_state["inflight"] = False

            def _post_status(text: str) -> None:
                if not (slack_client and status_ts):
                    return
                _slack_state["pending"] = text
                if not _slack_state["inflight"]:
                    _slack_state["inflight"] = True
                    _slack_pool.submit(_drain_status)

            # Durable job: create + claim so the conversation checkpoints to
            # the investigations table at every step (crash-resumable).
            try:
                from vishwakarma.storage.investigations import (
                    create_investigation, claim_investigation,
                )
                import socket
                create_investigation(
                    incident_id,
                    alert_key=issue.labels.get("alertname") or issue.title,
                )
                claim_investigation(incident_id, worker_id=socket.gethostname())
            except Exception as inv_err:
                log.warning(f"Durable-job setup failed (continuing without): {inv_err}")

            from vishwakarma.core import eventbus, metrics
            metrics.inc("vk_investigations_started_total",
                        labels={"cloud": issue.labels.get("cloud", "") or "none"})
            eventbus.publish(incident_id, {
                "type": "investigation_started", "title": issue.title,
                "severity": issue.severity, "source": issue.source,
            })

            # Multimodal: fetch any reported screenshots (Slack url_private
            # needs bot-token auth) → data URLs for vision-capable models.
            investigation_images = _fetch_issue_images(issue, config)

            # Curated tool subset for this alert (stable across the run →
            # prompt-cache friendly + better tool selection).
            tool_subset = None
            try:
                from vishwakarma.core.tool_selection import select_toolset_names
                from vishwakarma.storage.tool_effectiveness import top_toolsets
                from vishwakarma.storage.runbooks import normalize_alert_key
                available = {ts.name for ts in engine.executor.toolsets if ts.enabled}
                sel_text = f"{alert_name} {issue.title} {issue.description or ''}"
                learned = top_toolsets(normalize_alert_key(alert_name))
                tool_subset = select_toolset_names(sel_text, available, learned=learned)
            except Exception:
                pass

            for event in engine.stream_investigate(
                question=question,
                runbooks=matched_runbooks or None,
                extra_system_prompt=extra_system_prompt,
                pre_investigation_findings=sub_agent_findings_text,
                incident_id=incident_id,
                images=investigation_images or None,
                tool_subset=tool_subset,
            ):
                etype = event.get("type", "")
                # Fan out to the console UI (SSE) — fire-and-forget.
                if etype in ("tool_call_start", "tool_call_result", "hypothesis",
                             "compaction", "status", "done", "max_steps_reached"):
                    eventbus.publish(incident_id, {
                        k: v for k, v in event.items() if k != "messages"
                    })

                if etype == "tool_call_start":
                    tool = event.get("tool", "")
                    params = event.get("params", {})
                    param_str = _short_params(params)
                    tool_lines.append(f":gear: `{tool}({param_str})`")
                    _post_status("\n".join(tool_lines[-10:]))

                elif etype == "tool_call_result":
                    status = event.get("status", "")
                    marker = ":white_check_mark:" if status == "success" else ":x:"
                    tool_name = event.get("tool", "")
                    for i in range(len(tool_lines) - 1, -1, -1):
                        if tool_name and f"`{tool_name}(" in tool_lines[i] and ":white_check_mark:" not in tool_lines[i] and ":x:" not in tool_lines[i]:
                            tool_lines[i] = tool_lines[i] + f" {marker}"
                            break
                    _post_status("\n".join(tool_lines[-10:]))

                elif etype == "compaction":
                    tool_lines.append(":compression: _context compacted_")

                elif etype == "max_steps_reached":
                    analysis = event.get("content", "") or "Investigation reached max steps."

                elif etype == "done":
                    analysis = event.get("content", "") or analysis

            # Finalize status message (direct — after stopping the drain worker
            # so a stale queued update can't overwrite the final state)
            tool_count = len([t for t in tool_lines if ":gear:" in t])
            _slack_state["pending"] = None
            _slack_pool.shutdown(wait=True)
            if slack_client and status_ts:
                try:
                    final_text = "\n".join(tool_lines[-10:]) + f"\n:white_check_mark: _Done — {tool_count} tools_"
                    slack_client.chat_update(
                        channel=slack_channel_id, ts=status_ts, text=final_text,
                        blocks=[{"type": "context", "elements": [
                            {"type": "mrkdwn", "text": final_text}
                        ]}],
                    )
                except Exception:
                    pass

            # Durable job: terminal state
            try:
                from vishwakarma.storage.investigations import finish_investigation
                finish_investigation(incident_id, "done")
                from vishwakarma.core import metrics
                metrics.inc("vk_investigations_completed_total")
            except Exception:
                pass

            # Build a result-like object for the rest of the flow
            from vishwakarma.core.models import LLMResult, InvestigationMeta
            return LLMResult(
                answer=analysis,
                tool_outputs=[],
                messages=[],
                meta=InvestigationMeta(steps=tool_count),
            )

        if not auto_resolved and not pattern_matched:
            result = await loop.run_in_executor(None, _run_streaming_investigation)
    except Exception as e:
        log.error(f"Alert investigation failed for {issue.title}: {e}", exc_info=True)
        try:
            from vishwakarma.storage.investigations import finish_investigation
            finish_investigation(incident_id, "failed")
            from vishwakarma.core import metrics
            metrics.inc("vk_investigations_failed_total")
        except Exception:
            pass
        if fingerprint:
            _dedup.release(fingerprint)
        return

    analysis = result.answer or "(no analysis)"
    meta = result.meta.model_dump() if result.meta else {}
    meta["matched_runbook_ids"] = matched_runbook_ids  # for ✅/❌ runbook credit

    # ── Cross-cloud half: write this cloud's finding; the second finisher
    #    synthesizes both into one unified RCA and posts it once. ──
    if cross_cloud and cross_cloud_base:
        from vishwakarma.core import cross_cloud as cc
        cc.write_finding(cross_cloud_base, cross_cloud, analysis, meta)
        # Persist this half's incident record (for the console), then decide
        # whether we synthesize.
        try:
            from vishwakarma.storage.queries import save_incident
            save_incident(incident_id=incident_id, title=f"{issue.title} [{cross_cloud}]",
                          question=question, analysis=analysis, source=issue.source,
                          severity=issue.severity, labels=issue.labels,
                          tool_outputs=[o.model_dump() for o in result.tool_outputs], meta=meta)
        except Exception as e:
            log.warning(f"Cross-cloud half save failed: {e}")
        try:
            from vishwakarma.storage.investigations import finish_investigation
            finish_investigation(incident_id, "done")
        except Exception:
            pass
        if fingerprint:
            _dedup.release(fingerprint)
        if cc.both_present(cross_cloud_base):
            import socket
            if cc.claim_synthesis(cross_cloud_base, socket.gethostname()):
                await _synthesize_and_post_cross_cloud(config, issue, cross_cloud_base)
        return

    # Generate PDF
    pdf_path = None
    try:
        from vishwakarma.bot.pdf import generate_pdf
        pdf_path = generate_pdf(
            title=issue.title,
            analysis=analysis,
            source=issue.source,
            severity=issue.severity,
            tool_outputs=[o.model_dump() for o in result.tool_outputs],
            meta=meta,
        )
    except Exception as e:
        log.warning(f"PDF generation failed: {e}")

    # Update ack message to show completion
    if slack_client and slack_channel_id and ack_ts:
        try:
            severity_color = "#36a64f"  # green for completed
            slack_client.chat_update(
                channel=slack_channel_id, ts=ack_ts,
                text=f":white_check_mark: RCA complete for {issue.title}",
                attachments=[{"color": severity_color, "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": f":white_check_mark: {issue.title}"[:148], "emoji": True}},
                    {"type": "divider"},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": ":thread: _Investigation complete. See thread for full RCA report + PDF._"}]},
                ]}],
            )
        except Exception as e:
            log.debug(f"Ack update failed (non-fatal): {e}")

    # Post to Slack (or Xyne) — thread reply if fast RCA was posted, otherwise new message
    slack_ts = None
    if _destination_configured(config, issue.labels):
        try:
            dest = _make_destination(config, issue.labels)
            resp = dest.post_investigation(
                title=issue.title,
                analysis=analysis,
                source=issue.source,
                severity=issue.severity,
                pdf_path=pdf_path,
                incident_id=incident_id,
                thread_ts=ack_ts,
                channel=slack_channel_id or None,   # report channel for Argus issues
            )
            slack_ts = resp.get("ts")
            # If a draft PR was opened during the fix step, surface it
            # prominently in the thread (the RCA PDF is already attached above).
            # The streamed path leaves result.tool_outputs empty, so also look in
            # the RCA analysis text (propose_fix writes the PR link into it).
            pr_url = _extract_pr_url(result.tool_outputs)
            if not pr_url:
                import re as _re
                _m = _re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", analysis or "")
                if _m:
                    pr_url = _m.group(0)
            if pr_url and (slack_ts or ack_ts):
                try:
                    dest._get_client().chat_postMessage(
                        channel=resp.get("channel") or slack_channel_id,
                        thread_ts=slack_ts or ack_ts,
                        text=f":memo: I opened a *draft PR* with the fix: {pr_url}",
                        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": (
                            f":memo: *Draft PR opened with the proposed fix*\n{pr_url}\n\n"
                            "_It's a *draft* — I can't merge. Please compile/review it "
                            "(CI will build + test the branch), then merge if it looks good._"
                        )}}],
                    )
                except Exception as e:
                    log.debug(f"PR-link post failed (non-fatal): {e}")
        except Exception as e:
            log.warning(f"Slack notification failed: {e}")

    # Save to DB
    try:
        from vishwakarma.storage.queries import save_incident
        save_incident(
            incident_id=incident_id,
            title=issue.title,
            question=question,
            analysis=analysis,
            source=issue.source,
            severity=issue.severity,
            labels=issue.labels,
            tool_outputs=[o.model_dump() for o in result.tool_outputs],
            meta=meta,
            slack_ts=slack_ts,
            pdf_path=pdf_path,
        )
        # Index for semantic recurrence lookup (best-effort, no-op when
        # embeddings are unconfigured).
        try:
            from vishwakarma.core.embeddings import get_client
            from vishwakarma.storage.vectors import upsert_embedding
            emb = get_client()
            if emb.configured:
                vec = emb.embed_one(f"{issue.title}\n{question[:500]}\n{analysis[:3000]}")
                if vec:
                    upsert_embedding("incident", incident_id, vec)
        except Exception as idx_err:
            log.debug(f"Incident embedding skipped: {idx_err}")
    except Exception as e:
        log.warning(f"DB save failed: {e}")

    # Release the dedup lock — next firing of this alert will trigger a fresh investigation
    if fingerprint:
        _dedup.release(fingerprint)
        log.info(f"Investigation complete for {issue.title} — dedup lock released")
    # Close the correlation window so a genuinely new problem on the same
    # entity right after resolution starts a fresh investigation.
    try:
        from vishwakarma.core import correlation as _corr
        _corr.unlink(_corr.correlation_key(issue.labels))
    except Exception:
        pass


async def _synthesize_and_post_cross_cloud(config, issue, base_incident_id: str) -> None:
    """Merge both clouds' findings into one RCA, post it, save the unified incident."""
    from vishwakarma.core import cross_cloud as cc
    findings = cc.get_findings(base_incident_id)
    llm = config.make_llm()
    unified = cc.synthesize(llm, issue.title, findings)

    # PDF + Slack (report channel/thread when this came from Argus)
    pdf_path = None
    try:
        from vishwakarma.bot.pdf import generate_pdf
        pdf_path = generate_pdf(title=f"{issue.title} (cross-cloud)", analysis=unified,
                                source=issue.source, severity=issue.severity,
                                tool_outputs=[], meta={"cross_cloud": True})
    except Exception as e:
        log.warning(f"Cross-cloud PDF failed: {e}")

    slack_ts = None
    if _destination_configured(config, issue.labels):
        try:
            dest = _make_destination(config, issue.labels)
            channel = issue.labels.get("slack_channel") or None
            resp = dest.post_investigation(
                title=f"{issue.title} (cross-cloud)", analysis=unified,
                source=issue.source, severity=issue.severity, pdf_path=pdf_path,
                incident_id=base_incident_id,
                thread_ts=issue.labels.get("slack_thread_ts") or None,
                channel=channel,
            )
            slack_ts = resp.get("ts")
        except Exception as e:
            log.warning(f"Cross-cloud Slack post failed: {e}")

    try:
        from vishwakarma.storage.queries import save_incident
        save_incident(incident_id=base_incident_id, title=f"{issue.title} (cross-cloud)",
                      question=issue.question(), analysis=unified, source=issue.source,
                      severity=issue.severity, labels=issue.labels, tool_outputs=[],
                      meta={"cross_cloud": True, "clouds": [f["cloud"] for f in findings]},
                      slack_ts=slack_ts, pdf_path=pdf_path)
    except Exception as e:
        log.warning(f"Cross-cloud unified save failed: {e}")
    log.info(f"Cross-cloud RCA synthesized + posted for {base_incident_id}")


def _fetch_issue_images(issue, config) -> list[dict]:
    """
    Download images attached to a Slack-reported issue and return them as
    OpenAI-vision image parts ([{url: data-uri, detail}]).

    Slack file `url_private` requires the bot token. Other (already-public)
    URLs are passed through directly. Best-effort: failures are skipped.
    """
    import base64
    raw = getattr(issue, "raw", None) or {}
    urls = raw.get("image_urls") or []
    if not urls:
        return []
    token = config.slack_bot_token or ""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_one(url: str) -> dict | None:
        try:
            headers = {}
            if "slack.com" in url and token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                if not ctype.startswith("image/"):
                    return None
                data = resp.read()
            b64 = base64.b64encode(data).decode()
            return {"url": f"data:{ctype};base64,{b64}", "detail": "auto"}
        except Exception as e:
            log.warning(f"Could not fetch issue image: {e}")
            return None

    capped = urls[:4]  # cap — vision context is expensive
    with ThreadPoolExecutor(max_workers=len(capped)) as pool:
        images = [im for im in pool.map(_fetch_one, capped) if im]
    if images:
        log.info(f"Fetched {len(images)} image(s) for investigation")
    return images


def _prefetch_alert_context(issue) -> str:
    """
    Pre-fetch K8s context before the agentic loop starts.
    Runs kubectl commands in parallel so the LLM begins with real signal,
    not cold — saves the first 3-5 investigation steps.
    """
    import shlex
    import subprocess
    from concurrent.futures import ThreadPoolExecutor, as_completed

    labels = issue.labels or {}
    raw_namespace = (
        labels.get("namespace")
        or labels.get("kubernetes_namespace")
        or labels.get("exported_namespace")
        or "atlas"
    )
    # Sanitize namespace — alert labels are untrusted input
    namespace = shlex.quote(raw_namespace)

    def _run(cmd: str) -> str:
        try:
            out = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True, text=True, timeout=15,
            )
            return (out.stdout or "").strip() or "(no output)"
        except Exception as e:
            return f"(error: {e})"

    commands = {
        "pod_status": f"kubectl get pods -n {namespace} --no-headers 2>/dev/null | head -40",
        "recent_events": (
            f"kubectl get events -n {namespace} --sort-by=.lastTimestamp "
            f"--field-selector type!=Normal 2>/dev/null | tail -20"
        ),
        "recent_deploys": (
            f"kubectl get replicasets -n {namespace} --sort-by=.metadata.creationTimestamp "
            f"-o jsonpath='{{range .items[-5:]}}{{.metadata.name}} {{.metadata.creationTimestamp}}\\n{{end}}' 2>/dev/null"
        ),
    }

    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_run, cmd): key for key, cmd in commands.items()}
        for future in as_completed(futures):
            key = futures[future]
            results[key] = future.result()

    if all("(no output)" in v or "(error" in v for v in results.values()):
        return ""

    parts = ["## Pre-fetched Kubernetes Context\n*(gathered before investigation started — use this data directly. Do NOT re-run these kubectl commands.)*"]
    if results.get("pod_status") and "(error" not in results["pod_status"]:
        parts.append(f"\n### Pod Status (namespace: {namespace})\n```\n{results['pod_status']}\n```")
    if results.get("recent_events") and "(error" not in results["recent_events"]:
        parts.append(f"\n### Warning/Critical Events (namespace: {namespace})\n```\n{results['recent_events']}\n```")
    if results.get("recent_deploys") and "(error" not in results["recent_deploys"]:
        parts.append(f"\n### Recent ReplicaSets (last 5, namespace: {namespace})\n```\n{results['recent_deploys']}\n```")

    return "\n".join(parts)


def _extract_alert_entities(issue, llm) -> str:
    """
    Use the fast model to extract key investigation entities from the alert.
    Gives the main model a head start — costs ~200 tokens, saves 3+ steps.
    """
    if not llm or not llm.cfg.fast_model:
        return ""

    alert_name = issue.labels.get("alertname") or issue.title
    labels_str = "\n".join(f"  {k}: {v}" for k, v in (issue.labels or {}).items())
    description = getattr(issue, "description", "") or ""

    prompt = (
        f"Extract investigation entities from this alert. Be terse and specific.\n\n"
        f"Alert: {alert_name}\n"
        f"Labels:\n{labels_str}\n"
        f"Description: {description}\n\n"
        f"Return ONLY this structure (fill in what you can infer, leave blank if unknown):\n"
        f"Service: <kubernetes service name>\n"
        f"Namespace: <kubernetes namespace>\n"
        f"Impact: <what is broken for end users>\n"
        f"Likely area: <RDS/Redis/app/network/deploy>\n"
        f"Time anchor: <use alert startsAt if available>\n"
        f"Key metric: <the metric that triggered this alert>"
    )

    try:
        extracted = llm.summarize(prompt).strip()
        if not extracted:
            return ""
        return f"## Alert Entity Extraction (fast pre-analysis)\n{extracted}"
    except Exception:
        return ""


def _build_prior_context(issue) -> str:
    """
    Look up past investigations for the same alert and return a context block
    so the LLM knows if this is a recurrence and what was found before.

    Recall is hybrid: substring search on the alert name (always), augmented
    with semantic search over incident embeddings when an embeddings provider
    is configured — catches paraphrased/differently-named recurrences that
    LIKE misses.
    """
    try:
        from vishwakarma.storage.queries import search_incidents, get_incident
        # Search by alert name (from labels or title)
        alert_name = issue.labels.get("alertname") or issue.title
        past = search_incidents(query=alert_name, limit=3)

        # Semantic leg (best-effort)
        try:
            from vishwakarma.core.embeddings import get_client
            from vishwakarma.storage.vectors import search_similar
            emb = get_client()
            if emb.configured:
                qtext = f"{alert_name}\n{issue.title}\n{getattr(issue, 'description', '') or ''}"
                qvec = emb.embed_one(qtext)
                if qvec:
                    seen = {p["id"] for p in past}
                    for ref_id, score in search_similar("incident", qvec, top_k=3, min_score=0.45):
                        if ref_id in seen:
                            continue
                        inc = get_incident(ref_id)
                        if inc:
                            past.append(inc)
                            seen.add(ref_id)
        except Exception as sem_err:
            log.debug(f"Semantic prior-context skipped: {sem_err}")

        past = past[:4]
        if not past:
            return ""

        lines = [
            "## Prior Investigations for This Alert",
            f"This alert ('{alert_name}') has fired before. Previous findings:",
        ]
        for inc in past:
            created = inc.get("created_at", "")[:19]
            analysis_snippet = (inc.get("analysis") or "")[:400].replace("\n", " ")
            lines.append(f"\n**{created}** — {analysis_snippet}...")

        lines.append(
            "\nCheck if this is a recurrence of the same root cause. "
            "If the prior fix was applied, investigate why it recurred."
        )
        return "\n".join(lines)
    except Exception:
        return ""
