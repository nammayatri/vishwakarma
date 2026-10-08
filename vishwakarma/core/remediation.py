import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from vishwakarma.storage import remediation as store

log = logging.getLogger(__name__)

APPROVE_ACTION_ID = "vk_remediate_approve"
REJECT_ACTION_ID = "vk_remediate_reject"

_DNS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_NS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_COMMAND = re.compile(r"^kubectl delete pod (\S+) -n (\S+)$")
_YES = re.compile(r"^\s*(yes|y|approve|approved|ok|go ahead|do it)\W*$", re.I)
_NO = re.compile(r"^\s*(no|n|reject|rejected|cancel|stop)\W*$", re.I)


DRAINER_KEYS = {
    "driver": ("DRIVER_DRAINER_STOP", "DRIVER_FORCE_DRAIN"),
    "rider": ("RIDER_DRAINER_STOP", "FORCE_DRAIN"),
}
DRAINER_NAMESPACE = "drainer-redis"
_REDIS_ERRORS = ("MOVED", "ASK", "ERR", "(ERROR)", "CLUSTERDOWN", "NOAUTH", "WRONGPASS", "LOADING", "READONLY")


@dataclass
class RemediationSettings:
    enabled: bool = True
    allowed_namespaces: list[str] = field(default_factory=lambda: ["atlas"])
    max_pods: int = 3
    min_requests: float = 50.0
    outlier_factor: float = 10.0
    ttl_seconds: int = 900
    approver_emails: list[str] = field(default_factory=list)
    pod_label: str = "pod"
    kubectl_bin: str = "kubectl"
    redis_cli_bin: str = "redis-cli"
    drainer_redis_host: str = ""
    drainer_redis_port: int = 6379
    drainer_redis_cluster: bool = False
    on_proposal: Callable[[dict], None] | None = None


def settings_from_config(config) -> RemediationSettings:
    return RemediationSettings(
        enabled=config.remediation_enabled,
        allowed_namespaces=config.remediation_allowed_namespaces,
        max_pods=config.remediation_max_pods,
        min_requests=config.remediation_min_requests,
        outlier_factor=config.remediation_outlier_factor,
        ttl_seconds=config.remediation_ttl_seconds,
        approver_emails=config.remediation_approver_emails,
        pod_label=config.remediation_pod_label,
        kubectl_bin=config.remediation_kubectl_bin,
        redis_cli_bin=config.remediation_redis_cli_bin,
        drainer_redis_host=config.remediation_drainer_redis_host,
        drainer_redis_port=config.remediation_drainer_redis_port,
        drainer_redis_cluster=config.remediation_drainer_redis_cluster,
    )


def build_command(namespace: str, pod: str) -> str:
    return f"kubectl delete pod {pod} -n {namespace}"


def parse_command(command: str) -> tuple[str, str] | None:
    m = _COMMAND.match(command or "")
    if not m:
        return None
    pod, ns = m.group(1), m.group(2)
    if not (_DNS_NAME.match(pod) and _NS_NAME.match(ns)):
        return None
    return ns, pod


def build_drainer_command(side: str) -> str:
    stop_key, force_key = DRAINER_KEYS[side]
    return f"redis-cli SET {force_key} true\nredis-cli DEL {stop_key}"


def parse_drainer_command(command: str) -> str | None:
    for side in DRAINER_KEYS:
        if command == build_drainer_command(side):
            return side
    return None


def kind_of(action: dict) -> str:
    return "drainer_resume" if (action.get("command") or "").startswith("redis-cli ") else "pod_delete"


def is_yes(text: str) -> bool:
    return bool(_YES.match(text or ""))


def is_no(text: str) -> bool:
    return bool(_NO.match(text or ""))


def propose(settings: RemediationSettings, *, incident_id: str, alert_title: str, platform: str,
            channel: str, thread_ts: str, namespace: str, service: str, pod: str,
            evidence: str, queries_ran: list[str]) -> dict | None:
    if not settings.enabled:
        return None
    if namespace not in settings.allowed_namespaces:
        log.info(f"Remediation skipped: namespace {namespace!r} not in allowlist")
        return None
    if not (_NS_NAME.match(namespace) and _DNS_NAME.match(pod)):
        log.warning(f"Remediation skipped: unsafe names ns={namespace!r} pod={pod!r}")
        return None
    if not service or not pod.startswith(service):
        log.warning(f"Remediation skipped: pod {pod!r} does not belong to service {service!r}")
        return None
    action_id = store.create_action(
        incident_id=incident_id, alert_title=alert_title, platform=platform, channel=channel,
        thread_ts=thread_ts, namespace=namespace, service=service, pod=pod,
        command=build_command(namespace, pod), queries_ran=queries_ran, evidence=evidence,
    )
    return store.get_action(action_id)


def propose_drainer(settings: RemediationSettings, *, incident_id: str, alert_title: str, platform: str,
                    channel: str, thread_ts: str, side: str, evidence: str,
                    queries_ran: list[str]) -> dict | None:
    if not settings.enabled or side not in DRAINER_KEYS:
        return None
    if not settings.drainer_redis_host:
        log.warning("Drainer remediation skipped: remediation.drainer_redis.host is not configured")
        return None
    action_id = store.create_action(
        incident_id=incident_id, alert_title=alert_title, platform=platform, channel=channel,
        thread_ts=thread_ts, namespace=DRAINER_NAMESPACE, service=f"{side}-drainer", pod=side,
        command=build_drainer_command(side), queries_ran=queries_ran, evidence=evidence,
    )
    return store.get_action(action_id)


def _command_block(action: dict) -> str:
    if kind_of(action) == "drainer_resume":
        return f"```{action['command']}```"
    return f"`{action['command']}`"


def proposal_text(action: dict) -> str:
    if kind_of(action) == "drainer_resume":
        title = f":wrench: *Suggested fix — resume the {action['pod']} drainer*"
        how = "Approve to run exactly these commands, in this order"
    else:
        title = ":wrench: *Suggested fix — restart the 0DC pod*"
        how = "Approve to run exactly this command"
    return (
        f"{title}\n{action['evidence']}\n{_command_block(action)}\n"
        f"_{how} (on Xyne, reply `yes` in the thread). Only authorized approvers can approve. Expires in 15 min._"
    )


def proposal_blocks(action: dict, notice: str = "") -> list[dict]:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": proposal_text(action)}}]
    if notice:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": notice}]})
    blocks.append({
        "type": "actions",
        "block_id": f"remediate_{action['id']}",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve & run", "emoji": True},
             "style": "primary", "action_id": APPROVE_ACTION_ID, "value": action["id"]},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
             "style": "danger", "action_id": REJECT_ACTION_ID, "value": action["id"]},
        ],
    })
    return blocks


def denied_notice(approver: str, email: str) -> str:
    who = f"{approver} ({email})" if email else approver
    return f":no_entry: {who} — you don't have access to approve this. Nothing was run; an authorized approver can still use the buttons."


def result_text(action: dict, status: str, approver: str, output: str = "") -> str:
    cmd = _command_block(action)
    if status == "executed":
        return f":white_check_mark: *Executed* by {approver}: {cmd}\n{output[:600]}"
    if status == "failed":
        return f":x: *Approved by {approver} but failed*: {cmd}\n{output[:600]}"
    if status == "rejected":
        return f":no_entry_sign: *Rejected* by {approver}: {cmd} — nothing was run."
    if status == "expired":
        return f":hourglass: *Expired* — no approval within the window, nothing was run: {cmd}"
    return f":information_source: Already handled ({status}): {cmd}"


def _run(argv: list[str], timeout: int = 12) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def _run_read(argv: list[str]) -> tuple[int, str]:
    return _run(argv, timeout=10)


def _authorized(settings: RemediationSettings, email: str) -> bool:
    allowed = {e.strip().lower() for e in settings.approver_emails if e.strip()}
    return bool(email) and email.strip().lower() in allowed


def _redis_argv(settings: RemediationSettings, *args: str) -> list[str]:
    argv = [settings.redis_cli_bin, "-h", settings.drainer_redis_host, "-p", str(settings.drainer_redis_port)]
    if settings.drainer_redis_cluster:
        argv.append("-c")
    return argv + ["--no-auth-warning", *args]


def _redis_call(settings: RemediationSettings, runner, *args: str) -> tuple[bool, str]:
    code, out = runner(_redis_argv(settings, *args))
    lines = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith("-> ")]
    value = lines[-1] if lines else ""
    if code != 0 or value.upper().startswith(_REDIS_ERRORS):
        hint = " — Redis is a cluster: set VK_REMEDIATION_DRAINER_REDIS_CLUSTER=true" \
            if value.upper().startswith(("MOVED", "ASK")) else ""
        return False, f"{value or out}{hint}"
    return True, value


def read_drainer_stop_key(settings: RemediationSettings, side: str, runner=_run_read) -> tuple[str, str]:
    stop_key = DRAINER_KEYS[side][0]
    if not settings.drainer_redis_host:
        return "error", "remediation.drainer_redis.host is not configured"
    ok, val = _redis_call(settings, runner, "GET", stop_key)
    if not ok:
        return "error", val
    return ("set", val) if val.lower() == "true" else ("absent", val)


def _execute_pod_delete(settings: RemediationSettings, action: dict, runner) -> tuple[bool, str]:
    parsed = parse_command(action["command"])
    if parsed is None or parsed[0] not in settings.allowed_namespaces:
        return False, "command failed validation at execution time"
    ns, pod = parsed
    code, out = runner([settings.kubectl_bin, "get", "pod", pod, "-n", ns, "-o", "name"])
    if code != 0:
        return False, f"pod check failed (already gone?): {out}"
    code, out = runner([settings.kubectl_bin, "delete", "pod", pod, "-n", ns, "--wait=false"])
    if code != 0:
        return False, out
    gcode, gout = runner([settings.kubectl_bin, "get", "pod", pod, "-n", ns,
                          "-o", "jsonpath={.metadata.deletionTimestamp}"])
    if gcode != 0 and "NotFound" in gout:
        state = "pod is gone"
    elif gcode == 0 and gout.strip():
        state = f"pod is terminating (deletionTimestamp {gout.strip()})"
    else:
        return False, f"delete was issued but the pod state could not be confirmed: {gout or out}"
    return True, f"{out.strip()} — {state}"


def _execute_drainer_resume(settings: RemediationSettings, action: dict, runner) -> tuple[bool, str]:
    side = parse_drainer_command(action["command"])
    if side is None:
        return False, "command failed validation at execution time"
    if not settings.drainer_redis_host:
        return False, "drainer redis host is not configured"
    stop_key, force_key = DRAINER_KEYS[side]

    def redis(*args: str) -> tuple[bool, str]:
        return _redis_call(settings, runner, *args)

    ok, stop_val = redis("GET", stop_key)
    if not ok:
        return False, f"could not read {stop_key}: {stop_val}"
    if stop_val.lower() != "true":
        return True, f"no-op: {stop_key} is not set (drainer already resumed), nothing changed"
    ok, res = redis("SET", force_key, "true")
    if not ok or res != "OK":
        return False, f"SET {force_key} true failed: {res}"
    ok, res = redis("DEL", stop_key)
    if not ok or not res.isdigit():
        return False, f"{force_key} was set but DEL {stop_key} failed: {res}"
    ok_stop, stop_now = redis("GET", stop_key)
    ok_force, force_now = redis("GET", force_key)
    verified = ok_stop and ok_force and stop_now == "" and force_now.lower() == "true"
    if verified:
        return True, f"{stop_key} deleted, {force_key}=true (verified)"
    return False, f"verify mismatch: {stop_key}={stop_now or '(nil)'}, {force_key}={force_now or '(nil)'}"


def decide(settings: RemediationSettings, action_id: str, *, approved: bool, approver: str,
           approver_email: str = "", via: str,
           runner: Callable[[list[str]], tuple[int, str]] = _run) -> dict:
    action = store.get_action(action_id)
    if action is None:
        return {"status": "missing", "text": ":information_source: Unknown remediation request.", "action": None}
    if not settings.enabled:
        return {"status": "disabled", "text": ":lock: Remediation is disabled.", "action": action}
    if not _authorized(settings, approver_email):
        try:
            from vishwakarma.storage.audit import audit
            audit(approver, "remediation.denied", action_id, {"email": approver_email, "via": via})
        except Exception:
            pass
        return {"status": "unauthorized", "text": denied_notice(approver, approver_email), "action": action}

    status = store.claim_decision(action_id, approved=approved, approver=f"{approver} <{approver_email}>",
                                  via=via, ttl_seconds=settings.ttl_seconds)
    action = store.get_action(action_id)
    if status.startswith("already:"):
        prior = status.split(":", 1)[1]
        who = f" by {action['approved_by']}" if action.get("approved_by") else ""
        return {"status": status, "action": action,
                "text": f":information_source: Already {prior}{who} — nothing more was run: {_command_block(action)}"}
    if status != "approved":
        return {"status": status, "text": result_text(action, status, approver), "action": action}

    if kind_of(action) == "drainer_resume":
        ok, out = _execute_drainer_resume(settings, action, runner)
    else:
        ok, out = _execute_pod_delete(settings, action, runner)
    store.record_result(action_id, ok=ok, output=out)
    try:
        from vishwakarma.storage.audit import audit
        audit(approver, "remediation.execute" if ok else "remediation.failed", action_id,
              {"command": action["command"], "email": approver_email, "via": via, "output": out[:500]})
    except Exception:
        pass
    action = store.get_action(action_id)
    final = "executed" if ok else "failed"
    return {"status": final, "text": result_text(action, final, approver, out), "action": action}


def outcome_blocks(text: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _xyne_target(action: dict | None) -> tuple[str, str] | None:
    if not action:
        return None
    if action.get("mirror_channel") and action.get("mirror_message_ts"):
        return action["mirror_channel"], action["mirror_message_ts"]
    if action.get("platform") == "xyne" and action.get("channel") and action.get("message_ts"):
        return action["channel"], action["message_ts"]
    return None


def mirror_outcome_to_xyne(config, action: dict | None, text: str) -> None:
    target = _xyne_target(action)
    if not target or not (config.xyne_base_url and config.xyne_bot_token):
        return
    try:
        from vishwakarma.plugins.relays.xyne.plugin import XyneWebClient, text_flow
        XyneWebClient(config.xyne_base_url, config.xyne_bot_token).chat_update(
            channel=target[0], ts=target[1], text=text, flow=text_flow(text))
    except Exception as e:
        log.warning(f"Xyne remediation outcome update failed (non-fatal): {e}")


FINAL_STATUSES = ("executed", "failed", "rejected", "expired")


def update_primary_slack(config, action: dict | None, text: str) -> None:
    if not (action and action.get("platform") == "slack" and action.get("channel") and action.get("message_ts")):
        return
    if not config.slack_bot_token:
        return
    try:
        from slack_sdk import WebClient
        WebClient(token=config.slack_bot_token).chat_update(
            channel=action["channel"], ts=action["message_ts"], text=text, blocks=outcome_blocks(text))
    except Exception as e:
        log.warning(f"Slack remediation outcome update failed (non-fatal): {e}")


def mirror_notice_to_xyne(config, action: dict | None, notice: str) -> None:
    target = _xyne_target(action)
    if not target or not (config.xyne_base_url and config.xyne_bot_token):
        return
    try:
        from vishwakarma.plugins.relays.xyne.plugin import XyneWebClient
        XyneWebClient(config.xyne_base_url, config.xyne_bot_token).chat_update(
            channel=target[0], ts=target[1],
            text=f"{proposal_text(action)}\n{notice}", blocks=proposal_blocks(action, notice),
        )
    except Exception as e:
        log.warning(f"Xyne remediation notice update failed (non-fatal): {e}")


def update_primary_slack_notice(config, action: dict | None, notice: str) -> None:
    if not (action and action.get("platform") == "slack" and action.get("channel") and action.get("message_ts")):
        return
    if not config.slack_bot_token:
        return
    try:
        from slack_sdk import WebClient
        WebClient(token=config.slack_bot_token).chat_update(
            channel=action["channel"], ts=action["message_ts"],
            text=f"{proposal_text(action)}\n{notice}", blocks=proposal_blocks(action, notice))
    except Exception as e:
        log.warning(f"Slack remediation notice update failed (non-fatal): {e}")


def find_pending_for_xyne(channel: str, ttl_seconds: int) -> dict | None:
    return store.latest_pending(channel, ttl_seconds) or store.only_pending(ttl_seconds)


def _plain(text: str) -> str:
    from vishwakarma.plugins.relays.xyne.plugin import _emojify
    return _emojify(text).replace("`", "").replace("*", "")


def flow_response(result: dict) -> dict:
    status, text = result["status"], _plain(result["text"])
    if status in ("executed", "rejected"):
        return {"type": "ack", "message": text}
    return {"type": "error", "message": text, "error": text}


def handle_flow_action(config, payload: dict, user_lookup, runner=_run) -> dict | None:
    verb, _, remediation_id = str(payload.get("actionId") or "").partition(":")
    if verb not in (APPROVE_ACTION_ID, REJECT_ACTION_ID) or not remediation_id:
        return None
    user_id = str((payload.get("context") or {}).get("userId") or "")
    name, email = user_lookup(user_id)
    result = decide(settings_from_config(config), remediation_id, approved=verb == APPROVE_ACTION_ID,
                    approver=name or user_id or "unknown", approver_email=email, via="xyne", runner=runner)
    action = result["action"]
    if result["status"] == "unauthorized":
        mirror_notice_to_xyne(config, action, result["text"])
        update_primary_slack_notice(config, action, result["text"])
    elif result["status"] in FINAL_STATUSES:
        mirror_outcome_to_xyne(config, action, result["text"])
        update_primary_slack(config, action, result["text"])
    log.info(f"[REMEDIATE] {remediation_id} -> {result['status']} by {name or user_id} <{email or 'no email'}> via xyne click")
    return flow_response(result)


def extract_click(payload: dict) -> tuple[str, str, str, str, str] | None:
    actions = payload.get("actions")
    if isinstance(actions, list) and actions and isinstance(actions[0], dict):
        a = actions[0]
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
        uid = user.get("id") or payload.get("userId") or ""
        uname = user.get("name") or user.get("username") or payload.get("senderName") or uid
        email = (user.get("email") or profile.get("email") or payload.get("email")
                 or payload.get("userEmail") or payload.get("senderEmail") or "")
        return a.get("action_id", ""), a.get("value", ""), uid, uname, email
    inner = payload.get("payload")
    if isinstance(inner, dict):
        return extract_click(inner)
    return None
