"""Weekly on-call handoff summary."""
import logging
import threading
import time

log = logging.getLogger(__name__)

WINDOW_S = 7 * 86400


def collect_week(rows: list[dict], now: float, window_s: int = WINDOW_S) -> list[dict]:
    return [r for r in rows if (r.get("created_at") or 0) >= now - window_s]


def build_handoff(rows: list[dict]) -> str:
    open_rows = [r for r in rows if r.get("status") == "open"]
    resolved = [r for r in rows if r.get("status") != "open"]
    lines = ["# On-call handoff", "",
             "## What happened",
             f"- {len(rows)} incidents this week: {len(resolved)} resolved, {len(open_rows)} open"]
    for r in rows:
        lines.append(f"- [{r.get('status', '?')}] {r.get('title', '?')} (id {r.get('id')})")
    lines += ["", "## What it means"]
    lines += (["- Recurring titles below — candidates for new/drafted runbooks"]
              if len(rows) > 3 else ["- Quiet week — no pattern to promote"])
    lines += ["", "## What to do", "- Review and close the open incidents listed above"]
    return "\n".join(lines)


def run_once(rows: list[dict], summarize, post_fn) -> bool:
    if not rows:
        log.info("handoff: no incidents this week — skipping post")
        return False
    post_fn(summarize(build_handoff(rows)))
    return True


def start_handoff_scheduler(config, post_fn) -> None:
    hc = getattr(config, "handoff", None) or {}
    if not hc.get("enabled"):
        return
    def loop():
        while True:
            time.sleep(3600)
            if time.strftime("%u-%H") == f"{hc.get('weekday', '1')}-{hc.get('hour', '04')}":
                try:
                    from vishwakarma.storage.db import _get_conn
                    rows = [dict(r) for r in _get_conn().execute(
                        "SELECT id,title,status,analysis,created_at,labels FROM incidents "
                        "ORDER BY created_at DESC LIMIT 200").fetchall()]
                    llm = config.make_llm()
                    run_once(collect_week(rows, time.time()), llm.summarize, post_fn)
                except Exception as e:
                    log.warning(f"handoff run failed: {e}")
    threading.Thread(target=loop, daemon=True, name="handoff").start()
