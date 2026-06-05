"""
Event bus — live investigation events for the console UI (SSE).

Two layers behind one publish/subscribe API:
  in-process — always on; subscribers get events from investigations running
               in this pod (all-in-one mode, or the orchestrator's own work)
  Redis pub/sub — when configured: executors publish here so the
               orchestrator's SSE endpoint can fan events from EVERY pod to
               browsers. Channel: vk:events

Events are small dicts: {incident_id, type, ...} — the same event shapes the
engine already yields (step_start, tool_call_start/result, hypothesis,
compaction, done) plus lifecycle markers from the server flow.

Fire-and-forget: publishing must never block or break an investigation.
"""
import json
import logging
import queue
import threading

log = logging.getLogger(__name__)

CHANNEL = "vk:events"
SUBSCRIBER_QUEUE_MAX = 1000   # slow browser → drop oldest, never block

_redis = None
_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()
_listener_started = False


def init_eventbus(redis_url: str = "") -> None:
    """Connect the Redis layer (optional). In-process layer always works."""
    global _redis
    if not redis_url:
        _redis = None
        return
    try:
        import redis as redis_lib
        _redis = redis_lib.Redis.from_url(
            redis_url, socket_timeout=5, socket_connect_timeout=3,
            decode_responses=True,
        )
        _redis.ping()
        log.info("Event bus: Redis pub/sub enabled")
    except Exception as e:
        _redis = None
        log.warning(f"Event bus: Redis unavailable ({e}) — in-process only")


def publish(incident_id: str, event: dict) -> None:
    """Publish one event. Never raises.

    With Redis enabled, events go ONLY via Redis — the listener relays them
    back to local subscribers (avoids double delivery to a pod that both
    publishes and subscribes). Without Redis, deliver in-process directly.
    """
    evt = {"incident_id": incident_id, **event}
    if _redis is not None:
        try:
            _redis.publish(CHANNEL, json.dumps(evt, default=str))
            return
        except Exception as e:
            log.debug(f"Event publish to Redis failed ({e}) — local delivery")
    _deliver_local(evt)


def _deliver_local(evt: dict) -> None:
    with _sub_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(evt)
        except queue.Full:
            try:  # drop oldest, keep the stream flowing
                q.get_nowait()
                q.put_nowait(evt)
            except Exception:
                pass


def subscribe() -> queue.Queue:
    """
    Register a subscriber (one per SSE connection). Returns a Queue of event
    dicts. Call unsubscribe(q) when the connection closes.
    Starts the Redis→local relay on first use so cross-pod events flow in.
    """
    q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
    with _sub_lock:
        _subscribers.append(q)
    _ensure_listener()
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _sub_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def _ensure_listener() -> None:
    """Relay Redis pub/sub → local subscriber queues (orchestrator side)."""
    global _listener_started
    if _listener_started or _redis is None:
        return
    _listener_started = True

    def _listen():
        while True:
            try:
                ps = _redis.pubsub(ignore_subscribe_messages=True)
                ps.subscribe(CHANNEL)
                for msg in ps.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        evt = json.loads(msg["data"])
                    except Exception:
                        continue
                    _deliver_local(evt)
            except Exception as e:
                log.warning(f"Event bus listener reconnecting: {e}")
                import time
                time.sleep(3)

    threading.Thread(target=_listen, daemon=True).start()
