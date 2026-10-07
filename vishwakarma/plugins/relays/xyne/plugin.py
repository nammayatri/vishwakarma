"""
Xyne destination — post investigation results to Xyne, mirroring the Slack flow.

Xyne (spaces.xyne.juspay.net) exposes a Slack-API-compatible REST surface at
https://spaces.xyne.juspay.net/api/apps/slack/ — CONFIRMED live against the
real API (2026-08-11):
  - chat.postMessage: works, auth via `Authorization: Bearer <app JWT>`.
  - Response shape matches Slack exactly: {"ok": false, "error": "..."} on
    failure, HTTP 200 even on API-level failure (never raises via HTTP status
    alone — chat_postMessage/chat_update below check `ok` explicitly).
  - auth.test: works, returns {"ok", "user_id", "bot_id", "user": "Argus",
    "team": "Nammayatri", ...} — same shape Slack's auth.test returns.
  - conversations.replies/.history/.list: work once `channels:read` is
    granted.
  - KNOWN GAP (confirmed live, 2026-08-11): the APP_MENTIONED webhook
    payload's conversationId/messageId are UUID-format internal
    event/delivery ids — NOT the real Xyne message ts (a different
    cuid-style format, e.g. "cmsoj0act44c1v48c1l31jm1g", only obtainable
    from a chat.postMessage response). Passing the webhook's ids as
    thread_ts always fails with {"ok": false, "error": "thread_not_found"}.
    chat_postMessage below retries without thread_ts on that specific
    error, so the reply still lands (as a new top-level message in the
    right channel) instead of being silently dropped — it just can't be
    literally nested under the original mention message, since the webhook
    never exposes a usable id for that. Once the first post in an
    investigation falls back this way, its real returned ts is reused for
    every later post in the same investigation (ack/phase/RCA), so the
    whole conversation still threads together correctly from that point on.
"""
import logging
import re

import requests

from vishwakarma.plugins.relays.slack.plugin import SlackDestination

log = logging.getLogger(__name__)


_EMOJI = {
    "rotating_light": "🚨", "page_facing_up": "📄", "thread": "🧵",
    "hourglass_flowing_sand": "⏳", "hourglass": "⌛", "mag": "🔍",
    "white_check_mark": "✅", "memo": "📝", "warning": "⚠️", "wave": "👋",
    "brain": "🧠", "gear": "⚙️", "chart_with_upwards_trend": "📈",
    "x": "❌", "red_circle": "🔴", "large_green_circle": "🟢",
    "large_yellow_circle": "🟡", "bar_chart": "📊", "rocket": "🚀",
    "mega": "📣", "bulb": "💡", "bug": "🐛", "fire": "🔥",
    "wrench": "🔧", "test_tube": "🧪", "no_entry": "⛔", "no_entry_sign": "🚫",
    "information_source": "ℹ️", "lock": "🔒", "pushpin": "📌", "eyes": "👀",
}


_NO_EMOJIFY_KEYS = ("action_id", "block_id", "type", "value", "actionId", "id", "screenId", "version", "variant")


def _emojify(value):
    if isinstance(value, str):
        return re.sub(r":([a-z0-9_+-]+):", lambda m: _EMOJI.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_emojify(v) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in _NO_EMOJIFY_KEYS else _emojify(v)) for k, v in value.items()}
    return value


_FLOW_STATE = {"values": {}, "touched": {}, "errors": {}, "submitting": False, "submitted": False,
               "history": [], "loadingComponentIds": []}
_FLOW_ACTION_PREFIX = "vk_remediate_"


def _flow_md(text: str) -> str:
    return re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"**\1**", text or "")


def text_flow(text: str, screen_id: str = "argus-outcome") -> dict:
    return {"version": "2.0", "screenId": screen_id,
            "components": [{"id": "t0", "type": "text", "props": {"content": _flow_md(text)}}],
            "state": {**_FLOW_STATE}}


def blocks_to_flow(blocks: list | None) -> dict | None:
    """Slack-style blocks carrying our remediation Approve/Reject buttons become a Xyne Flow UI v2 card
    whose buttons submit `<action_id>:<remediation id>` to the app webhook. Any other blocks are left
    for the Slack-compat layer."""
    def is_ours(b):
        return b.get("type") == "actions" and any(
            str(e.get("action_id", "")).startswith(_FLOW_ACTION_PREFIX) for e in b.get("elements", []))

    if not blocks or not any(is_ours(b) for b in blocks):
        return None
    components = []
    for i, b in enumerate(blocks):
        kind = b.get("type")
        if kind == "section":
            components.append({"id": f"t{i}", "type": "text",
                               "props": {"content": _flow_md((b.get("text") or {}).get("text", ""))}})
        elif kind == "context":
            text = " ".join((e.get("text") or "") for e in b.get("elements", []))
            components.append({"id": f"t{i}", "type": "text", "props": {"content": _flow_md(text)}})
        elif is_ours(b):
            children = []
            for j, e in enumerate(b["elements"]):
                destructive = e.get("style") == "danger"
                children.append({
                    "id": f"btn{i}_{j}", "type": "button",
                    "props": {"label": (e.get("text") or {}).get("text", "OK"),
                              "variant": "destructive" if destructive else "primary",
                              "action": {"type": "submit", "actionId": f"{e['action_id']}:{e.get('value', '')}"}}})
            components.append({"id": f"row{i}", "type": "row", "style": {"gap": "8px"}, "children": children})
    return {"version": "2.0", "screenId": "argus-remediation", "components": components,
            "state": {**_FLOW_STATE}}


class XyneApiError(Exception):
    """Raised when Xyne's Slack-compatible API returns {"ok": false, ...} —
    mirrors slack_sdk's SlackApiError so callers written against the real
    Slack SDK's error-handling behave the same way against Xyne."""


class XyneWebClient:
    """
    Minimal slack_sdk.WebClient-compatible shim — implements only the methods
    SlackDestination/_do_investigation actually call (chat_postMessage,
    chat_update). `base_url` is the full API prefix
    (https://spaces.xyne.juspay.net/api/apps/slack) — methods are appended
    directly, not re-prefixed.
    """

    def __init__(self, base_url: str, token: str):
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})

    def _post(self, method: str, body: dict) -> dict:
        if method in ("chat.postMessage", "chat.update"):
            body = _emojify(body)
        r = self._session.post(f"{self._base_url}/{method}", json=body, timeout=15)
        r.raise_for_status()  # transport-level failures still raise
        data = r.json() if r.content else {}
        if not data.get("ok", False):
            # Xyne returns HTTP 200 even on API-level failure (confirmed) —
            # raise_for_status() above can't catch this; check `ok` explicitly.
            raise XyneApiError(data.get("error", "unknown_error"))
        return data

    def chat_postMessage(self, channel, text: str = "", thread_ts: str | None = None,
                          blocks: list | None = None, attachments: list | None = None,
                          flow: dict | None = None, **_ignored) -> dict:
        body: dict = {"channel": channel, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        flow = flow or blocks_to_flow(blocks)
        if flow:
            body["flow"] = flow
        elif blocks:
            body["blocks"] = blocks
        if attachments:
            body["attachments"] = attachments
        try:
            data = self._post("chat.postMessage", body)
        except XyneApiError as e:
            if thread_ts and str(e) == "thread_not_found":
                # The webhook-supplied thread_ts (see module docstring) is
                # never a real message id — fall back to a top-level post so
                # the reply lands somewhere instead of being dropped. Its
                # real returned ts becomes the thread root for every later
                # post in this investigation (see server.py: ack_ts reuse).
                log.info("Xyne thread_not_found — posting as a new top-level message instead")
                body.pop("thread_ts", None)
                data = self._post("chat.postMessage", body)
            else:
                raise
        return {"ok": True, "ts": data.get("ts", ""), "channel": data.get("channel") or channel}

    def chat_update(self, channel, ts, text: str = "", blocks: list | None = None,
                     flow: dict | None = None, **_ignored) -> dict:
        body: dict = {"channel": channel, "ts": ts, "text": text}
        flow = flow or blocks_to_flow(blocks)
        if flow:
            body["flow"] = flow
        elif blocks:
            body["blocks"] = blocks
        data = self._post("chat.update", body)
        return {"ok": True, "ts": data.get("ts", ts), "channel": data.get("channel") or channel}

    def get_user(self, user_id: str) -> tuple[str, str]:
        if not user_id:
            return "", ""
        try:
            u = self._post("users.info", {"user": user_id}).get("user") or {}
        except Exception as e:
            log.warning(f"Xyne users.info failed for {user_id!r}: {e}")
            return "", ""
        profile = u.get("profile") or {}
        return (u.get("real_name") or u.get("name") or profile.get("display_name") or ""), (profile.get("email") or "")

    def files_upload_v2(self, channel, content: bytes, filename: str, title: str = "",
                         thread_ts: str | None = None, initial_comment: str = "", **_ignored) -> dict:
        slot = self._post("files.getUploadURLExternal", {"filename": filename, "length": len(content)})
        up = self._session.post(slot["upload_url"], data=content, timeout=60,
                                headers={"Content-Type": "application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream"})
        up.raise_for_status()
        body: dict = {"files": [{"id": slot["file_id"], "title": title or filename}],
                      "channel_id": channel}
        if thread_ts:
            body["thread_ts"] = thread_ts
        if initial_comment:
            body["initial_comment"] = _emojify(initial_comment)
        try:
            return self._post("files.completeUploadExternal", body)
        except XyneApiError as e:
            if thread_ts and str(e) == "thread_not_found":
                body.pop("thread_ts", None)
                return self._post("files.completeUploadExternal", body)
            raise

    def auth_test(self) -> dict:
        """Confirmed live — returns {"ok", "user_id", "bot_id", "user", "team", ...},
        same shape as Slack's auth.test. Used to self-discover our bot's own
        user_id (the <@ID> tag to match for mentions) instead of requiring it
        as separate config."""
        return self._post("auth.test", {})


def verify_xyne_signature(signing_secret: str, body: bytes, signature: str) -> bool:
    """
    Xyne's REAL request signature scheme — CONFIRMED from a live webhook
    request (2026-08-11): a single `x-xyne-signature` header, no timestamp
    header at all, value = plain hex-encoded HMAC-SHA256 of the raw request
    body (64 hex chars observed). No "v0:" prefix, no Slack-style basestring
    — this is NOT the Slack request-signing scheme, despite the "signing
    secret" naming suggesting it. (Corrects the original Slack-shaped
    assumption, which never matched a single real request.)
    """
    import hashlib
    import hmac

    if not (signing_secret and signature):
        return False
    computed = hmac.new(signing_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


class XyneDestination(SlackDestination):
    """
    Reuses SlackDestination's entire post_investigation flow (thread-vs-new-message
    decision, text chunking, feedback buttons) — only the transport differs:
    XyneWebClient instead of the real Slack SDK. PDFs go through Xyne's
    files.getUploadURLExternal/completeUploadExternal flow, falling back to
    chunked text if the upload fails.

    Config:
      base_url: https://spaces.xyne.juspay.net
      token: Bearer token for the Xyne API
      channel: default channel if none is passed per-call
    """

    def __init__(self, config: dict):
        self._base_url = config.get("base_url", "").rstrip("/")
        self._token = config.get("token", "")
        self._channel = config.get("channel", "")
        self._mention = ""
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = XyneWebClient(self._base_url, self._token)
        return self._client

    def _resolve_channel_id(self, channel: str) -> str:
        # The Xyne event payload's channel id is assumed directly usable —
        # no known channel-list-lookup equivalent to resolve names against.
        return channel



_channel_cache: dict[str, str] = {}


def resolve_xyne_channel(client: XyneWebClient, channel: str) -> str:
    if not channel:
        return ""
    name = channel.lstrip("#")
    if name in _channel_cache:
        return _channel_cache[name]
    try:
        data = client._post("conversations.list", {"limit": 1000})
    except Exception as e:
        log.warning(f"Xyne channel lookup failed for '{channel}': {e}")
        return channel if not channel.startswith("#") else ""
    for c in data.get("channels", []):
        if c.get("name") == name:
            _channel_cache[name] = c["id"]
            return c["id"]
        if c.get("id") == channel:
            return channel
    log.warning(f"Xyne channel '{channel}' not found")
    return ""


class MirroredClient:
    def __init__(self, primary, xyne_client: XyneWebClient, xyne_channel: str):
        self._primary = primary
        self._xyne = xyne_client
        self._xyne_channel = xyne_channel
        self._ts: dict[str, str] = {}

    def __getattr__(self, name):
        return getattr(self._primary, name)

    def xyne_ts(self, primary_ts):
        return self._ts.get(primary_ts) if primary_ts else None

    def chat_postMessage(self, **kwargs):
        resp = self._primary.chat_postMessage(**kwargs)
        try:
            thread = kwargs.get("thread_ts")
            xr = self._xyne.chat_postMessage(
                channel=self._xyne_channel,
                text=kwargs.get("text", ""),
                thread_ts=self._ts.get(thread) if thread else None,
                blocks=kwargs.get("blocks"),
                attachments=kwargs.get("attachments"),
            )
            if xr.get("ts"):
                self._ts[resp["ts"]] = xr["ts"]
        except Exception as e:
            log.warning(f"Xyne mirror post failed (non-fatal): {e}")
        return resp

    def chat_update(self, **kwargs):
        resp = self._primary.chat_update(**kwargs)
        try:
            xts = self._ts.get(kwargs.get("ts"))
            if xts:
                self._xyne.chat_update(
                    channel=self._xyne_channel, ts=xts,
                    text=kwargs.get("text", ""), blocks=kwargs.get("blocks"),
                )
        except Exception as e:
            log.warning(f"Xyne mirror update failed (non-fatal): {e}")
        return resp

    def post_investigation(self, primary_thread_ts, **kwargs):
        dest = XyneDestination({"base_url": self._xyne._base_url, "token": ""})
        dest._client = self._xyne
        return dest.post_investigation(
            channel=self._xyne_channel,
            thread_ts=self.xyne_ts(primary_thread_ts),
            **kwargs,
        )


def build_alert_mirror(config, primary_client):
    if not (config.xyne_base_url and config.xyne_bot_token):
        return primary_client
    import os
    xc = XyneWebClient(config.xyne_base_url, config.xyne_bot_token)
    channel = resolve_xyne_channel(
        xc, config.xyne_alert_channel or os.environ.get("SLACK_CHANNEL", ""))
    if not channel:
        log.warning("Xyne alert mirror disabled — no Xyne channel resolved")
        return primary_client
    return MirroredClient(primary_client, xc, channel)
