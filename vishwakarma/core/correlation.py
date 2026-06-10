"""
Incident correlation — group an alert storm into one investigation.

A cascading outage fires MANY DIFFERENT alerts for one underlying incident
(pod OOM → 5xx → latency → DB connections → ...). Fingerprint dedup only
catches identical re-fires, so without correlation the fleet runs N competing
investigations of one incident, burning gateway tokens and spamming Slack.

Correlation is deliberately CONSERVATIVE: an alert is grouped into an active
investigation only when it shares a strong entity (same service+namespace, or
same cluster) within a short time window. When unsure, it does NOT group —
better an extra investigation than a missed second incident.

Backed by the control Redis (key → active incident_id, TTL = window); falls
back to in-memory for single-pod/no-Redis. Grouped alerts are recorded in
correlated_alerts for the console + as added RCA context.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)

WINDOW_SECONDS = 900        # covers a long investigation + the storm burst;
                            # refreshed on each correlated hit, unlinked on done
_KEY_PREFIX = "vk:corr:"

_redis = None
_local: dict[str, tuple[str, float]] = {}   # key -> (incident_id, expires_at)
_local_lock = threading.Lock()


def init_correlation(redis_url: str = "") -> None:
    global _redis
    if not redis_url:
        _redis = None
        return
    try:
        import redis as redis_lib
        _redis = redis_lib.Redis.from_url(
            redis_url, socket_timeout=3, socket_connect_timeout=3, decode_responses=True)
        _redis.ping()
        log.info("Incident correlation: Redis-backed")
    except Exception as e:
        _redis = None
        log.warning(f"Incident correlation: Redis unavailable ({e}) — in-memory")


def correlation_key(labels: dict) -> str:
    """
    Strong-entity key. Prefer service+namespace; else cluster; else ''
    (no key → never correlate, always investigate).
    """
    def norm(v) -> str:
        return str(v or "").lower().strip()

    svc = norm(labels.get("service") or labels.get("app") or labels.get("job"))
    ns = norm(labels.get("namespace"))
    if svc and ns:
        return f"svc:{svc}/{ns}"
    if svc:
        return f"svc:{svc}"
    cluster = norm(labels.get("cluster") or labels.get("cluster_name"))
    if cluster:
        return f"cluster:{cluster}"
    # Fallback: many alerts carry only `alertname` (no service/namespace), e.g.
    # GCPDriverDrainerNotProcessing + GCPNoDriverDrainerRunning are one incident.
    # Group by the alertname's core entity tokens (cloud/state words stripped).
    stem = _alertname_stem(labels.get("alertname", ""))
    if stem:
        return f"alert:{stem}"
    return ""   # too vague to correlate


# Tokens that don't identify the SUBSYSTEM — stripped so different states of the
# same entity share a stem (Running/NotProcessing/High/Low/...).
_STEM_NOISE = {
    "gcp", "aws", "prod", "production", "staging", "no", "not", "is", "are",
    "the", "a", "high", "low", "critical", "warning", "sev1", "sev2", "sev3",
    "increasing", "decreasing", "rising", "dropped", "drop", "down", "up",
    "running", "processing", "dead", "alive", "failing", "failed", "failure",
    "error", "errors", "alert", "alerts", "issue", "problem", "pod", "pods",
    "count", "rate", "ratio", "percent", "usage", "exceeded", "threshold",
}


def _alertname_stem(alertname: str) -> str:
    """Core entity tokens of an alertname, sorted+joined. Needs >=2 to group
    (one generic token like 'redis' is too broad → '')."""
    import re
    if not alertname:
        return ""
    # split camelCase + separators: GCPDriverDrainerNotProcessing -> tokens
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+", alertname)
    toks = [p.lower() for p in parts if p]
    sig = [t for t in toks if t not in _STEM_NOISE and len(t) > 1]
    # dedup preserve, then sort for order-independence
    seen = []
    for t in sig:
        if t not in seen:
            seen.append(t)
    return "+".join(sorted(seen)) if len(seen) >= 2 else ""


def find_correlated(key: str) -> str | None:
    """Return the active investigation's incident_id for this key, if any."""
    if not key:
        return None
    if _redis is not None:
        try:
            return _redis.get(_KEY_PREFIX + key)
        except Exception:
            pass
    now = time.time()
    with _local_lock:
        v = _local.get(key)
        if v and v[1] > now:
            return v[0]
    return None


def link(key: str, incident_id: str, ttl: int = WINDOW_SECONDS) -> None:
    """Mark this entity as having an active investigation (refreshes the window)."""
    if not key:
        return
    if _redis is not None:
        try:
            _redis.set(_KEY_PREFIX + key, incident_id, ex=ttl)
            return
        except Exception:
            pass
    with _local_lock:
        _local[key] = (incident_id, time.time() + ttl)


def unlink(key: str) -> None:
    if not key:
        return
    if _redis is not None:
        try:
            _redis.delete(_KEY_PREFIX + key)
        except Exception:
            pass
    with _local_lock:
        _local.pop(key, None)


def record_correlated_alert(parent_id: str, title: str, labels: dict) -> None:
    """Store a grouped alert (console view + RCA context)."""
    import json
    from vishwakarma.storage.db import _get_conn, _lock
    try:
        conn = _get_conn()
        with _lock:
            conn.execute(
                "INSERT INTO correlated_alerts (parent_id, alert_title, alert_labels, created_at) "
                "VALUES (?,?,?,?)",
                (parent_id, title, json.dumps(labels or {}), time.time()),
            )
            conn.commit()
    except Exception as e:
        log.debug(f"record_correlated_alert failed: {e}")


def list_correlated(parent_id: str) -> list[dict]:
    import json
    from vishwakarma.storage.db import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        "SELECT alert_title, alert_labels, created_at FROM correlated_alerts "
        "WHERE parent_id = ? ORDER BY created_at", (parent_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("alert_labels"), str) and d["alert_labels"]:
            try:
                d["alert_labels"] = json.loads(d["alert_labels"])
            except Exception:
                pass
        out.append(d)
    return out
