"""
Argus — the RCA bot. Triggered by the team's existing escalation convention.

The team already tags `@mre` when something is wrong; Argus watches channel
messages for that subteam mention (bots can't be members of Slack user
groups, but the bot receives all channel messages, so it matches the
`<!subteam^...>` id in the text). A direct `@Argus` mention also triggers.
Zero new behavior for users.

Trigger → light issue-vs-noise check (the @mre context already implies an
issue, so the prior is strongly YES; only clear non-issues like thanks/FYI
stay quiet) → build an Issue → dispatch (orchestrator: cloud-route +
enqueue; executors investigate and post the RCA as Argus).

Sage (`bot/slack.py`) is untouched — it stays chat/commands only.

Config (config.yaml):
  argus:
    bot_token: xoxb-...        # the Argus Slack app
    app_token: xapp-...
    mre_group_id: S030U6CJU8M  # the @mre user group id
Env: ARGUS_BOT_TOKEN / ARGUS_APP_TOKEN / ARGUS_MRE_GROUP_ID
"""
import logging
import re
import time
import uuid
from typing import Callable

log = logging.getLogger(__name__)

# Don't trigger twice for the same Slack message (edits, Slack re-delivery).
_SEEN_TTL = 600


class ArgusBot:
    """
    Pure message-handling core — no Slack SDK here, so it's fully testable.
    `dispatch(issue_payload)` and `classify(text) -> bool` are injected.
    """

    def __init__(
        self,
        mre_group_id: str,
        bot_user_id: str,
        dispatch: Callable[[dict], str],     # returns incident_id
        classify: Callable[[str], bool],     # True = real issue report
        say: Callable[[str, str, str], None] | None = None,  # (channel, thread_ts, text)
    ):
        self.mre_group_id = mre_group_id
        self.bot_user_id = bot_user_id
        self.dispatch = dispatch
        self.classify = classify
        self.say = say or (lambda *_: None)
        self._seen: dict[str, float] = {}

    # ── Decision logic ────────────────────────────────────────────────────────

    def handle_message(self, event: dict) -> str:
        """
        Process one Slack message event.
        Returns the action taken: 'investigate' | 'quiet' | 'ignore'.
        """
        text = (event.get("text") or "").strip()
        ts = event.get("ts", "")
        channel = event.get("channel", "")

        if not text or not ts:
            return "ignore"
        if event.get("bot_id"):           # never trigger on bot messages
            return "ignore"
        if self._already_seen(ts):
            return "ignore"

        mre_tagged = bool(self.mre_group_id) and f"<!subteam^{self.mre_group_id}>" in text
        argus_tagged = bool(self.bot_user_id) and f"<@{self.bot_user_id}>" in text
        if not (mre_tagged or argus_tagged):
            return "ignore"

        clean = self._strip_mentions(text)

        # Direct @Argus or explicit debug → always investigate, no filter.
        forced = argus_tagged or re.match(r"^\s*debug\b", clean, re.IGNORECASE)

        if not forced:
            # @mre context: strong prior toward issue; only clear non-issues
            # (thanks/FYI/scheduling chatter) stay quiet.
            try:
                is_issue = self.classify(clean)
            except Exception as e:
                log.warning(f"Argus noise filter failed ({e}) — treating as issue")
                is_issue = True
            if not is_issue:
                log.info(f"Argus: @mre message classified non-issue — staying quiet: {clean[:80]}")
                return "quiet"

        payload = self._build_payload(event, clean)
        try:
            incident_id = self.dispatch(payload)
        except Exception as e:
            log.error(f"Argus dispatch failed: {e}", exc_info=True)
            self.say(channel, event.get("thread_ts") or ts,
                     f":warning: Argus couldn't start the investigation: {str(e)[:120]}")
            return "quiet"

        log.info(f"Argus: investigating '{clean[:60]}' [{incident_id}]")
        return "investigate"

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_payload(self, event: dict, clean_text: str) -> dict:
        title = clean_text.splitlines()[0][:140] or "Slack-reported issue"
        image_urls = [
            f.get("url_private")
            for f in (event.get("files") or [])
            if str(f.get("mimetype", "")).startswith("image/") and f.get("url_private")
        ]
        return {
            "id": str(uuid.uuid4()),
            "title": title,
            "source": "slack-argus",
            "description": clean_text,
            "severity": "high",
            "labels": {
                "slack_channel": event.get("channel", ""),
                "slack_ts": event.get("ts", ""),
                "slack_thread_ts": event.get("thread_ts") or event.get("ts", ""),
                "reporter": event.get("user", ""),
            },
            "raw": {"slack_event": {k: event.get(k) for k in
                                    ("channel", "ts", "thread_ts", "user")},
                    "image_urls": image_urls},
        }

    def _strip_mentions(self, text: str) -> str:
        text = re.sub(r"<!subteam\^[A-Z0-9]+(\|[^>]*)?>", "", text)
        text = re.sub(r"<@[A-Z0-9]+>", "", text)
        return text.strip(" \t:,-")

    def _already_seen(self, ts: str) -> bool:
        now = time.time()
        if len(self._seen) > 512:
            self._seen = {k: v for k, v in self._seen.items() if now - v < _SEEN_TTL}
        if ts in self._seen:
            return True
        self._seen[ts] = now
        return False


# ── Default noise filter (fast model) ─────────────────────────────────────────

def make_classifier(llm) -> Callable[[str], bool]:
    """
    Issue-vs-noise check with a strong prior toward 'issue' — the @mre tag
    already signals escalation. Returns True (investigate) unless the model
    clearly answers NO for thanks/FYI/scheduling chatter.
    """
    def classify(text: str) -> bool:
        prompt = (
            "A message tagged the on-call escalation group. Decide if it reports "
            "an actual problem/incident/bug that should be investigated, or is "
            "clearly NOT one (thanks, FYI, scheduling, congratulations).\n"
            f"Message: {text[:600]}\n\n"
            "Answer with exactly one word: YES (problem report — when in any "
            "doubt answer YES) or NO (clearly not)."
        )
        try:
            answer = llm.summarize(prompt).strip().upper()
            return not answer.startswith("NO")
        except Exception:
            return True  # fail open — better a spurious investigation than a missed incident
    return classify


# ── Slack wiring (Socket Mode) ────────────────────────────────────────────────

def make_dispatcher(config) -> Callable[[dict], str]:
    """
    Dispatch a Slack-reported issue into the investigation pipeline:
    orchestrator topology → cloud-route + enqueue; all-in-one → in-process.
    """
    def dispatch(payload: dict) -> str:
        import json as _json
        from vishwakarma.core.issue import Issue
        from vishwakarma.storage import dedup as _dedup
        from vishwakarma.storage.queries import alert_fingerprint

        issue = Issue.model_validate(payload)
        fingerprint = alert_fingerprint(
            {"alertname": issue.title, "service": issue.labels.get("slack_channel", "")})
        if not _dedup.try_acquire(fingerprint):
            raise RuntimeError("an investigation for this report is already running")
        incident_id = str(uuid.uuid4())

        if getattr(config, "role", "") == "orchestrator":
            from vishwakarma.core.cloud_router import route_issue
            from vishwakarma.core import jobstream
            cloud = route_issue(issue, default_cloud=config.default_cloud)
            jobstream.enqueue(cloud, {
                "incident_id": incident_id,
                "fingerprint": fingerprint,
                "cloud": cloud,
                "issue": _json.loads(issue.model_dump_json()),
            })
        else:
            import asyncio
            import threading
            from vishwakarma.server import _run_alert_investigation, _state

            def _bg():
                asyncio.run(_run_alert_investigation(
                    config, _state, issue, incident_id, fingerprint))
            threading.Thread(target=_bg, daemon=True).start()
        return incident_id
    return dispatch


def start_argus(config) -> None:
    """Start the Argus Slack app (Socket Mode) in a background thread."""
    bot_token = getattr(config, "argus_bot_token", "")
    app_token = getattr(config, "argus_app_token", "")
    if not (bot_token and app_token):
        log.info("Argus not configured (argus.bot_token/app_token) — skipping")
        return

    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    import threading

    app = App(token=bot_token)
    bot_user_id = app.client.auth_test()["user_id"]

    llm = config.make_llm()
    argus = ArgusBot(
        mre_group_id=getattr(config, "argus_mre_group_id", ""),
        bot_user_id=bot_user_id,
        dispatch=make_dispatcher(config),
        classify=make_classifier(llm),
        say=lambda ch, ts, text: app.client.chat_postMessage(
            channel=ch, thread_ts=ts, text=text),
    )

    @app.event("message")
    def on_message(event, say):  # noqa: ANN001
        argus.handle_message(event)

    @app.event("app_mention")
    def on_mention(event, say):  # noqa: ANN001
        argus.handle_message(event)

    handler = SocketModeHandler(app, app_token)
    threading.Thread(target=handler.start, daemon=True).start()
    log.info(f"⚡ Argus bot running (user={bot_user_id}, "
             f"mre_group={getattr(config, 'argus_mre_group_id', '') or 'unset'})")
