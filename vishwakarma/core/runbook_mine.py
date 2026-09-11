"""Runbook mining — cluster confirmed incidents and write runbooks from them."""
import logging
import math

log = logging.getLogger(__name__)

CONFIRMED_SQL = """
SELECT i.id, i.title, i.analysis, i.labels
FROM incidents i
JOIN evidence_snapshots e ON e.incident_id = i.id
WHERE e.outcome = 'correct' AND i.analysis IS NOT NULL AND i.created_at >= ?
ORDER BY i.created_at DESC
LIMIT ?
"""


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def cluster_rcas(items: list[dict], vecs: list[list[float]],
                 threshold: float = 0.72) -> list[list[dict]]:
    """Greedy single-pass clustering against the first centroid over threshold."""
    clusters: list[list[dict]] = []
    centroids: list[list[float]] = []
    for item, vec in zip(items, vecs):
        for k, cvec in enumerate(centroids):
            if cosine(vec, cvec) >= threshold:
                clusters[k].append(item)
                break
        else:
            clusters.append([item])
            centroids.append(vec)
    return clusters


def build_draft_prompt(cluster: list[dict]) -> str:
    """User-message asking the main LLM to draft one runbook from a cluster."""
    parts = ["Draft ONE runbook (markdown) generalising these confirmed RCAs. "
             "Sections: ## 0 Match (alert names), ## 1 First checks, "
             "## 2 Diagnosis, ## 3 Fix, ## 4 Prevention. Use <placeholder> for "
             "cluster-specific values; reference the Site Knowledge Base. End "
             "with EXACTLY this trailer line:\n"
             f"> Provenance: mined from {len(cluster)} incidents (ids: "
             + ", ".join(i["id"] for i in cluster) + "), unverified"]
    for i in cluster:
        parts.append(f"\n--- incident {i['id']}: {i['title']} ---\n{i['analysis']}")
    return "\n".join(parts)


def mine(items: list[dict], embed_fn, draft_fn, save_fn,
         min_cluster: int = 2, threshold: float = 0.72) -> list[str]:
    """Returns the content of every runbook produced."""
    if not items:
        return []
    vecs = embed_fn([f"{i['title']}\n{i['analysis']}" for i in items])
    if vecs is None:
        vecs = [[float(hash(i["id"]) % 997), 0.0] for i in items]
    out = []
    for cluster in cluster_rcas(items, vecs, threshold):
        if len(cluster) < min_cluster:
            log.info(f"mine: skip singleton cluster ({cluster[0]['title']!r})")
            continue
        content = draft_fn(cluster)
        save_fn(runbook_id=f"mined-{cluster[0]['id']}",
                title=f"Mined: {cluster[0]['title']}"[:200],
                content_md=content)
        out.append(content)
    return out
