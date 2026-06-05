"""
Active-investigation dedup lock — Redis-backed when configured, in-memory
fallback otherwise.

Prevents two investigations of the same alert fingerprint running at once.
The in-memory set (today's behavior) is only correct for a single pod;
Redis SET NX + TTL makes it correct across pods AND clouds (the control
Redis is shared cross-cloud by design).

TTL guards against leaked locks: if a pod dies without releasing, the lock
self-expires after DEDUP_LOCK_TTL instead of suppressing that alert forever.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)

# A leaked lock (pod crash without release) self-clears after this long.
# Generous enough for the longest code-deep-dive investigations.
DEDUP_LOCK_TTL = 3600  # 1 hour

_KEY_PREFIX = "vk:dedup:"

_redis = None
_redis_url: str = ""

# In-memory fallback (single-pod mode / no Redis configured)
_local: dict[str, float] = {}  # fingerprint -> expires_at
_local_lock = threading.Lock()


def init_dedup(redis_url: str = "") -> None:
    """Configure the backend. Empty url = in-memory fallback."""
    global _redis, _redis_url
    _redis_url = redis_url or ""
    if not _redis_url:
        _redis = None
        log.info("Dedup: in-memory (single-pod) mode")
        return
    try:
        import redis as redis_lib
        _redis = redis_lib.Redis.from_url(
            _redis_url, socket_timeout=3, socket_connect_timeout=3,
            decode_responses=True,
        )
        _redis.ping()
        log.info("Dedup: Redis-backed (multi-pod safe)")
    except Exception as e:
        _redis = None
        log.warning(f"Dedup: Redis unavailable ({e}) — falling back to in-memory")


def try_acquire(fingerprint: str, ttl: int = DEDUP_LOCK_TTL) -> bool:
    """
    Atomically acquire the investigation lock for a fingerprint.
    Returns True if acquired (caller should investigate), False if another
    investigation of this alert is already in flight.
    """
    if _redis is not None:
        try:
            # SET NX EX — atomic acquire with self-expiry.
            return bool(_redis.set(_KEY_PREFIX + fingerprint, "1", nx=True, ex=ttl))
        except Exception as e:
            log.warning(f"Dedup Redis acquire failed ({e}) — using in-memory for this call")
    # In-memory path (or Redis hiccup)
    now = time.time()
    with _local_lock:
        exp = _local.get(fingerprint, 0)
        if exp > now:
            return False
        _local[fingerprint] = now + ttl
        # Opportunistic cleanup of expired entries
        if len(_local) > 256:
            for k in [k for k, v in _local.items() if v <= now]:
                del _local[k]
        return True


def release(fingerprint: str) -> None:
    """Release the lock so the next firing of this alert investigates fresh."""
    if _redis is not None:
        try:
            _redis.delete(_KEY_PREFIX + fingerprint)
        except Exception as e:
            log.warning(f"Dedup Redis release failed ({e})")
    with _local_lock:
        _local.pop(fingerprint, None)


def is_active(fingerprint: str) -> bool:
    """Check without acquiring (observability/UI)."""
    if _redis is not None:
        try:
            return bool(_redis.exists(_KEY_PREFIX + fingerprint))
        except Exception:
            pass
    with _local_lock:
        return _local.get(fingerprint, 0) > time.time()
