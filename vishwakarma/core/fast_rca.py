"""
Fast RCA — quick classification for alerts with known root cause patterns.

For alerts like NoDriverDrainerRunning (15+/week, only 4 known root causes),
this module runs targeted checks via a specialized toolset and classifies
the result with a single fast_model LLM call (~5-10s) instead of a full
40-step agentic investigation (~15 min).

The fast RCA is posted to Slack immediately; the deep investigation follows
as a thread reply.
"""
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# ── Registry: alert_name → (toolset_name, tool_name, params) ─────────────────

_REGISTRY: dict[str, tuple[str, str, dict]] = {
    # Drainer stopped
    "NoDriverDrainerRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "driver"}),
    "NoDriverDrainerPodRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "driver"}),
    "NoAppDrainerRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "app"}),
    "NoAppDrainerPodRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "app"}),
    "NoCustomerDrainerPodRunning": ("cloud_alerts", "investigate_drainer", {"drainer_type": "app"}),
    # Drainer lag
    "DriverDrainerLagIncreasing": ("cloud_alerts", "investigate_drainer", {"drainer_type": "driver"}),
    "CustomerDrainerLagIncreasing": ("cloud_alerts", "investigate_drainer", {"drainer_type": "app"}),
}


def match_fast_rca(alert_name: str) -> tuple[str, str, dict] | None:
    """Check if an alert has a fast-RCA handler. Returns (toolset, tool, params) or None."""
    return _REGISTRY.get(alert_name)


def synthesize_fast_rca(llm, checks: dict, alert_name: str) -> dict:
    """
    Single fast_model LLM call to classify the root cause from check results.

    Returns dict with: root_cause, confidence, scenario, impact, suggested_fix, evidence_summary
    """
    prompt = f"""You are an SRE analyzing a "{alert_name}" alert. Classify the root cause from these parallel check results.

## Check Results
{json.dumps(checks, indent=2)}

## Decision Tree
- **Scenario A (Pods DOWN)**: pod_status shows 0/0 or CrashLoopBackOff + pod_events shows OOMKilled/Evicted → Root cause: OOM or eviction
- **Scenario B (Pods UP + SQL errors)**: pod_logs contains "integer out of range" or "value too long" or "BATCH_INSERT" with sqlState → Root cause: SQL data type overflow (integer overflow or varchar overflow)
- **Scenario C (Pods UP + connection errors)**: pod_logs contains "connection refused" or "too many connections" + rds_cpu is high → Root cause: Database overload
- **Scenario D (Pods UP + Redis errors)**: pod_logs contains "CLUSTERDOWN" or "NOGROUP" or redis_health shows bandwidth exceeded → Root cause: Redis cluster issue
- **Scenario E (Pods UP + no errors + drain_rate > 0)**: Drainer is actually processing queries, metric may be stale → Root cause: False alarm / stale metric
- **Scenario F (Pods UP + stop_metric active)**: stop_metric value is > 0, drainer intentionally stopped → Root cause: Drainer stopped (check why stop was triggered)

Respond ONLY with valid JSON (no markdown fences):
{{"root_cause": "one-line description", "confidence": "high|medium|low", "scenario": "A|B|C|D|E|F", "impact": "what is broken for users", "suggested_fix": "immediate action", "evidence_summary": "2-3 key facts from checks"}}"""

    try:
        raw = llm.summarize(prompt).strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Fast RCA synthesis failed: {e}")
        return {
            "root_cause": "Unable to classify — check results available for deep investigation",
            "confidence": "low",
            "scenario": "unknown",
            "impact": "Drainer may be down, DB writes could be delayed",
            "suggested_fix": "Wait for deep investigation",
            "evidence_summary": str(e),
        }


def format_slack_message(result: dict, title: str) -> str:
    """Format fast RCA result as Slack mrkdwn."""
    confidence = result.get("confidence", "low")
    scenario = result.get("scenario", "?")

    if confidence == "high":
        icon = ":large_green_circle:"
    elif confidence == "medium":
        icon = ":large_yellow_circle:"
    else:
        icon = ":red_circle:"

    lines = [
        f":zap: *Fast RCA: {title}*",
        "",
        f"{icon} *Confidence:* {confidence.upper()} (Scenario {scenario})",
        f":mag: *Root Cause:* {result.get('root_cause', 'Unknown')}",
        f":warning: *Impact:* {result.get('impact', 'Unknown')}",
        f":wrench: *Suggested Fix:* {result.get('suggested_fix', 'N/A')}",
        "",
        f"_Evidence: {result.get('evidence_summary', 'N/A')}_",
        "",
        ":hourglass_flowing_sand: _Deep investigation in progress — full RCA with PDF will follow in this thread..._",
    ]
    return "\n".join(lines)
