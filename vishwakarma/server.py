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

    # Initialize toolset manager and storage once at startup
    _state: dict[str, Any] = {}

    from vishwakarma.ui.routes import create_ui_router
    app.include_router(create_ui_router(_state))
    from vishwakarma.ui.console_api import create_console_router
    app.include_router(create_console_router(config, _state))
    _mount_console_spa(app)

    @app.on_event("startup")
    async def startup():
        from vishwakarma.storage.db import init_db
        init_db(config.db_path, dsn=config.pg_dsn)
        _dedup.init_dedup(config.redis_url)
        from vishwakarma.core.embeddings import init_embeddings
        init_embeddings(config.embeddings_api_base, config.embeddings_api_key,
                        config.embeddings_model, config.embeddings_dim)
        from vishwakarma.core.eventbus import init_eventbus
        init_eventbus(config.redis_url)
        from vishwakarma.core.keypool import init_keypool
        init_keypool(config.llm.api_keys or ([config.llm.api_key] if config.llm.api_key else []))
        from vishwakarma.core.correlation import init_correlation
        init_correlation(config.redis_url)
        from vishwakarma.core.pr_creator import init_pr_creator
        init_pr_creator(config.github_enabled, config.github_token,
                        config.github_api_base, config.github_default_base)
        # Seed DB runbooks from the repo's agents.json + .md files (idempotent;
        # keeps file-based runbooks working as defaults on fresh installs).
        try:
            from vishwakarma.storage.runbooks import seed_from_files
            seed_from_files()
        except Exception as seed_err:
            log.warning(f"Runbook seeding failed (file-based matching still works): {seed_err}")
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

        log.info(f"Vishwakarma server ready (role={getattr(config, 'role', '') or 'all-in-one'})")

    # ── /healthz ──────────────────────────────────────────────────────────────

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
        import asyncio

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        from vishwakarma.plugins.channels.alertmanager.plugin import parse_alertmanager_webhook
        from vishwakarma.storage.queries import save_incident, alert_fingerprint

        issues = parse_alertmanager_webhook(payload)
        if not issues:
            return {"status": "no_issues"}

        triggered = []
        for issue in issues:
            fingerprint = alert_fingerprint(issue.labels)

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

        return {"status": "ok", "alerts": triggered}

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


# ── Background investigation ───────────────────────────────────────────────────

async def _run_alert_investigation(config, state, issue, incident_id: str, fingerprint: str = ""):
    import asyncio

    semaphore = _get_semaphore()
    queue_pos = MAX_CONCURRENT_INVESTIGATIONS - semaphore._value
    if queue_pos >= MAX_CONCURRENT_INVESTIGATIONS:
        log.info(f"Alert queued (concurrency limit {MAX_CONCURRENT_INVESTIGATIONS} reached): {issue.title}")

    async with semaphore:
        await _do_investigation(config, state, issue, incident_id, fingerprint)


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

    # Scale investigation depth by alert severity
    _severity_steps = {"critical": 60, "high": 50, "warning": 40, "medium": 40, "low": 25, "info": 20}
    engine.max_steps = _severity_steps.get((issue.severity or "").lower(), config.max_steps)

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
        if config.is_slack_configured() and not cross_cloud:
            try:
                from vishwakarma.plugins.relays.slack.plugin import SlackDestination
                dest = SlackDestination({"token": config.slack_bot_token})
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
                        "text": {"type": "plain_text", "text": f":rotating_light: {issue.title[:150]}", "emoji": True},
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
            except Exception as e:
                log.warning(f"Slack ack failed (non-fatal): {e}")

        # Run the 4 pre-enrichment tasks in parallel
        prefetch_future = loop.run_in_executor(None, _prefetch_alert_context, issue)
        prior_future = loop.run_in_executor(None, _build_prior_context, issue)
        entities_future = loop.run_in_executor(None, _extract_alert_entities, issue, llm)
        runbooks_future = loop.run_in_executor(None, load_matching_runbooks, alert_name, llm)

        prefetch_ctx, prior_ctx, entities_ctx, matched_runbooks = await _asyncio.gather(
            prefetch_future, prior_future, entities_future, runbooks_future
        )

        # Capture the matched runbook ids so the ✅/❌ feedback loop can credit
        # them (Slack buttons + console feedback bump hit/miss + self-populate
        # the alert→runbook map).
        matched_runbook_ids: list[str] = []
        try:
            from vishwakarma.core.runbook_match import match_runbooks
            cloud = issue.labels.get("cloud", "")
            matched_runbook_ids = [m["id"] for m in match_runbooks(alert_name, cloud=cloud)]
        except Exception:
            pass

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
                    if slack_client and slack_channel_id and ack_ts:
                        try:
                            slack_client.chat_postMessage(
                                channel=slack_channel_id, thread_ts=ack_ts,
                                text=f":mag: Launching {len(domains)} parallel sub-agents: {', '.join(d.upper() for d in domains)}",
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": f":mag: _Launching {len(domains)} parallel sub-agents: {', '.join(d.upper() for d in domains)}_"}
                                ]}],
                            )
                        except Exception:
                            pass

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

            # Post initial status message in thread
            log.info(f"Streaming investigation: slack_client={bool(slack_client)} channel={slack_channel_id} ack_ts={ack_ts}")
            if slack_client and slack_channel_id and ack_ts:
                try:
                    resp = slack_client.chat_postMessage(
                        channel=slack_channel_id,
                        thread_ts=ack_ts,
                        text=":hourglass: Starting deep investigation...",
                        blocks=[{"type": "context", "elements": [
                            {"type": "mrkdwn", "text": ":hourglass: _Starting deep investigation..._"}
                        ]}],
                    )
                    status_ts = resp["ts"]
                    log.info(f"Status message posted: ts={status_ts}")
                except Exception as e:
                    log.warning(f"Status message failed: {e}")

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
                available = {ts.name for ts in engine.executor.toolsets if ts.enabled}
                sel_text = f"{alert_name} {issue.title} {issue.description or ''}"
                tool_subset = select_toolset_names(sel_text, available)
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
                    visible = tool_lines[-10:]
                    status_text = "\n".join(visible)
                    if slack_client and status_ts:
                        try:
                            slack_client.chat_update(
                                channel=slack_channel_id, ts=status_ts, text=status_text,
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": status_text}
                                ]}],
                            )
                        except Exception:
                            pass

                elif etype == "tool_call_result":
                    status = event.get("status", "")
                    marker = ":white_check_mark:" if status == "success" else ":x:"
                    tool_name = event.get("tool", "")
                    for i in range(len(tool_lines) - 1, -1, -1):
                        if tool_name and f"`{tool_name}(" in tool_lines[i] and ":white_check_mark:" not in tool_lines[i] and ":x:" not in tool_lines[i]:
                            tool_lines[i] = tool_lines[i] + f" {marker}"
                            break
                    visible = tool_lines[-10:]
                    status_text = "\n".join(visible)
                    if slack_client and status_ts:
                        try:
                            slack_client.chat_update(
                                channel=slack_channel_id, ts=status_ts, text=status_text,
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": status_text}
                                ]}],
                            )
                        except Exception:
                            pass

                elif etype == "compaction":
                    tool_lines.append(":compression: _context compacted_")

                elif etype == "max_steps_reached":
                    analysis = event.get("content", "") or "Investigation reached max steps."

                elif etype == "done":
                    analysis = event.get("content", "") or analysis

            # Finalize status message
            tool_count = len([t for t in tool_lines if ":gear:" in t])
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
                    {"type": "header", "text": {"type": "plain_text", "text": f":white_check_mark: {issue.title[:150]}", "emoji": True}},
                    {"type": "divider"},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": ":thread: _Investigation complete. See thread for full RCA report + PDF._"}]},
                ]}],
            )
        except Exception as e:
            log.debug(f"Ack update failed (non-fatal): {e}")

    # Post to Slack — thread reply if fast RCA was posted, otherwise new message
    slack_ts = None
    if config.is_slack_configured():
        try:
            from vishwakarma.plugins.relays.slack.plugin import SlackDestination
            dest = SlackDestination({"token": config.slack_bot_token})
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
    if config.is_slack_configured():
        try:
            from vishwakarma.plugins.relays.slack.plugin import SlackDestination
            dest = SlackDestination({"token": config.slack_bot_token})
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
    images: list[dict] = []
    token = config.slack_bot_token or ""
    import urllib.request
    for url in urls[:4]:  # cap — vision context is expensive
        try:
            headers = {}
            if "slack.com" in url and token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                if not ctype.startswith("image/"):
                    continue
                data = resp.read()
            b64 = base64.b64encode(data).decode()
            images.append({"url": f"data:{ctype};base64,{b64}", "detail": "auto"})
        except Exception as e:
            log.warning(f"Could not fetch issue image: {e}")
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
