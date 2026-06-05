"""
Hybrid runbook matching — parallel recall → RRF merge → optional LLM rerank.

NOT a short-circuit cascade: an exact-map hit must not hide a better semantic
match, so all recall legs run and Reciprocal Rank Fusion merges them.

Legs (over cloud-filtered, active runbooks):
  exact-map  — normalized alert key → alert_runbook_map (strongest signal)
  keyword    — runbook.keywords[] substring match against the alert text
  vector     — embedding cosine over runbook embeddings (when provider set)

Rerank: a single fast-model call scores the merged candidates — but only
when there are >3 candidates (bounded cost: at most one LLM call, usually
zero for well-mapped alerts).

Used by config.load_matching_runbooks (DB-backed path) and exposed to the
agent as the runbook_search tool for post-recon re-retrieval.
"""
import json
import logging

log = logging.getLogger(__name__)

RRF_K = 60                 # standard RRF constant
TOP_K_PER_LEG = 8
MAX_RETURNED = 3
VECTOR_MIN_SCORE = 0.35
RERANK_THRESHOLD = 3       # rerank only when candidates exceed this

# RRF weight per leg — exact map is the strongest signal.
_LEG_WEIGHTS = {"exact": 2.0, "keyword": 1.0, "vector": 1.0}


def match_runbooks(
    alert_text: str,
    cloud: str = "",
    llm=None,
    top_k: int = MAX_RETURNED,
) -> list[dict]:
    """
    Find the best runbooks for an alert/query.

    alert_text: alert name for upfront matching, or a recon-enriched query
                from the runbook_search tool.
    cloud:      'aws'|'gcp' filters eligibility ('' = no filter).
    llm:        VishwakarmaLLM for the rerank leg (optional).

    Returns runbook dicts (id, title, content_md, ...) best-first.
    """
    from vishwakarma.storage import runbooks as rb

    candidates = {r["id"]: r for r in rb.list_runbooks(status="active", cloud=cloud or None)}
    if not candidates:
        return []

    rankings: dict[str, list[str]] = {}

    # Leg 1 — exact map on normalized key
    rankings["exact"] = [rid for rid in rb.mapped_runbook_ids(alert_text) if rid in candidates]

    # Leg 2 — keyword overlap, ranked by number of matched keywords
    text_l = alert_text.lower()
    kw_scored = []
    for rid, r in candidates.items():
        hits = sum(1 for kw in r.get("keywords", []) if kw and kw in text_l)
        if hits:
            kw_scored.append((rid, hits))
    kw_scored.sort(key=lambda x: -x[1])
    rankings["keyword"] = [rid for rid, _ in kw_scored[:TOP_K_PER_LEG]]

    # Leg 3 — vector similarity (best-effort)
    rankings["vector"] = []
    try:
        from vishwakarma.core.embeddings import get_client
        from vishwakarma.storage.vectors import search_similar
        emb = get_client()
        if emb.configured:
            qvec = emb.embed_one(alert_text)
            if qvec:
                rankings["vector"] = [
                    rid for rid, _ in search_similar(
                        "runbook", qvec, top_k=TOP_K_PER_LEG, min_score=VECTOR_MIN_SCORE)
                    if rid in candidates
                ]
    except Exception as e:
        log.debug(f"Vector leg skipped: {e}")

    # RRF merge
    scores: dict[str, float] = {}
    for leg, ranked in rankings.items():
        w = _LEG_WEIGHTS.get(leg, 1.0)
        for rank, rid in enumerate(ranked):
            scores[rid] = scores.get(rid, 0.0) + w / (RRF_K + rank + 1)
    if not scores:
        return []
    merged = sorted(scores, key=lambda rid: -scores[rid])

    # Single LLM rerank — only when the merged set is genuinely ambiguous
    if llm is not None and len(merged) > RERANK_THRESHOLD:
        merged = _llm_rerank(llm, alert_text, merged, candidates) or merged

    return [candidates[rid] for rid in merged[:top_k]]


def _llm_rerank(llm, alert_text: str, merged: list[str], candidates: dict) -> list[str] | None:
    """One fast-model call: order candidate ids by relevance. None on failure."""
    listing = "\n".join(
        f"- {rid}: {candidates[rid]['title']} (keywords: {', '.join(candidates[rid].get('keywords', [])[:6])})"
        for rid in merged[:12]
    )
    prompt = (
        "You match infrastructure alerts to investigation runbooks.\n"
        f"Alert/context: {alert_text[:500]}\n\n"
        f"Candidate runbooks:\n{listing}\n\n"
        "Return ONLY a JSON array of the candidate ids ordered most-relevant "
        "first, dropping any that are irrelevant. Example: [\"id-a\", \"id-b\"]"
    )
    try:
        response = llm.summarize(prompt)
        ids = json.loads(response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        valid = [i for i in ids if i in candidates]
        return valid or None
    except Exception as e:
        log.debug(f"Rerank failed ({e}) — keeping RRF order")
        return None
