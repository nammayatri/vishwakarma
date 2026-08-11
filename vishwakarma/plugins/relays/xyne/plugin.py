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
  - conversations.replies/.history/.list: endpoints EXIST but the app's
    current token lacks the `channels:read` scope (granted:
    ["files:write", "chat:write", "im:write"]) — thread-fetching will fail
    with {"ok": false, "error": "missing_permission"} until that scope is
    granted on Xyne's side.
"""
import logging

import requests

from vishwakarma.plugins.relays.slack.plugin import SlackDestination

log = logging.getLogger(__name__)


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
                          **_ignored) -> dict:
        body: dict = {"channel": channel, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        if blocks:
            body["blocks"] = blocks
        if attachments:
            body["attachments"] = attachments
        data = self._post("chat.postMessage", body)
        return {"ok": True, "ts": data.get("ts", ""), "channel": data.get("channel") or channel}

    def chat_update(self, channel, ts, text: str = "", blocks: list | None = None,
                     **_ignored) -> dict:
        body: dict = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            body["blocks"] = blocks
        data = self._post("chat.update", body)
        return {"ok": True, "ts": data.get("ts", ts), "channel": data.get("channel") or channel}

    def auth_test(self) -> dict:
        """Confirmed live — returns {"ok", "user_id", "bot_id", "user", "team", ...},
        same shape as Slack's auth.test. Used to self-discover our bot's own
        user_id (the <@ID> tag to match for mentions) instead of requiring it
        as separate config."""
        return self._post("auth.test", {})


def verify_xyne_signature(signing_secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """
    Slack-compatible request signature verification (the 'v0' scheme: HMAC-SHA256
    over "v0:{timestamp}:{body}", compared against an "X-Slack-Signature"-style
    header). Xyne provided a signing secret matching Slack's own request-signing
    model, so this assumes the same scheme — UNVERIFIED against a real inbound
    Xyne webhook (none received yet); adjust here if Xyne's header names or
    algorithm differ once a real event is observed.
    """
    import hashlib
    import hmac
    import time

    if not (signing_secret and timestamp and signature):
        return False
    try:
        if abs(time.time() - float(timestamp)) > 60 * 5:
            return False  # stale request — replay protection, matches Slack's own rule
    except ValueError:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    computed = "v0=" + hmac.new(signing_secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


class XyneDestination(SlackDestination):
    """
    Reuses SlackDestination's entire post_investigation flow (thread-vs-new-message
    decision, text chunking, feedback buttons) — only the transport differs:
    XyneWebClient instead of the real Slack SDK, and PDF upload is skipped
    (no known Xyne equivalent to files_upload_v2) — always falls back to
    posting the RCA as chunked text.

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

    def _upload_pdf(self, client, channel, thread_ts, pdf_path, title, initial_comment) -> bool:
        return False
