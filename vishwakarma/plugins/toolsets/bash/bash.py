"""
Bash toolset — run shell commands with allow/deny enforcement.

Rules are loaded from config and also from the global BashRules in VishwakarmaConfig.
The engine additionally enforces session-level approval for individual commands.

Config:
  safe_mode: false        # if true, only safe commands allowed
  allow: [kubectl, aws]   # extra allowed prefixes (in addition to safe list)
  block: [rm, wget]       # prefixes to always block

The engine layer handles require_approval / bash_always_allow / bash_always_deny.
This toolset handles the configured allow/block lists.
"""
import logging
import os
import queue as _queue
import re
import shlex
import subprocess
import threading
import time
from typing import Any

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120  # seconds


class PersistentShell:
    """Singleton persistent bash process — no fork/exec per command.

    Keeps one bash process alive. Commands are sent via stdin with a unique
    delimiter to detect when output is complete. ~100ms faster per command
    compared to subprocess.run() which forks a new process each time.
    """
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "PersistentShell":
        if cls._instance is None or not cls._instance.alive:
            with cls._lock:
                if cls._instance is None or not cls._instance.alive:
                    cls._instance = PersistentShell()
        return cls._instance

    def __init__(self):
        self._proc = subprocess.Popen(
            ["bash", "--norc", "--noprofile"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PS1": "", "TERM": "dumb"},
        )
        self._lock = threading.Lock()
        # Single persistent reader thread that funnels every stdout line
        # into self._line_q. run() drains this queue with a wall-clock
        # deadline so a stuck command can't hang the worker thread.
        self._line_q: _queue.Queue = _queue.Queue()
        self._reader_th = threading.Thread(
            target=self._reader_loop, daemon=True,
        )
        self._reader_th.start()
        log.debug("Persistent shell started (PID %d)", self._proc.pid)

    def _reader_loop(self):
        try:
            while True:
                line = self._proc.stdout.readline()
                self._line_q.put(line)
                if not line:  # EOF — shell died
                    break
        except Exception as ex:
            self._line_q.put(("__EXC__", ex))

    @property
    def alive(self) -> bool:
        return self._proc and self._proc.poll() is None

    def run(self, command: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
        """Run a command and return (exit_code, stdout, stderr).

        Falls back to subprocess.run() if persistent shell is dead.
        """
        if not self.alive:
            return self._fallback(command, timeout)

        # Use a unique delimiter to detect end of output
        delimiter = f"__VK_END_{id(command)}_{time.monotonic_ns()}__"

        # Construct command that captures exit code and outputs delimiter
        wrapped = (
            f"{{ {command} ; }} 2>/tmp/_vk_stderr\n"
            f"echo \"{delimiter}$?\"\n"
        )

        with self._lock:
            try:
                # Drain any stale lines from a previous timed-out call so we
                # don't read its leftover output as ours.
                while not self._line_q.empty():
                    try:
                        self._line_q.get_nowait()
                    except _queue.Empty:
                        break

                self._proc.stdin.write(wrapped)
                self._proc.stdin.flush()

                # Drain lines from the persistent reader thread until we hit
                # our delimiter, or the wall-clock deadline expires.
                stdout_lines: list[str] = []
                exit_code = 0
                deadline = time.monotonic() + timeout
                timed_out = False
                got_delimiter = False

                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        item = self._line_q.get(timeout=remaining)
                    except _queue.Empty:
                        timed_out = True
                        break
                    if isinstance(item, tuple) and item and item[0] == "__EXC__":
                        # Reader thread crashed — treat as shell death
                        break
                    line = item
                    if not line:  # EOF
                        break
                    if line.startswith(delimiter):
                        exit_code = int(line[len(delimiter):].strip() or "0")
                        got_delimiter = True
                        break
                    stdout_lines.append(line)

                if timed_out:
                    # Kill the stuck shell process so the next call gets a
                    # fresh one via PersistentShell.get(). Reader thread is
                    # daemon and will exit on its own.
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                    self._proc = None
                    return 124, "".join(stdout_lines), f"Command timed out after {timeout}s"

                if not got_delimiter:
                    # EOF without delimiter — shell died mid-command.
                    self._proc = None
                    return 1, "".join(stdout_lines), "Persistent shell died mid-command"

                stdout = "".join(stdout_lines)

                # Read stderr from temp file
                try:
                    with open("/tmp/_vk_stderr", "r") as f:
                        stderr = f.read()
                except Exception:
                    stderr = ""

                return exit_code, stdout, stderr

            except Exception as e:
                log.warning(f"Persistent shell error: {e}, falling back")
                return self._fallback(command, timeout)

    @staticmethod
    def _fallback(command: str, timeout: int) -> tuple[int, str, str]:
        """Fallback to subprocess.run() when persistent shell is unavailable."""
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"Command timed out after {timeout}s"
        except Exception as e:
            return 1, "", str(e)


@register_toolset
class BashToolset(Toolset):
    name = "bash"
    description = (
        "Run shell/bash commands. This is the PRIMARY tool for all infrastructure queries. "
        "Use kubectl for Kubernetes (pods, events, logs, deployments). "
        "Use aws CLI for AWS resources (RDS, CloudWatch, ElastiCache, ALB). "
        "Use stern for multi-pod log streaming. "
        "Supports: kubectl, aws, stern, jq, grep, awk, sort, timeout, head, tail."
    )

    def __init__(self, config: dict):
        self.safe_mode: bool = config.get("safe_mode", False)
        self.allow: list[str] = config.get("allow", [])
        self.block: list[str] = config.get("block", [])
        self.timeout: int = config.get("timeout", DEFAULT_TIMEOUT)

    def check_prerequisites(self) -> tuple[bool, str]:
        return True, ""  # bash is always available

    def get_tools(self) -> list[ToolDef]:
        desc = "Run a bash command."
        if self.safe_mode:
            desc += " [SAFE MODE: only pre-approved commands are allowed]"
        if self.block:
            desc += f" Blocked: {', '.join(self.block[:5])}."

        return [
            ToolDef(
                name="bash",
                description=desc,
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": (
                                "The bash command to run. "
                                "Prefer single, readable commands. "
                                "Avoid pipelines longer than 3 stages."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        if tool_name != "bash":
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")

        command = params.get("command", "").strip()
        if not command:
            # An empty command is usually a truncated/dropped tool call — treat as
            # a no-op (NO_DATA) so it doesn't burn a step as an "error".
            return ToolOutput(tool_name="bash", status=ToolStatus.NO_DATA,
                              output="(empty command — nothing to run)")

        # Check rules
        allowed, reason = self._is_allowed(command)
        if not allowed:
            return ToolOutput(
                status=ToolStatus.ERROR,
                error=reason,
                invocation=f"bash({command[:100]})",
            )

        log.debug(f"bash: {command}")
        try:
            shell = PersistentShell.get()
            exit_code, stdout, stderr = shell.run(command, timeout=self.timeout)

            if exit_code != 0:
                error_msg = f"Exit code {exit_code}"
                if stderr.strip():
                    error_msg += f"\n{stderr.strip()}"
                if stdout.strip():
                    error_msg += f"\nstdout:\n{stdout}"
                # Actionable guidance for common, recoverable shell failures.
                low = stderr.lower()
                if "command not found" in low:
                    missing = stderr.split(":")[1].strip() if ":" in stderr else "that tool"
                    error_msg += (f"\nHINT: '{missing}' isn't installed in this agent. Use an "
                                  "available tool instead (e.g. the database toolset for SQL, the "
                                  "prometheus/elasticsearch toolsets for metrics/logs, kubectl for k8s).")
                elif "no such host" in low or "could not resolve" in low or "connection refused" in low:
                    error_msg += ("\nHINT: that host isn't reachable from this agent (cross-cloud / "
                                  "VPC-internal). Use the endpoint from the Site Knowledge Base for THIS cluster's cloud.")
                elif "forbidden" in low or "cannot " in low and "kubectl" in command.lower():
                    error_msg += ("\nHINT: read-only RBAC — that verb/resource isn't permitted. Use get/"
                                  "describe/logs/top only.")
                return ToolOutput(
                    status=ToolStatus.ERROR,
                    error=error_msg,
                    invocation=f"bash({command})",
                )
            if not stdout.strip():
                return ToolOutput(
                    status=ToolStatus.NO_DATA,
                    invocation=f"bash({command})",
                )
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=stdout,
                invocation=f"bash({command})",
            )
        except Exception as e:
            return ToolOutput(
                status=ToolStatus.ERROR,
                error=str(e),
                invocation=f"bash({command})",
            )

    # SQL/DB destructive patterns — blocked regardless of allow/block config
    _DESTRUCTIVE_SQL_RE = re.compile(
        r"\b(DROP\s+TABLE|DROP\s+DATABASE|DELETE\s+FROM|TRUNCATE\s+TABLE|ALTER\s+TABLE"
        r"|FLUSHALL|FLUSHDB|SHUTDOWN\s+NOSAVE)\b",
        re.IGNORECASE,
    )

    # Infra-mutation guard — the agent is READ-ONLY. Even though kubectl/aws/
    # gcloud/helm/etc. are allowed (for get/describe/logs), a destructive
    # SUBCOMMAND must never run with prod credentials. Blocked regardless of
    # config; cannot be overridden. This catches both honest mistakes and
    # prompt-injection attempts to mutate infrastructure.
    _INFRA_MUTATION_RE = re.compile(
        r"\b("
        # kubectl write verbs
        r"kubectl\s+(?:[\w\-=./]+\s+)*?(delete|apply|create|edit|patch|replace|scale|"
        r"rollout\s+(?:restart|undo|pause)|drain|cordon|uncordon|taint|evict|set|"
        r"annotate|label|exec|cp|attach|port-forward)\b"
        # aws mutating verbs (delete-/create-/put-/update-/modify-/terminate-/stop-/start-/
        # reboot-/run-/remove-/deregister-/detach-/attach-/disable-/enable-/revoke-/authorize-/
        # associate-/disassociate-/reset-/restore-/cancel-/release-)
        r"|aws\s+s3\s+(?:rm|cp|mv|sync|rb|mb)\b"
        r"|aws\s+[\w\-]+\s+(?:delete|create|put|update|modify|terminate|stop|start|reboot|"
        r"run|remove|deregister|register|detach|attach|disable|enable|revoke|authorize|"
        r"associate|disassociate|reset|restore|cancel|release|set|add|deploy|promote|"
        r"reboot|purge|empty|abort)[\w\-]*"
        # gcloud mutating verbs
        r"|gcloud\s+(?:[\w\-]+\s+)*?(delete|create|update|patch|remove|set|add|disable|"
        r"enable|reset|restart|stop|start|scale|deploy|promote|rollback|resize|drain|"
        r"detach|attach|clear|purge|undelete|import|restore)\b"
        # helm / gsutil / argocd / flux / terraform / pulumi / docker push etc.
        r"|helm\s+(install|upgrade|uninstall|rollback|delete)"
        r"|gsutil\s+(rm|cp|mv|rsync|setmeta|rewrite)"
        r"|argocd\s+app\s+(create|delete|set|sync|rollback|patch)"
        r"|flux\s+(create|delete|reconcile|suspend|resume)"
        r"|terraform\s+(apply|destroy|import|taint|state\s+(rm|mv))"
        r"|pulumi\s+(up|destroy|import)"
        r"|docker\s+(push|rmi|rm|kill|stop)"
        r")",
        re.IGNORECASE,
    )

    def _is_allowed(self, command: str) -> tuple[bool, str]:
        """
        Apply local bash rules (safe_mode, allow, block).
        Note: engine-level bash_always_allow/deny takes priority — this is a secondary check.
        """
        from vishwakarma.config import HARDCODED_BLOCK, SAFE_BASH_COMMANDS
        cmd = command.strip()

        # Hardcoded dangerous patterns
        for pattern in HARDCODED_BLOCK:
            if pattern in cmd:
                return False, f"Blocked by hardcoded safety rule: {pattern}"

        # Block destructive SQL/DB commands embedded in bash (psql -c "DROP TABLE", redis-cli FLUSHALL, etc.)
        if self._DESTRUCTIVE_SQL_RE.search(cmd):
            return False, "Blocked: destructive database operation detected (DROP/DELETE/TRUNCATE/FLUSH)"

        # Block infra-mutating CLI subcommands (kubectl/aws/gcloud/helm/...) — the
        # agent is read-only. Non-overridable.
        m = self._INFRA_MUTATION_RE.search(cmd)
        if m:
            return False, (f"Blocked: infrastructure-mutating command — the agent is "
                           f"READ-ONLY (matched '{m.group(0)[:60]}'). Use get/describe/"
                           f"logs/list only.")

        # Config block list — split on ALL chaining operators: |, ;, &&, ||
        parts = [p.strip() for p in re.split(r'[|;]|&&|\|\|', cmd)]
        for blocked in self.block:
            for part in parts:
                if part.startswith(blocked):
                    return False, f"Command blocked by config rule: {blocked}"

        # Also check subshell/process-substitution content: $(...), `...`, <(...)
        # Use a broad extraction that handles nesting by finding all parenthesized content
        subshell_content = re.findall(r'\$\((.+?)\)|`([^`]+)`|<\((.+?)\)', cmd)
        for groups in subshell_content:
            for sub in groups:
                if sub:
                    # Check HARDCODED_BLOCK against subshell content too
                    for pattern in HARDCODED_BLOCK:
                        if pattern in sub:
                            return False, f"Blocked by hardcoded safety rule (in subshell): {pattern}"
                    if self._INFRA_MUTATION_RE.search(sub):
                        return False, "Blocked: infrastructure-mutating command in subshell (agent is read-only)"
                    for blocked in self.block:
                        sub_parts = [p.strip() for p in re.split(r'[|;]|&&|\|\|', sub)]
                        for part in sub_parts:
                            if part.startswith(blocked):
                                return False, f"Command blocked by config rule (in subshell): {blocked}"

        # Config allow list (takes priority over safe_mode)
        for allowed in self.allow:
            if cmd.startswith(allowed):
                return True, ""

        # Safe mode check
        if self.safe_mode:
            first_cmd = cmd.split()[0] if cmd.split() else ""
            if any(first_cmd == safe or first_cmd.endswith(f"/{safe}") for safe in SAFE_BASH_COMMANDS):
                return True, ""
            return False, (
                f"safe_mode is on — '{first_cmd}' not in allowed command list. "
                f"Add '{first_cmd}' to bash.config.allow in config.yaml to permit it."
            )

        return True, ""
