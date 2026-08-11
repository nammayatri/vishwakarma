"""
Xyne — same mention-triggered investigation flow as Argus (bot/argus.py), on
Xyne (spaces.xyne.juspay.net) instead of Slack.

Reuses ArgusBot as-is (it's deliberately platform-agnostic — dispatch/classify/
say/fetch_thread are all injected) and make_dispatcher as-is (issue.labels are
what carry platform identity downstream, not this module). Only the transport
is new: posting via XyneWebClient instead of the Slack SDK, and receiving
events via a webhook route (server.py: POST /api/xyne/events) instead of a
socket-mode connection, since Xyne is only confirmed to expose REST endpoints.

Confirmed live against the real API (2026-08-11): chat.postMessage, auth.test
work; conversations.replies/.history/.list exist but the app's current token
lacks the `channels:read` scope (granted: files:write, chat:write, im:write)
— thread-fetching (fetch_thread below) will fail with "missing_permission"
until that scope is granted on Xyne's side. The inbound event shape (assumed
Slack-Events-API-like) is still unverified — no real webhook received yet.

Config (config.yaml):
  xyne:
    base_url: https://spaces.xyne.juspay.net/api/apps/slack
    bot_token: ...            # Bearer JWT for posting/fetching (confirmed working)
    bot_user_id: ...          # optional override — auto-discovered via auth.test if unset
    mre_group_id: ...         # optional, mirrors argus.mre_group_id
    signing_secret: ...       # verifies inbound webhook requests (Slack v0 scheme, assumed)
    webhook_token: ...        # fallback shared-secret auth if signing_secret is unset
Env: XYNE_BASE_URL / XYNE_BOT_TOKEN / XYNE_BOT_USER_ID / XYNE_MRE_GROUP_ID /
     XYNE_SIGNING_SECRET / XYNE_WEBHOOK_TOKEN
"""
import logging

from vishwakarma.bot.argus import ArgusBot, make_classifier, make_dispatcher
from vishwakarma.plugins.relays.xyne.plugin import XyneApiError, XyneWebClient

log = logging.getLogger(__name__)


def _fetch_xyne_thread_msgs(client: XyneWebClient, channel: str, thread_ts: str,
                             bot_user_id: str) -> list[str]:
    """Mirrors bot/argus.py's _fetch_thread_msgs. conversations.replies exists
    on Xyne but currently 403s with missing_permission (channels:read not
    granted to this app) — fails soft (empty list), same as any other
    thread-fetch failure, until that scope is granted."""
    try:
        data = client._post("conversations.replies", {"channel": channel, "ts": thread_ts, "limit": 100})
    except (XyneApiError, Exception) as e:
        log.debug(f"Xyne thread fetch failed (non-fatal — likely missing channels:read scope): {e}")
        return []
    out: list[str] = []
    for m in data.get("messages", []):
        txt = (m.get("text") or "").strip()
        if not txt:
            continue
        who = m.get("user") or m.get("username") or m.get("bot_id") or "?"
        who = "Argus" if who == bot_user_id else who
        out.append(f"<{who}>: {txt}")
    return out


def start_xyne(config) -> ArgusBot | None:
    """
    Build (but don't start a socket for) the Xyne-flavored ArgusBot. Returns
    the bot so server.py's /api/xyne/events route can feed it events directly.
    Mirrors bot/argus.py:start_argus's wiring, minus the Socket Mode handler —
    Xyne only exposes REST endpoints, so events arrive via a webhook route we
    expose instead of an outbound socket connection.
    """
    base_url = getattr(config, "xyne_base_url", "")
    bot_token = getattr(config, "xyne_bot_token", "")
    if not (base_url and bot_token):
        log.info("Xyne not configured (xyne.base_url/bot_token) — skipping")
        return None

    client = XyneWebClient(base_url, bot_token)

    bot_user_id = getattr(config, "xyne_bot_user_id", "") or ""
    if not bot_user_id:
        try:
            bot_user_id = client.auth_test()["user_id"]
        except Exception as e:
            log.warning(f"Xyne auth.test failed — cannot self-discover bot_user_id, "
                        f"and none was configured. Xyne mentions will never match: {e}")
            return None

    llm = config.make_llm()

    def say(channel: str, thread_ts: str, text: str) -> None:
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        except Exception as e:
            log.warning(f"Xyne say() failed (non-fatal): {e}")

    xyne = ArgusBot(
        mre_group_id=getattr(config, "xyne_mre_group_id", ""),
        bot_user_id=bot_user_id,
        dispatch=make_dispatcher(config),
        classify=make_classifier(llm),
        say=say,
        fetch_thread=lambda ch, tts: _fetch_xyne_thread_msgs(client, ch, tts, bot_user_id),
        platform="xyne",
    )
    log.info(f"⚡ Xyne bot ready (user={bot_user_id}, "
             f"mre_group={getattr(config, 'xyne_mre_group_id', '') or 'unset'})")
    return xyne
