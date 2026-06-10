"""
Console API — REST + SSE backend for the Argus web console.

Mounted at /api/console. Serves the 7 console pages:
  overview · live investigations · incident history · runbook studio ·
  fixes/PRs · fleet · feedback (knowledge/settings reuse existing routes)

RBAC: two roles. Reads need `reader` (or better); writes need `admin`.
  ui.auth_disabled: true   → everything is admin (local dev default)
  ui.admin_tokens / ui.reader_tokens → bearer-style X-VK-Token header.
Google SSO replaces token auth at deployment time behind the same
`require_role` dependency — handlers don't change.
"""
import json
import logging
import queue
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)


# ── RBAC ──────────────────────────────────────────────────────────────────────

class _Auth:
    def __init__(self, config):
        ui = getattr(config, "raw", {}) if hasattr(config, "raw") else {}
        self.disabled = bool(getattr(config, "ui_auth_disabled", True))
        self.admin_tokens = set(getattr(config, "ui_admin_tokens", []) or [])
        self.reader_tokens = set(getattr(config, "ui_reader_tokens", []) or [])

    def role_of(self, request: Request) -> str:
        if self.disabled:
            return "admin"
        token = request.headers.get("X-VK-Token", "")
        if token and token in self.admin_tokens:
            return "admin"
        if token and token in self.reader_tokens:
            return "reader"
        return ""


def _require(auth: _Auth, minimum: str):
    order = {"": 0, "reader": 1, "admin": 2}

    async def dep(request: Request) -> str:
        role = auth.role_of(request)
        if order[role] < order[minimum]:
            raise HTTPException(401 if not role else 403,
                                detail=f"requires {minimum} role")
        return role
    return dep


# ── Request models ────────────────────────────────────────────────────────────

class RunbookBody(BaseModel):
    title: str
    content_md: str
    cloud_type: str = "any"
    keywords: list[str] = []
    services: list[str] = []
    author: str = ""


class MappingBody(BaseModel):
    alert_name: str
    priority: int = 100


class DryRunBody(BaseModel):
    alert_text: str
    cloud: str = ""


class FeedbackBody(BaseModel):
    correct: bool
    runbook_ids: list[str] = []   # runbooks used by the investigation, if known
    alert_name: str = ""


# ── Router ────────────────────────────────────────────────────────────────────

def create_console_router(config, state: dict) -> APIRouter:
    router = APIRouter(prefix="/api/console")
    auth = _Auth(config)
    reader = Depends(_require(auth, "reader"))
    admin = Depends(_require(auth, "admin"))

    # ── Overview / dashboard ──────────────────────────────────────────────────

    @router.get("/overview")
    async def overview(role: str = reader):
        from vishwakarma.storage.investigations import list_active
        from vishwakarma.storage.queries import get_stats
        out = {
            "active_investigations": list_active(limit=20),
            "incident_stats": get_stats(),
            "fleet": _fleet_snapshot(),
        }
        for inv in out["active_investigations"]:
            inv.pop("messages", None)  # cards don't need the transcript
        return out

    # ── Investigations (live) ─────────────────────────────────────────────────

    @router.get("/investigations")
    async def investigations(status: str | None = None, limit: int = 50,
                             role: str = reader):
        from vishwakarma.storage.db import _get_conn
        conn = _get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM investigations WHERE status=? "
                "ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM investigations ORDER BY updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("messages", None)
            for f in ("findings", "code_session"):
                if isinstance(d.get(f), str) and d[f]:
                    try:
                        d[f] = json.loads(d[f])
                    except Exception:
                        pass
            out.append(d)
        return out

    @router.get("/investigations/{incident_id}")
    async def investigation_detail(incident_id: str, role: str = reader):
        from vishwakarma.storage.investigations import get_investigation
        inv = get_investigation(incident_id)
        if not inv:
            raise HTTPException(404, "investigation not found")
        # Attach any alerts grouped into this one by incident correlation.
        try:
            from vishwakarma.core.correlation import list_correlated
            inv["correlated_alerts"] = list_correlated(incident_id)
        except Exception:
            inv["correlated_alerts"] = []
        return inv

    @router.post("/investigations/{incident_id}/abort")
    async def abort_investigation(incident_id: str, role: str = admin):
        """Stop a running investigation — the engine halts at the next step."""
        from vishwakarma.core.aborts import request_abort
        from vishwakarma.storage.investigations import get_investigation
        inv = get_investigation(incident_id)
        if not inv:
            raise HTTPException(404, "investigation not found")
        if inv.get("status") not in ("running", "queued"):
            return {"status": inv.get("status"), "note": "not running — nothing to abort"}
        request_abort(incident_id)
        # Mark TERMINAL 'aborted' — the reaper only resumes 'running', so an
        # aborted investigation is never restarted (by reaper, deploy, or retry).
        try:
            import time as _t
            from vishwakarma.storage.db import _get_conn, _lock
            with _lock:
                conn = _get_conn()
                conn.execute("UPDATE investigations SET status='aborted', updated_at=? WHERE id=?",
                             (_t.time(), incident_id))
                conn.commit()
        except Exception:
            pass
        return {"status": "aborted", "incident_id": incident_id}

    @router.post("/investigations/{incident_id}/retry")
    async def retry_investigation(incident_id: str, role: str = admin):
        """Re-run an investigation fresh — re-fires the original alert through the pipeline."""
        import json as _json
        from vishwakarma.storage.queries import get_incident
        from vishwakarma.storage.investigations import get_investigation
        inv = get_investigation(incident_id)
        if inv and inv.get("status") == "aborted":
            raise HTTPException(409, "investigation was aborted — it will not be restarted")
        inc = get_incident(incident_id)
        if not inc:
            raise HTTPException(404, "incident not found")
        labels = inc.get("labels") or {}
        if isinstance(labels, str):
            try:
                labels = _json.loads(labels)
            except Exception:
                labels = {}
        if not labels.get("alertname"):
            labels["alertname"] = inc.get("title", "RetryInvestigation")[:80]
        summary = inc.get("question") or inc.get("title") or "Retry investigation"
        payload = {"alerts": [{"status": "firing", "labels": labels,
                               "annotations": {"summary": summary}}]}
        try:
            import urllib.request
            req = urllib.request.Request("http://localhost:5050/api/alertmanager",
                                         data=_json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            resp = _json.load(urllib.request.urlopen(req, timeout=30))
            return {"status": "retriggered", "result": resp}
        except Exception as e:
            raise HTTPException(500, f"retry failed: {e}")

    # ── Incident history ──────────────────────────────────────────────────────

    @router.get("/incidents")
    async def incidents(q: str | None = None, source: str | None = None,
                        status: str | None = None, limit: int = 50,
                        role: str = reader):
        from vishwakarma.storage.queries import search_incidents, list_incidents
        if q:
            rows = search_incidents(q, limit=limit)
            # Semantic augmentation (best-effort)
            try:
                from vishwakarma.core.embeddings import get_client
                from vishwakarma.storage.vectors import search_similar
                from vishwakarma.storage.queries import get_incident
                emb = get_client()
                if emb.configured:
                    vec = emb.embed_one(q)
                    if vec:
                        seen = {r["id"] for r in rows}
                        for rid, _s in search_similar("incident", vec, top_k=5,
                                                      min_score=0.4):
                            if rid not in seen:
                                inc = get_incident(rid)
                                if inc:
                                    rows.append(inc)
            except Exception:
                pass
        else:
            rows = list_incidents(source=source, status=status, limit=limit)
        for r in rows:
            r.pop("tool_outputs", None)  # list view stays light
            if isinstance(r.get("analysis"), str):
                r["analysis"] = r["analysis"][:500]
        return rows[:limit]

    @router.get("/incidents/{incident_id}")
    async def incident_detail(incident_id: str, role: str = reader):
        from vishwakarma.storage.queries import get_incident
        inc = get_incident(incident_id)
        if not inc:
            raise HTTPException(404, "incident not found")
        return inc

    # ── Feedback (✅/❌ — same loops the Slack buttons feed) ───────────────────

    @router.post("/incidents/{incident_id}/feedback")
    async def feedback(incident_id: str, body: FeedbackBody, role: str = admin):
        from vishwakarma.storage import evidence
        from vishwakarma.storage import runbooks as rb
        from vishwakarma.storage.audit import audit
        audit(role, "feedback", incident_id, {"correct": body.correct})
        results = {"evidence": False, "runbooks": []}
        try:
            if body.correct:
                evidence.mark_evidence_correct(incident_id)
            else:
                evidence.mark_evidence_wrong(incident_id)
            results["evidence"] = True
        except Exception as e:
            log.warning(f"Feedback evidence update failed: {e}")
        for rid in body.runbook_ids:
            try:
                if body.correct:
                    rb.mark_runbook_hit(rid, alert_name=body.alert_name)
                else:
                    rb.mark_runbook_miss(rid)
                results["runbooks"].append(rid)
            except Exception as e:
                log.warning(f"Feedback runbook update failed for {rid}: {e}")
        return results

    # ── Runbook studio ────────────────────────────────────────────────────────

    @router.get("/runbooks")
    async def runbooks_list(status: str | None = "active",
                            cloud: str | None = None, role: str = reader):
        from vishwakarma.storage import runbooks as rb
        return rb.list_runbooks(status=status or None, cloud=cloud)

    @router.get("/runbooks/{runbook_id}")
    async def runbook_get(runbook_id: str, role: str = reader):
        from vishwakarma.storage import runbooks as rb
        got = rb.get_runbook(runbook_id)
        if not got:
            raise HTTPException(404, "runbook not found")
        return got

    @router.put("/runbooks/{runbook_id}")
    async def runbook_save(runbook_id: str, body: RunbookBody, role: str = admin):
        from vishwakarma.storage import runbooks as rb
        from vishwakarma.storage.audit import audit
        rb.save_runbook(runbook_id, body.title, body.content_md,
                        cloud_type=body.cloud_type, keywords=body.keywords,
                        services=body.services, author=body.author or role)
        audit(role, "runbook.save", runbook_id, {"title": body.title})
        return rb.get_runbook(runbook_id)

    @router.delete("/runbooks/{runbook_id}")
    async def runbook_delete(runbook_id: str, role: str = admin):
        from vishwakarma.storage import runbooks as rb
        from vishwakarma.storage.audit import audit
        if not rb.get_runbook(runbook_id):
            raise HTTPException(404, "runbook not found")
        rb.delete_runbook(runbook_id)
        audit(role, "runbook.delete", runbook_id)
        return {"deleted": runbook_id}

    @router.post("/runbooks/{runbook_id}/mappings")
    async def runbook_map(runbook_id: str, body: MappingBody, role: str = admin):
        from vishwakarma.storage import runbooks as rb
        from vishwakarma.storage.audit import audit
        if not rb.get_runbook(runbook_id):
            raise HTTPException(404, "runbook not found")
        rb.map_alert(body.alert_name, runbook_id, priority=body.priority)
        audit(role, "runbook.map", runbook_id, {"alert": body.alert_name})
        return {"mapped": rb.normalize_alert_key(body.alert_name),
                "runbook_id": runbook_id}

    @router.post("/runbooks/dry-run")
    async def runbook_dry_run(body: DryRunBody, role: str = reader):
        """What would match this alert? (Studio test panel — no LLM rerank.)"""
        from vishwakarma.core.runbook_match import match_runbooks
        matched = match_runbooks(body.alert_text, cloud=body.cloud)
        return [{"id": m["id"], "title": m["title"], "cloud_type": m["cloud_type"],
                 "hit_count": m["hit_count"], "miss_count": m["miss_count"]}
                for m in matched]

    # ── Fixes / PRs (Phase-3 gate surface; PR fields land with the GitHub App) ─

    @router.get("/fixes")
    async def fixes(role: str = reader):
        from vishwakarma.storage.db import _get_conn
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM investigations WHERE status='awaiting_fix_review' "
            "ORDER BY updated_at DESC LIMIT 50").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("messages", None)
            if isinstance(d.get("code_session"), str) and d["code_session"]:
                try:
                    d["code_session"] = json.loads(d["code_session"])
                except Exception:
                    pass
            out.append(d)
        return out

    # ── Audit log ─────────────────────────────────────────────────────────────

    @router.get("/audit")
    async def audit_list(limit: int = 100, role: str = admin):
        from vishwakarma.storage.audit import list_audit
        return list_audit(limit=limit)

    # ── Fleet ─────────────────────────────────────────────────────────────────

    @router.get("/fleet")
    async def fleet(role: str = reader):
        return _fleet_snapshot()

    def _fleet_snapshot() -> dict:
        snap: dict = {"queues": {}, "executors": [], "orphaned": []}
        try:
            from vishwakarma.core import jobstream
            for cloud in ("aws", "gcp"):
                snap["queues"][cloud] = {
                    "depth": jobstream.depth(cloud),
                    "pending": jobstream.pending_count(cloud),
                }
        except Exception:
            snap["queues"] = {}  # no Redis / all-in-one mode
        try:
            from vishwakarma.storage.db import _get_conn
            conn = _get_conn()
            rows = conn.execute(
                "SELECT worker_id, cloud, COUNT(*) AS jobs, MAX(heartbeat_at) AS hb "
                "FROM investigations WHERE status='running' "
                "GROUP BY worker_id, cloud").fetchall()
            now = time.time()
            snap["executors"] = [
                {"worker_id": r[0], "cloud": r[1], "running_jobs": r[2],
                 "heartbeat_age_s": round(now - (r[3] or now), 1)}
                for r in rows
            ]
            from vishwakarma.storage.investigations import find_orphaned
            snap["orphaned"] = [
                {"id": o["id"], "worker_id": o.get("worker_id"), "step": o.get("step")}
                for o in find_orphaned()
            ]
        except Exception as e:
            log.debug(f"Fleet snapshot partial: {e}")
        return snap

    # ── Live events (SSE) ─────────────────────────────────────────────────────

    @router.get("/events")
    async def events(request: Request, incident_id: str | None = None,
                     role: str = reader):
        """Server-sent events: every investigation event, optionally filtered."""
        from vishwakarma.core import eventbus

        async def stream():
            import asyncio
            q = eventbus.subscribe()
            last_beat = time.time()
            try:
                yield "event: hello\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        evt = q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.5)
                        if time.time() - last_beat > 15:   # proxy keepalive
                            last_beat = time.time()
                            yield ": keepalive\n\n"
                        continue
                    if incident_id and evt.get("incident_id") != incident_id:
                        continue
                    yield f"data: {json.dumps(evt, default=str)}\n\n"
            finally:
                eventbus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router
