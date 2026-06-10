"""
LLM gateway API-key pool.

The Acme gateway rate-limits per key (429 max_parallel_requests). With a
fleet of executors + sub-agents + OpenCode sessions all hitting one key, we
saturate instantly. The pool round-robins across N keys and briefly benches a
key that returns 429, so capacity scales with key count.

Thread-safe; a process-wide singleton initialized from config.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)

BENCH_SECONDS = 30   # how long a 429'd key sits out


class KeyPool:
    def __init__(self, keys: list[str]):
        self._keys = [k for k in keys if k]
        self._benched: dict[str, float] = {}   # key -> bench-until epoch
        self._i = 0
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return len(self._keys)

    def get(self) -> str | None:
        """Next available key (round-robin, skipping benched ones)."""
        if not self._keys:
            return None
        now = time.time()
        with self._lock:
            for _ in range(len(self._keys)):
                key = self._keys[self._i % len(self._keys)]
                self._i += 1
                if self._benched.get(key, 0) <= now:
                    return key
            # All benched — return the soonest-free one anyway (better than nothing)
            return min(self._keys, key=lambda k: self._benched.get(k, 0))

    def penalize(self, key: str, seconds: int = BENCH_SECONDS) -> None:
        """Bench a key after a 429."""
        if not key:
            return
        with self._lock:
            self._benched[key] = time.time() + seconds
        log.warning(f"Key benched for {seconds}s after rate limit (…{key[-6:]})")


_pool: KeyPool | None = None


def init_keypool(keys: list[str]) -> None:
    global _pool
    _pool = KeyPool(keys)
    if _pool.size > 1:
        log.info(f"LLM key pool: {_pool.size} keys")


def get_pool() -> KeyPool | None:
    return _pool
