import json
import logging
import threading

log = logging.getLogger(__name__)

CORRECT_ACTION_ID = "vk_rca_correct"
WRONG_ACTION_ID = "vk_rca_wrong"
FEEDBACK_SCREEN_ID = "argus-feedback"

_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


def _as_dict(value) -> dict:
    if isinstance(value, str):
        try:
            return json.loads(value) if value else {}
        except Exception:
            return {}
    return value or {}


def _as_list(value) -> list:
    if isinstance(value, str):
        try:
            return json.loads(value) if value else []
        except Exception:
            return []
    return value or []


def record_correct(config, incident_id: str) -> dict:
    from vishwakarma.bot.slack import _distill_fact, _infer_category
    from vishwakarma.core.learnings import LearningsManager
    from vishwakarma.storage.queries import get_incident

    incident = get_incident(incident_id)
    if not incident:
        return {"status": "missing"}

    learnings = LearningsManager()
    analysis = incident.get("analysis", "")
    alert_name = (incident.get("labels") or {}).get("alertname") or incident.get("title", "")

    existing = learnings.get(_infer_category(alert_name))
    fact = _distill_fact(config, analysis, alert_name, existing_facts=existing)
    if not fact:
        return {"status": "duplicate"}
    category = _infer_category(alert_name, config=config, fact=fact)
    learnings.append(category, fact)
    log.info(f"[FEEDBACK] ✅ Appended to learnings[{category}]: {fact[:80]}")
    llm = config.make_llm()
    learnings.compact(category, llm.summarize)

    try:
        from vishwakarma.storage.evidence import mark_evidence_correct
        mark_evidence_correct(incident_id, alert_name=alert_name)
    except Exception as e:
        log.debug(f"[FEEDBACK] Evidence update failed (non-fatal): {e}")

    try:
        from vishwakarma.storage.runbooks import mark_runbook_hit
        for rid in _as_dict(incident.get("meta")).get("matched_runbook_ids", []):
            mark_runbook_hit(rid, alert_name=alert_name)
    except Exception as e:
        log.debug(f"[FEEDBACK] Runbook hit update failed (non-fatal): {e}")

    tool_outputs = _as_list(incident.get("tool_outputs"))
    try:
        from vishwakarma.storage.runbooks import normalize_alert_key
        from vishwakarma.storage.tool_effectiveness import record_effective, tools_to_toolsets
        used_tools = {o.get("tool_name") for o in tool_outputs if o.get("tool_name")}
        toolsets = config.make_toolset_manager().active_toolsets()
        record_effective(normalize_alert_key(alert_name), tools_to_toolsets(used_tools, toolsets))
    except Exception as e:
        log.debug(f"[FEEDBACK] Tool effectiveness update failed (non-fatal): {e}")

    pattern_type = ""
    try:
        import uuid
        from vishwakarma.storage.patterns import extract_pattern_from_rca, save_pattern
        pattern = extract_pattern_from_rca(llm, alert_name, analysis, tool_outputs)
        if pattern:
            save_pattern(
                pattern_id=str(uuid.uuid4()),
                alert_name=alert_name,
                root_cause_type=pattern["root_cause_type"],
                root_cause_detail=pattern["root_cause_detail"],
                investigation_steps=pattern["investigation_steps"],
                verification_keywords=pattern.get("verification_keywords", []),
                verification_anti_keywords=pattern.get("verification_anti_keywords", []),
                fix=pattern["fix"],
                incident_id=incident_id,
            )
            pattern_type = pattern["root_cause_type"]
            log.info(f"[FEEDBACK] Pattern saved: {alert_name}/{pattern_type}")
    except Exception as e:
        log.warning(f"[FEEDBACK] Pattern extraction failed (non-fatal): {e}")

    return {"status": "saved", "category": category, "pattern_saved": bool(pattern_type),
            "pattern_type": pattern_type}


def record_wrong(config, incident_id: str, real_cause: str = "") -> dict:
    from vishwakarma.bot.slack import _distill_fact, _infer_category
    from vishwakarma.core.learnings import LearningsManager
    from vishwakarma.storage.queries import get_incident

    incident = get_incident(incident_id) if incident_id else None
    alert_name = ((incident.get("labels") or {}).get("alertname") if incident else "") or ""

    category = ""
    if real_cause.strip():
        learnings = LearningsManager()
        existing = learnings.get(_infer_category(alert_name))
        fact = _distill_fact(config, real_cause, alert_name, correction=True, existing_facts=existing)
        if not fact:
            return {"status": "duplicate"}
        category = _infer_category(alert_name, config=config, fact=fact)
        learnings.append(category, fact)
        learnings.compact(category, config.make_llm().summarize)
        log.info(f"[FEEDBACK] ❌ Correction appended to learnings[{category}]: {fact[:80]}")

    try:
        from vishwakarma.storage.patterns import invalidate_patterns_for_incident
        invalidate_patterns_for_incident(incident_id)
    except Exception as e:
        log.warning(f"[FEEDBACK] Pattern invalidation failed (non-fatal): {e}")

    matched: list = []
    try:
        from vishwakarma.storage.runbooks import mark_runbook_miss
        matched = _as_dict(incident.get("meta") if incident else {}).get("matched_runbook_ids", [])
        for rid in matched:
            mark_runbook_miss(rid)
    except Exception as e:
        log.debug(f"[FEEDBACK] Runbook miss update failed (non-fatal): {e}")

    if real_cause.strip():
        try:
            from vishwakarma.core.amendment_proposals import propose_amendments
            from vishwakarma.storage import runbooks as rb
            wrong_rca = (incident.get("analysis") or "")[:4000] if incident else ""
            llm = config.make_llm()
            propose_amendments(
                incident_id, matched, corrected=False,
                fetch_incident=lambda i: {
                    "analysis": f"WRONG RCA:\n{wrong_rca}\n\nCORRECT ROOT CAUSE (per human):\n{real_cause}"},
                get_runbook=rb.get_runbook,
                summarize=lambda p: llm.summarize(p),
                save_proposal=rb.save_proposal)
        except Exception as e:
            log.debug(f"[FEEDBACK] Amendment proposal failed (non-fatal): {e}")

    return {"status": "saved", "category": category, "runbooks_penalized": len(matched)}


def _outcome_text(verb: str, result: dict, who: str) -> str:
    status = result.get("status")
    if status == "missing":
        return "⚠️ Could not find this incident, so no feedback was recorded."
    if verb == CORRECT_ACTION_ID:
        if status == "duplicate":
            return f"✅ {who} marked this RCA correct — the root cause is already captured in learnings."
        lines = [f"✅ Marked correct by {who}", f"🧠 Learning saved to {result.get('category')}"]
        if result.get("pattern_saved"):
            lines.append(f"⚙️ Investigation pattern saved ({result.get('pattern_type')}) — it will replay on the next identical alert")
        lines.append("📈 Evidence baselines updated")
        return "\n".join(lines)
    if status == "duplicate":
        return f"❌ {who} marked this RCA wrong — that correction is already captured in learnings."
    penalized = result.get("runbooks_penalized", 0)
    note = f" ({penalized} runbook match(es) down-weighted)" if penalized else ""
    return f"❌ Marked wrong by {who}{note}. Matching replay patterns were invalidated."


def handle_flow_action(config, payload: dict, user_lookup) -> dict | None:
    verb, _, incident_id = str(payload.get("actionId") or "").partition(":")
    if verb not in (CORRECT_ACTION_ID, WRONG_ACTION_ID) or not incident_id:
        return None
    context = payload.get("context") or {}
    user_id = str(context.get("userId") or payload.get("userId") or "")
    name, _email = user_lookup(user_id)
    who = name or user_id or "someone"
    channel = str(payload.get("conversationId") or "")
    message_id = str(payload.get("messageId") or "")

    key = f"{verb}:{incident_id}"
    with _in_flight_lock:
        if key in _in_flight:
            return {"type": "ack", "message": "Already saving this feedback."}
        _in_flight.add(key)

    def _update_card(text: str) -> None:
        if not (channel and message_id and config.xyne_base_url and config.xyne_bot_token):
            return
        try:
            from vishwakarma.plugins.relays.xyne.plugin import FLOW_TS_PREFIX, XyneWebClient, text_flow
            XyneWebClient(config.xyne_base_url, config.xyne_bot_token).chat_update(
                channel=channel, ts=f"{FLOW_TS_PREFIX}{message_id}", text=text, flow=text_flow(text))
        except Exception as e:
            log.warning(f"[FEEDBACK] Xyne card update failed (non-fatal): {e}")

    def _work() -> None:
        try:
            result = (record_correct(config, incident_id) if verb == CORRECT_ACTION_ID
                      else record_wrong(config, incident_id))
            text = _outcome_text(verb, result, who)
            log.info(f"[FEEDBACK] {verb} {incident_id[:8]} -> {result.get('status')} by {who} via xyne")
        except Exception as e:
            log.error(f"[FEEDBACK] {verb} failed for {incident_id[:8]}: {e}", exc_info=True)
            text = "⚠️ Saving feedback failed — please try again."
        finally:
            with _in_flight_lock:
                _in_flight.discard(key)
        _update_card(text)

    _update_card("⏳ Saving feedback…")
    threading.Thread(target=_work, daemon=True).start()
    return {"type": "ack", "message": "Saving feedback…"}
