"""
RCA Pattern Database — stores confirmed investigation patterns for replay.

When an RCA is marked ✅ Correct, the LLM extracts:
  - root_cause_type: category (missing_index, autovacuum, connection_pool, etc.)
  - investigation_steps: ordered list of tool calls that found the root cause
  - verification_criteria: what to check in tool output to confirm the pattern matches
  - fix: recommended action

On the next alert of the same type:
  1. Fetch matching patterns (same alert_name, active, recent)
  2. Replay the investigation_steps (targeted tool calls)
  3. Check verification_criteria against the new tool output
  4. If match → instant RCA. If not → fall back to full investigation.
"""
import json
import logging
import time
from typing import Any

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)

# ── Schema (added to main DB) ────────────────────────────────────────────────

PATTERNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS rca_patterns (
    id                  TEXT PRIMARY KEY,
    alert_name          TEXT NOT NULL,
    root_cause_type     TEXT NOT NULL,
    root_cause_detail   TEXT NOT NULL,
    investigation_steps TEXT NOT NULL,   -- JSON array of {tool, params_template, what_to_check}
    verification_criteria TEXT NOT NULL, -- what must be true in tool output for pattern to match
    fix                 TEXT NOT NULL,
    confidence          TEXT DEFAULT 'high',
    hit_count           INTEGER DEFAULT 1,
    miss_count          INTEGER DEFAULT 0,
    first_seen          REAL NOT NULL,
    last_seen           REAL NOT NULL,
    last_incident_id    TEXT,
    status              TEXT DEFAULT 'active'  -- active / expired / wrong
);

CREATE INDEX IF NOT EXISTS idx_patterns_alert ON rca_patterns(alert_name, status);
CREATE INDEX IF NOT EXISTS idx_patterns_type ON rca_patterns(root_cause_type);
"""


def init_patterns() -> None:
    """Create patterns table if it doesn't exist."""
    conn = _get_conn()
    with _lock:
        conn.executescript(PATTERNS_SCHEMA)
        conn.commit()


# ── CRUD ──────────────────────────────────────────────────────────────────────

def save_pattern(
    pattern_id: str,
    alert_name: str,
    root_cause_type: str,
    root_cause_detail: str,
    investigation_steps: list[dict],
    verification_criteria: str,
    fix: str,
    confidence: str = "high",
    incident_id: str | None = None,
) -> str:
    """Save a new pattern or increment hit_count if similar pattern exists."""
    conn = _get_conn()
    now = time.time()

    # Check for existing similar pattern (same alert + same root_cause_type)
    with _lock:
        existing = conn.execute(
            "SELECT id, hit_count FROM rca_patterns "
            "WHERE alert_name = ? AND root_cause_type = ? AND status = 'active'",
            (alert_name, root_cause_type),
        ).fetchone()

        if existing:
            # Increment hit count and update
            conn.execute(
                "UPDATE rca_patterns SET hit_count = hit_count + 1, last_seen = ?, "
                "last_incident_id = ?, root_cause_detail = ?, investigation_steps = ?, "
                "verification_criteria = ?, fix = ? WHERE id = ?",
                (now, incident_id, root_cause_detail,
                 json.dumps(investigation_steps), verification_criteria, fix,
                 existing["id"]),
            )
            conn.commit()
            log.info(f"Pattern updated: {existing['id']} (hit_count={existing['hit_count'] + 1})")
            return existing["id"]
        else:
            # Create new pattern
            conn.execute(
                "INSERT INTO rca_patterns "
                "(id, alert_name, root_cause_type, root_cause_detail, investigation_steps, "
                "verification_criteria, fix, confidence, hit_count, first_seen, last_seen, "
                "last_incident_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'active')",
                (pattern_id, alert_name, root_cause_type, root_cause_detail,
                 json.dumps(investigation_steps), verification_criteria, fix,
                 confidence, now, now, incident_id),
            )
            conn.commit()
            log.info(f"New pattern saved: {pattern_id} ({alert_name}/{root_cause_type})")
            return pattern_id


def get_patterns_for_alert(alert_name: str, max_age_days: int = 30) -> list[dict]:
    """Fetch active patterns for an alert, ordered by hit_count (most confirmed first)."""
    conn = _get_conn()
    cutoff = time.time() - (max_age_days * 86400)
    rows = conn.execute(
        "SELECT * FROM rca_patterns "
        "WHERE alert_name = ? AND status = 'active' AND last_seen > ? "
        "ORDER BY hit_count DESC LIMIT 5",
        (alert_name, cutoff),
    ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["investigation_steps"] = json.loads(d["investigation_steps"])
        results.append(d)
    return results


def mark_pattern_hit(pattern_id: str, incident_id: str | None = None) -> None:
    """Increment hit count when pattern successfully replayed."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE rca_patterns SET hit_count = hit_count + 1, last_seen = ?, "
            "last_incident_id = ? WHERE id = ?",
            (time.time(), incident_id, pattern_id),
        )
        conn.commit()


def mark_pattern_miss(pattern_id: str) -> None:
    """Increment miss count when pattern replay didn't match current data."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE rca_patterns SET miss_count = miss_count + 1 WHERE id = ?",
            (pattern_id,),
        )
        # Auto-expire if too many misses
        conn.execute(
            "UPDATE rca_patterns SET status = 'expired' "
            "WHERE id = ? AND miss_count > hit_count * 2",
            (pattern_id,),
        )
        conn.commit()


def mark_pattern_wrong(alert_name: str, root_cause_type: str) -> None:
    """Mark a pattern as wrong (from ❌ feedback)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE rca_patterns SET status = 'wrong' "
            "WHERE alert_name = ? AND root_cause_type = ? AND status = 'active'",
            (alert_name, root_cause_type),
        )
        conn.commit()


# ── Pattern Extraction ────────────────────────────────────────────────────────

def extract_pattern_from_rca(llm, alert_name: str, analysis: str, tool_outputs: list) -> dict | None:
    """Use LLM to extract a replayable pattern from a confirmed RCA.

    Returns dict with: root_cause_type, root_cause_detail, investigation_steps,
    verification_criteria, fix. Or None if extraction fails.
    """
    # Build tool call summary from the investigation
    tool_summary = []
    for t in (tool_outputs or [])[:20]:
        if isinstance(t, dict):
            name = t.get("tool_name", "?")
            params = t.get("params", {})
            status = t.get("status", "?")
            invocation = t.get("invocation", "")
        else:
            name = getattr(t, "tool_name", "?")
            params = getattr(t, "params", {})
            status = getattr(t, "status", "?")
            invocation = getattr(t, "invocation", "")
        if name in ("todo_write", "todo_read", "learnings_list", "learnings_read"):
            continue
        tool_summary.append(f"- {name}({json.dumps(params)[:200]}) → {status}")

    prompt = f"""You are analyzing a CONFIRMED correct RCA for a "{alert_name}" alert.
Extract a replayable investigation pattern that can be used to quickly diagnose the SAME TYPE of issue next time.

## Full RCA Analysis
{analysis[:4000]}

## Tool Calls Made During Investigation
{chr(10).join(tool_summary[:15])}

Extract the pattern as JSON. The investigation_steps should be the MINIMAL set of tool calls needed to confirm this specific root cause type — not the entire investigation, just the 3-5 key steps.

IMPORTANT:
- investigation_steps params should be TEMPLATES with <placeholders> for values that change (table names, instance IDs)
- verification_criteria should describe what the tool output must show for this pattern to match
- root_cause_type should be a short category (e.g. "missing_index", "autovacuum", "connection_pool", "stale_replication_slot")

Respond ONLY with valid JSON (no markdown fences):
{{"root_cause_type": "short_category", "root_cause_detail": "one sentence description", "investigation_steps": [{{"tool": "tool_name", "params_template": {{"key": "value_or_<placeholder>"}}, "what_to_check": "what to look for in output"}}, ...], "verification_criteria": "what must be true for this pattern to match", "fix": "recommended action"}}"""

    try:
        raw = llm.summarize(prompt).strip()
        # Strip markdown fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        pattern = json.loads(raw)
        # Validate required fields
        required = ["root_cause_type", "root_cause_detail", "investigation_steps",
                     "verification_criteria", "fix"]
        if all(k in pattern for k in required):
            return pattern
        log.warning(f"Pattern extraction missing fields: {[k for k in required if k not in pattern]}")
        return None
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Pattern extraction failed: {e}")
        return None


# ── Pattern Replay ────────────────────────────────────────────────────────────

def replay_pattern(pattern: dict, executor, llm, alert_context: str = "") -> dict | None:
    """Replay a pattern's investigation steps and validate against current data.

    Returns dict with: matched (bool), root_cause, evidence, fix, pattern_id.
    Or None if replay fails.
    """
    steps = pattern.get("investigation_steps", [])
    if not steps:
        return None

    results = []
    for step in steps[:5]:  # max 5 steps
        tool_name = step.get("tool", "")
        params = step.get("params_template", {})
        what_to_check = step.get("what_to_check", "")

        # Execute the tool
        try:
            output = executor.execute(tool_name, params)
            content = str(output.output) if output.output else str(output.error)
            results.append({
                "tool": tool_name,
                "what_to_check": what_to_check,
                "output": content[:2000],
                "status": str(output.status),
            })
        except Exception as e:
            results.append({
                "tool": tool_name,
                "what_to_check": what_to_check,
                "output": f"(error: {e})",
                "status": "error",
            })

    # Ask LLM to validate: does the current data match the pattern?
    validation_prompt = f"""You are validating a known RCA pattern against fresh data from a new "{pattern.get('alert_name', '?')}" alert.

## Known Pattern
Root cause type: {pattern.get('root_cause_type', '?')}
Root cause detail: {pattern.get('root_cause_detail', '?')}
Verification criteria: {pattern.get('verification_criteria', '?')}
Confirmed {pattern.get('hit_count', 0)} times before.

## Fresh Tool Results (just collected)
{json.dumps(results, indent=2)[:3000]}

## Current Alert Context
{alert_context[:1000]}

QUESTION: Does the fresh data match this known pattern?
- The ROOT CAUSE TYPE must be the same (e.g. missing index), but specific details can differ (different table, different column)
- The VERIFICATION CRITERIA must be satisfied by the fresh tool output
- If the data clearly shows a DIFFERENT root cause (e.g. pattern says missing_index but data shows autovacuum), it does NOT match

Respond ONLY with valid JSON (no markdown fences):
{{"matched": true/false, "confidence": "high/medium/low", "root_cause": "specific root cause for THIS instance based on fresh data", "evidence": "2-3 key facts from the fresh tool output", "differences": "what's different from the known pattern (if any)"}}"""

    try:
        raw = llm.summarize(validation_prompt).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        validation = json.loads(raw)
        validation["pattern_id"] = pattern.get("id", "")
        validation["fix"] = pattern.get("fix", "")
        validation["root_cause_type"] = pattern.get("root_cause_type", "")
        validation["hit_count"] = pattern.get("hit_count", 0)
        return validation
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Pattern validation failed: {e}")
        return None
