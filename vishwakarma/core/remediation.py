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


@dataclass
class RemediationSettings:
    enabled: bool = True
    allowed_namespaces: list[str] = field(default_factory=lambda: ["atlas"])
    max_pods: int = 3
    min_requests: float = 5.0
    ttl_seconds: int = 900
    approvers: list[str] = field(default_factory=list)
    pod_label: str = "pod"
    kubectl_bin: str = "kubectl"
    on_proposal: Callable[[dict], None] | None = None


def settings_from_config(config) -> RemediationSettings:
    return RemediationSettings(
        enabled=config.remediation_enabled,
        allowed_namespaces=config.remediation_allowed_namespaces,
        max_pods=config.remediation_max_pods,
        min_requests=config.remediation_min_requests,
        ttl_seconds=config.remediation_ttl_seconds,
        approvers=config.remediation_approvers,
        pod_label=config.remediation_pod_label,
        kubectl_bin=config.remediation_kubectl_bin,
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


def proposal_text(action: dict) -> str:
    return (
        f":wrench: *Suggested fix — restart the 0DC pod*\n{action['evidence']}\n"
        f"Command: `{action['command']}`\n"
        f"_Approve to run exactly this command (on Xyne, reply `yes` in the thread). Expires in 15 min._"
    )


def proposal_blocks(action: dict) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": proposal_text(action)}},
        {
            "type": "actions",
            "block_id": f"remediate_{action['id']}",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve & run", "emoji": True},
                 "style": "primary", "action_id": APPROVE_ACTION_ID, "value": action["id"]},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                 "style": "danger", "action_id": REJECT_ACTION_ID, "value": action["id"]},
            ],
        },
    ]


def result_text(action: dict, status: str, approver: str, output: str = "") -> str:
    cmd = f"`{action['command']}`"
    if status == "executed":
        return f":white_check_mark: *Executed* by {approver}: {cmd}\n{output[:400]}"
    if status == "failed":
        return f":x: *Approved by {approver} but failed*: {cmd}\n{output[:400]}"
    if status == "rejected":
        return f":no_entry_sign: *Rejected* by {approver}: {cmd} — nothing was run."
    if status == "expired":
        return f":hourglass: *Expired* — no approval within the window, nothing was run: {cmd}"
    if status == "unauthorized":
        return f":lock: {approver} is not in the approver list — nothing was run."
    return f":information_source: Already handled ({status}): {cmd}"


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=45, shell=False)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def _authorized(settings: RemediationSettings, approver_ids: list[str]) -> bool:
    if not settings.approvers:
        return True
    allowed = {a.lower() for a in settings.approvers}
    return any((i or "").lower() in allowed for i in approver_ids)


def decide(settings: RemediationSettings, action_id: str, *, approved: bool, approver: str,
           approver_ids: list[str] | None = None, via: str,
           runner: Callable[[list[str]], tuple[int, str]] = _run) -> dict:
    action = store.get_action(action_id)
    if action is None:
        return {"status": "missing", "text": ":information_source: Unknown remediation request.", "action": None}
    if not settings.enabled:
        return {"status": "disabled", "text": ":lock: Remediation is disabled.", "action": action}
    if not _authorized(settings, (approver_ids or []) + [approver]):
        return {"status": "unauthorized", "text": result_text(action, "unauthorized", approver), "action": action}

    status = store.claim_decision(action_id, approved=approved, approver=approver, via=via,
                                  ttl_seconds=settings.ttl_seconds)
    action = store.get_action(action_id)
    if status != "approved":
        return {"status": status, "text": result_text(action, status, approver), "action": action}

    parsed = parse_command(action["command"])
    if parsed is None or parsed[0] not in settings.allowed_namespaces:
        store.record_result(action_id, ok=False, output="command failed validation at execution time")
        action = store.get_action(action_id)
        return {"status": "failed", "text": result_text(action, "failed", approver, action["output"]), "action": action}
    ns, pod = parsed

    code, out = runner([settings.kubectl_bin, "get", "pod", pod, "-n", ns, "-o", "name"])
    if code != 0:
        store.record_result(action_id, ok=False, output=f"pod check failed (already gone?): {out}")
        action = store.get_action(action_id)
        return {"status": "failed", "text": result_text(action, "failed", approver, action["output"]), "action": action}

    code, out = runner([settings.kubectl_bin, "delete", "pod", pod, "-n", ns])
    ok = code == 0
    store.record_result(action_id, ok=ok, output=out)
    try:
        from vishwakarma.storage.audit import audit
        audit(approver, "remediation.execute" if ok else "remediation.failed", action_id,
              {"command": action["command"], "via": via, "output": out[:500]})
    except Exception:
        pass
    action = store.get_action(action_id)
    final = "executed" if ok else "failed"
    return {"status": final, "text": result_text(action, final, approver, out), "action": action}


def outcome_blocks(text: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def mirror_outcome_to_xyne(config, action: dict | None, text: str) -> None:
    if not (action and action.get("mirror_channel") and action.get("mirror_message_ts")):
        return
    if not (config.xyne_base_url and config.xyne_bot_token):
        return
    try:
        from vishwakarma.plugins.relays.xyne.plugin import XyneWebClient
        XyneWebClient(config.xyne_base_url, config.xyne_bot_token).chat_update(
            channel=action["mirror_channel"], ts=action["mirror_message_ts"],
            text=text, blocks=outcome_blocks(text),
        )
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


def find_pending_for_xyne(channel: str, ttl_seconds: int) -> dict | None:
    return store.latest_pending(channel, ttl_seconds) or store.only_pending(ttl_seconds)


def extract_click(payload: dict) -> tuple[str, str, str, str] | None:
    actions = payload.get("actions")
    if isinstance(actions, list) and actions and isinstance(actions[0], dict):
        a = actions[0]
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        uid = user.get("id") or payload.get("userId") or ""
        uname = user.get("name") or user.get("username") or payload.get("senderName") or uid
        return a.get("action_id", ""), a.get("value", ""), uid, uname
    inner = payload.get("payload")
    if isinstance(inner, dict):
        return extract_click(inner)
    return None
