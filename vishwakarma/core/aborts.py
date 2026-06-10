"""
Cooperative investigation cancellation.

An abort request marks an incident_id; the engine checks it at each step boundary
and stops cleanly (writes a best-effort partial RCA). In-process (all-in-one) it's
a simple set; for multi-pod, the abort is also persisted to investigations.status
so a worker on another pod sees it on its next checkpoint read.
"""
import threading

_lock = threading.Lock()
_aborted: set[str] = set()


def request_abort(incident_id: str) -> None:
    with _lock:
        _aborted.add(incident_id)


def is_aborted(incident_id: str) -> bool:
    if not incident_id:
        return False
    with _lock:
        if incident_id in _aborted:
            return True
    # Cross-pod: an abort marked in the DB by another pod.
    try:
        from vishwakarma.storage.investigations import get_investigation
        inv = get_investigation(incident_id)
        if inv and inv.get("status") in ("aborting", "aborted"):
            with _lock:
                _aborted.add(incident_id)
            return True
    except Exception:
        pass
    return False


def clear_abort(incident_id: str) -> None:
    with _lock:
        _aborted.discard(incident_id)
