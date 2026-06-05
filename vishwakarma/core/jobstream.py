"""
Job stream — Redis Streams transport between the orchestrator and the
per-cloud executor pools.

Streams:   vk:jobs:aws , vk:jobs:gcp     (one per cloud)
Group:     'executors'                    (one consumer group per stream;
                                           each executor pod = one consumer)

Semantics: at-least-once. A job stays in the consumer's pending list until
XACK'd; if an executor dies mid-job, another executor reclaims it via
XAUTOCLAIM after `min_idle_ms`. Idempotency + resume live in the
investigations table (Phase 0), keyed by incident_id.

'both' jobs are XADD'd to both streams; each cloud investigates its own half.
"""
import json
import logging
import time

log = logging.getLogger(__name__)

GROUP = "executors"
STREAM_PREFIX = "vk:jobs:"
MAXLEN = 10_000          # stream trim — plenty of headroom, bounds memory
DEFAULT_BLOCK_MS = 5_000
DEFAULT_MIN_IDLE_MS = 300_000   # reclaim jobs idle >5 min (executor died)

_redis = None


def init_jobstream(redis_url: str) -> None:
    """Connect and ensure streams + consumer groups exist."""
    global _redis
    import redis as redis_lib
    _redis = redis_lib.Redis.from_url(
        redis_url, socket_timeout=30, socket_connect_timeout=5,
        decode_responses=True,
    )
    _redis.ping()
    for cloud in ("aws", "gcp"):
        stream = STREAM_PREFIX + cloud
        try:
            _redis.xgroup_create(stream, GROUP, id="0", mkstream=True)
        except redis_lib.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
    log.info("Job stream initialized (Redis Streams, groups ready)")


def _r():
    if _redis is None:
        raise RuntimeError("jobstream not initialized — call init_jobstream(redis_url)")
    return _redis


def enqueue(cloud: str, payload: dict) -> list[str]:
    """
    Enqueue a job for one cloud ('aws'|'gcp') or fan out ('both').
    Returns the stream message id(s).
    """
    clouds = ["aws", "gcp"] if cloud == "both" else [cloud]
    ids = []
    body = {"payload": json.dumps(payload), "enqueued_at": str(time.time())}
    for c in clouds:
        if c not in ("aws", "gcp"):
            raise ValueError(f"invalid cloud: {c}")
        msg_id = _r().xadd(STREAM_PREFIX + c, body, maxlen=MAXLEN, approximate=True)
        ids.append(msg_id)
        log.info(f"Enqueued job to {c}: {payload.get('incident_id', '?')} ({msg_id})")
    return ids


def consume(cloud: str, consumer: str, block_ms: int = DEFAULT_BLOCK_MS) -> tuple[str, dict] | None:
    """
    Read one new job for this consumer. Blocks up to block_ms.
    Returns (msg_id, payload) or None on timeout. Job stays pending until ack().
    """
    resp = _r().xreadgroup(
        GROUP, consumer, {STREAM_PREFIX + cloud: ">"}, count=1, block=block_ms
    )
    if not resp:
        return None
    _stream, messages = resp[0]
    msg_id, fields = messages[0]
    return msg_id, json.loads(fields["payload"])


def ack(cloud: str, msg_id: str) -> None:
    """Acknowledge a completed job (removes it from the pending list)."""
    _r().xack(STREAM_PREFIX + cloud, GROUP, msg_id)


def claim_stale(cloud: str, consumer: str,
                min_idle_ms: int = DEFAULT_MIN_IDLE_MS) -> list[tuple[str, dict]]:
    """
    Take over jobs whose owning consumer stopped working them (died mid-job).
    Returns [(msg_id, payload), ...] now owned by `consumer`.
    """
    _next, messages, _deleted = _r().xautoclaim(
        STREAM_PREFIX + cloud, GROUP, consumer,
        min_idle_time=min_idle_ms, start_id="0", count=10,
    )
    out = []
    for msg_id, fields in messages:
        if fields and "payload" in fields:
            out.append((msg_id, json.loads(fields["payload"])))
    if out:
        log.warning(f"Reclaimed {len(out)} stale job(s) on {cloud} for {consumer}")
    return out


def pending_count(cloud: str) -> int:
    """Jobs delivered but not yet acked (observability/UI)."""
    info = _r().xpending(STREAM_PREFIX + cloud, GROUP)
    return int(info.get("pending", 0)) if isinstance(info, dict) else 0


def depth(cloud: str) -> int:
    """Total entries in the stream (queue depth, observability/UI)."""
    return int(_r().xlen(STREAM_PREFIX + cloud))
