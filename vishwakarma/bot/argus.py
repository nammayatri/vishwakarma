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
        fetch_thread: Callable[[str, str], list[str]] | None = None,  # (channel, thread_ts) -> messages
        platform: str = "slack",             # stamped into issue.source/labels — lets
                                              # _do_investigation pick the right destination
                                              # (e.g. "xyne" for the Xyne-flavored instance)
    ):
        self.mre_group_id = mre_group_id
        self.bot_user_id = bot_user_id
        self.dispatch = dispatch
        self.classify = classify
        self.say = say or (lambda *_: None)
        self.fetch_thread = fetch_thread
        self.platform = platform
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

        # Explicit `debug …` always investigates, skipping every filter.
        explicit_debug = bool(re.match(r"^\s*debug\b", clean, re.IGNORECASE))
        # A screenshot/image attached = a reported issue (e.g. an OA-screen bug) —
        # never treat as trivial.
        has_image = any(str(f.get("mimetype", "")).startswith("image/")
                        for f in (event.get("files") or []))

        if not explicit_debug and not has_image:
            # Trivial greeting / capability question / chit-chat → quick reply,
            # NOT a full investigation. Applies to BOTH direct @Argus and @mre
            # (this is the "@Argus hi" fix — don't burn an RCA on a greeting).
            if self._is_trivial(clean):
                self.say(channel, event.get("thread_ts") or ts,
                         ":wave: I'm *Argus*. Tag me with an issue — an error/stack trace, "
                         "an alert, a failing pod or service, or `debug <question>` — and I'll "
                         "investigate and post an RCA. (For chat, ping *Sage*.)")
                log.info(f"Argus: trivial message — quick reply, no investigation: {clean[:60]}")
                return "quiet"

            # @mre context (not a direct @Argus): strong prior toward issue, but
            # let the classifier drop clear non-issues (thanks/FYI/scheduling).
            if not argus_tagged:
                try:
                    is_issue = self.classify(clean)
                except Exception as e:
                    log.warning(f"Argus noise filter failed ({e}) — treating as issue")
                    is_issue = True
                if not is_issue:
                    log.info(f"Argus: @mre message classified non-issue — quiet: {clean[:80]}")
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
        # If mentioned INSIDE an existing thread, pull the whole discussion so the
        # RCA has the full context of what's been reported/tried, not just the @mention.
        description = clean_text
        thread_ts = event.get("thread_ts")
        if thread_ts and thread_ts != event.get("ts") and self.fetch_thread:
            try:
                msgs = self.fetch_thread(event.get("channel", ""), thread_ts)
                if msgs:
                    convo = "\n".join(msgs)[:6000]
                    description = (
                        f"{clean_text}\n\n--- Full Slack thread (the ongoing discussion "
                        f"this was raised in — use it as context for the RCA) ---\n{convo}"
                    )
            except Exception as e:
                log.debug(f"Argus thread fetch failed: {e}")
        return {
            "id": str(uuid.uuid4()),
            "title": title,
            "source": f"{self.platform}-argus",
            "description": description,
            "severity": "high",
            "labels": {
                "platform": self.platform,
                # kept as "slack_*" even for non-Slack platforms (e.g. Xyne) —
                # _do_investigation already reads these exact keys to resolve
                # the reply channel/thread; only the "platform" label above
                # determines which client/destination it uses to post there.
                "slack_channel": event.get("channel", ""),
                "slack_ts": event.get("ts", ""),
                "slack_thread_ts": event.get("thread_ts") or event.get("ts", ""),
                "reporter": event.get("user", ""),
            },
            "raw": {"slack_event": {k: event.get(k) for k in
                                    ("channel", "ts", "thread_ts", "user")},
                    "image_urls": image_urls},
        }

    # Issue signals — if any appear, it's NOT trivial (investigate).
    _ISSUE_SIGNALS = (
        "error", "errors", "fail", "down", "5xx", "4xx", "500", "503", "timeout",
        "why", "fix", "issue", "alert", "log", "logs", "pod", "crash", "oom",
        "slow", "lag", "drop", "spike", "stuck", "exception", "broke", "broken",
        "restart", "cannot", "can't", "not working", "throttl", "latency",
        "high cpu", "memory", "eviction", "deadlock", "panic", "fatal", "503",
        "investigate", "debug", "rca", "root cause", "duplicate key", "constraint",
    )
    _GREETINGS = {
        "hi", "hello", "hey", "yo", "sup", "hola", "gm", "good morning",
        "good evening", "good afternoon", "thanks", "thank you", "ty", "thx",
        "ok", "okay", "cool", "nice", "test", "ping", "pong", "great", "lol",
        "what can you do", "who are you", "help", "hru", "how are you",
    }

    def _is_trivial(self, clean: str) -> bool:
        """Greeting / capability question / chit-chat with no issue content."""
        c = clean.lower().strip(" \t:,.!?-")
        if not c:
            return True
        if any(s in c for s in self._ISSUE_SIGNALS):
            return False                      # has issue content → investigate
        if c in self._GREETINGS:
            return True
        return len(c.split()) <= 4            # short + no issue signal → trivial

    def _strip_mentions(self, text: str) -> str:
        text = re.sub(r"<!subteam\^[A-Z0-9]+(\|[^>]*)?>", "", text)
        # Slack IDs are uppercase (U0AM456U726); Xyne's are lowercase
        # (cmsoaadid08c913ivwi8351xn) — match both.
        text = re.sub(r"<@[A-Za-z0-9]+>", "", text)
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


def _fetch_thread_msgs(client, channel: str, thread_ts: str, bot_user_id: str) -> list[str]:
    """Return the thread's messages as '<user>: text' lines (oldest→newest), so an
    @Argus mention inside a thread investigates with the whole discussion as context."""
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=100)
    except Exception:
        return []
    out: list[str] = []
    for m in resp.get("messages", []):
        txt = (m.get("text") or "").strip()
        if not txt:
            continue
        who = m.get("user") or m.get("username") or m.get("bot_id") or "?"
        who = "Argus" if who == bot_user_id else who
        out.append(f"<{who}>: {txt}")
    return out


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
        fetch_thread=lambda ch, tts: _fetch_thread_msgs(app.client, ch, tts, bot_user_id),
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
