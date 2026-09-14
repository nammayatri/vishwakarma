"""❌ feedback → drafts a runbook amendment, parked for human review."""
import logging

log = logging.getLogger(__name__)

_PROMPT = """A human rejected this RCA as wrong. Propose an amended runbook body
that would have led to the correct diagnosis. Keep structure, fix the guidance.
Return ONLY the amended markdown.

## Current runbook
{runbook}

## Rejected RCA (incident {incident})
{rca}"""


def propose_amendments(incident_id: str, runbook_ids: list[str], corrected: bool,
                       *, fetch_incident, get_runbook, summarize,
                       save_proposal) -> list[str]:
    if corrected or not runbook_ids:
        return []
    try:
        inc = fetch_incident(incident_id) or {}
    except Exception as e:
        log.warning(f"amendment: cannot fetch incident {incident_id}: {e}")
        return []
    out = []
    for rid in runbook_ids:
        try:
            rb = get_runbook(rid)
            if not rb:
                continue
            md = summarize(_PROMPT.format(runbook=rb["content_md"],
                                          incident=incident_id,
                                          rca=inc.get("analysis", "")[:8000]))
            out.append(save_proposal(runbook_id=rid, incident_id=incident_id,
                                     proposed_md=md,
                                     reason=f"❌ feedback on {incident_id}"))
        except Exception as e:
            log.warning(f"amendment proposal failed for {rid}: {e}")
    return out
