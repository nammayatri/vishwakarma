"""
Self-observability — Prometheus metrics for the agent itself.

Dependency-free: a tiny in-process registry + text exposition (we don't want
to pull prometheus_client into the base image just for a handful of series).
Exposed at GET /metrics. Pairs with the dead-man's-switch alert.
"""
import threading

_lock = threading.Lock()
_counters: dict[tuple[str, tuple], float] = {}
_gauges: dict[tuple[str, tuple], float] = {}

# (name, help, type)
_DESCRIBED: dict[str, tuple[str, str]] = {}


def _key(name: str, labels: dict | None):
    return (name, tuple(sorted((labels or {}).items())))


def describe(name: str, help_text: str, mtype: str) -> None:
    _DESCRIBED[name] = (help_text, mtype)


def inc(name: str, value: float = 1.0, labels: dict | None = None) -> None:
    with _lock:
        k = _key(name, labels)
        _counters[k] = _counters.get(k, 0.0) + value


def set_gauge(name: str, value: float, labels: dict | None = None) -> None:
    with _lock:
        _gauges[_key(name, labels)] = value


def _fmt_labels(label_tuple: tuple) -> str:
    if not label_tuple:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in label_tuple)
    return "{" + inner + "}"


def render() -> str:
    """Prometheus text exposition format."""
    lines: list[str] = []
    emitted_help: set[str] = set()

    def emit(store: dict, default_type: str):
        for (name, labels), val in sorted(store.items()):
            if name not in emitted_help:
                help_text, mtype = _DESCRIBED.get(name, ("", default_type))
                if help_text:
                    lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {mtype}")
                emitted_help.add(name)
            lines.append(f"{name}{_fmt_labels(labels)} {val}")

    with _lock:
        emit(_counters, "counter")
        emit(_gauges, "gauge")
    return "\n".join(lines) + "\n"


# Standard series
describe("vk_investigations_started_total", "Investigations started", "counter")
describe("vk_investigations_completed_total", "Investigations completed", "counter")
describe("vk_investigations_failed_total", "Investigations failed", "counter")
describe("vk_queue_depth", "Job stream depth per cloud", "gauge")
describe("vk_queue_pending", "Delivered-but-unacked jobs per cloud", "gauge")
describe("vk_llm_tokens_total", "LLM tokens consumed", "counter")
