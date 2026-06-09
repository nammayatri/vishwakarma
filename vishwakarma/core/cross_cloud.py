"""
Cross-cloud synthesis for `both`-tagged investigations.

A BAP↔BPP incident (e.g. tickets not draining because OnConfirm lands in AWS
but polling runs in GCP) spans both clouds. The orchestrator fans the job to
both executor pools; each investigates ITS cloud and writes its half here
instead of posting individually. The second executor to finish atomically
claims synthesis, merges the two halves into one RCA, and posts once.

Storage:
  cross_cloud_findings   (incident_id, cloud) → rca text
  cross_cloud_synthesis  (incident_id)        → single-claim lock
"""
import json
import logging
import time

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)


def write_finding(incident_id: str, cloud: str, rca: str, meta: dict | None = None) -> None:
    """Persist one cloud's half of a cross-cloud investigation."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO cross_cloud_findings (incident_id, cloud, rca, meta, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(incident_id, cloud) DO UPDATE SET
              rca = excluded.rca, meta = excluded.meta, created_at = excluded.created_at
            """,
            (incident_id, cloud, rca, json.dumps(meta or {}), time.time()),
        )
        conn.commit()


def get_findings(incident_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT cloud, rca, meta FROM cross_cloud_findings WHERE incident_id = ? ORDER BY cloud",
        (incident_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("meta"), str) and d["meta"]:
            try:
                d["meta"] = json.loads(d["meta"])
            except Exception:
                pass
        out.append(d)
    return out


def both_present(incident_id: str) -> bool:
    return {f["cloud"] for f in get_findings(incident_id)} >= {"aws", "gcp"}


def claim_synthesis(incident_id: str, worker_id: str) -> bool:
    """
    Atomically claim the right to synthesize. Returns True for exactly one
    caller (the second-to-finish executor); False if already claimed.
    """
    conn = _get_conn()
    with _lock:
        try:
            conn.execute(
                "INSERT INTO cross_cloud_synthesis (incident_id, worker_id, created_at) "
                "VALUES (?, ?, ?)",
                (incident_id, worker_id, time.time()),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False


def synthesize(llm, incident_title: str, findings: list[dict]) -> str:
    """Merge per-cloud RCAs into one unified cross-cloud RCA via the LLM."""
    halves = "\n\n".join(
        f"### {f['cloud'].upper()} findings\n{f['rca']}" for f in findings
    )
    prompt = (
        "You are synthesizing a CROSS-CLOUD root cause analysis. An incident "
        f"('{incident_title}') was investigated on both AWS and GCP because its "
        "data plane spans both clouds. Below are the two clouds' findings. "
        "Merge them into ONE coherent RCA: identify the single root cause "
        "(often a cross-cloud interaction — e.g. a request handled in one cloud "
        "while its follow-up runs in the other), the evidence chain across both "
        "clouds, and the fix. Be specific about which cloud each piece of "
        "evidence came from.\n\n"
        f"{halves}\n\n"
        "Output the unified RCA in the standard format (Root Cause / Confidence "
        "/ Evidence Chain / Immediate Fix / Prevention)."
    )
    try:
        return llm.summarize(prompt)
    except Exception as e:
        log.warning(f"Cross-cloud synthesis LLM failed ({e}) — concatenating halves")
        return f"# Cross-cloud RCA (auto-merged)\n\n{halves}"
