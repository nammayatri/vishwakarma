"""
code_session toolset — conversational coding-agent sessions for the RCA loop.

The agent decides when it needs deep code work, starts a session, converses
(send → read → decide → send), and ends it. read mode is always available;
edit mode (write the fix) must be enabled in config AND is intended to sit
behind the fix-confidence gate.

Config (config.yaml):
  toolsets:
    code_session:
      enabled: true
      config:
        repo_dir: /data/repos          # same cache the code_analyst maintains
        repos: [example-app, frontend]  # names under repo_dir (allow-list)
        model: open-large
        allow_edit: false              # edit sessions refused until true
        send_timeout: 420
"""
import logging

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class CodeSessionToolset(Toolset):
    name = "code_session"
    description = (
        "Conversational coding-agent sessions over the configured repos. Use "
        "code_session_start to open a session (mode=read to trace/understand "
        "code), code_session_send to ask questions or give instructions — each "
        "send blocks until the coding agent finishes and returns its answer — "
        "and code_session_end when done. Prefer quick code_analyst tools "
        "(blame, log, diff) for single lookups; sessions are for deep, "
        "multi-step code understanding."
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = config or {}
        from pathlib import Path
        self.repo_dir = Path(cfg.get("repo_dir", "/data/repos"))
        self.repos: list[str] = cfg.get("repos", [])
        self.allow_edit: bool = bool(cfg.get("allow_edit", False))
        self._agent = None
        self._agent_cfg = {
            "api_base": cfg.get("api_base", ""),
            "api_key": cfg.get("api_key", ""),
            "model": cfg.get("model", "open-large"),
            "send_timeout": int(cfg.get("send_timeout", 420)),
        }

    def _get_agent(self):
        if self._agent is None:
            from vishwakarma.core.code_agent import OpenCodeAgent
            self._agent = OpenCodeAgent(
                api_base=self._agent_cfg["api_base"],
                api_key=self._agent_cfg["api_key"],
                model=self._agent_cfg["model"],
                send_timeout=self._agent_cfg["send_timeout"],
            )
        return self._agent

    def check_prerequisites(self) -> tuple[bool, str]:
        import shutil
        if shutil.which("opencode") is None:
            return False, "opencode binary not found"
        if not self.repos:
            return False, "no repos configured (toolsets.code_session.config.repos)"
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        modes = ["read", "edit"] if self.allow_edit else ["read"]
        return [
            ToolDef(
                name="code_session_start",
                description=(
                    "Open a coding-agent session on a repo. mode=read for "
                    "understanding/tracing (edits are impossible in read mode)."
                    + (" mode=edit creates an isolated worktree where the agent "
                       "can write a fix." if self.allow_edit else "")
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string",
                                 "description": f"Repo name. One of: {', '.join(self.repos)}",
                                 "enum": self.repos},
                        "mode": {"type": "string", "enum": modes,
                                 "description": "read (default) or edit"},
                    },
                    "required": ["repo"],
                },
            ),
            ToolDef(
                name="code_session_send",
                description=(
                    "Send a question/instruction to an open session and get the "
                    "coding agent's answer. Blocks while it works (it may read "
                    "many files / run searches internally). Iterate: read the "
                    "answer, then send a follow-up."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "message": {"type": "string",
                                    "description": "Question or instruction for the coding agent"},
                    },
                    "required": ["session_id", "message"],
                },
            ),
            ToolDef(
                name="code_session_end",
                description=(
                    "Close a session. For edit sessions this returns the diff of "
                    "everything the coding agent changed."
                ),
                parameters={
                    "type": "object",
                    "properties": {"session_id": {"type": "string"}},
                    "required": ["session_id"],
                },
            ),
            ToolDef(
                name="propose_fix",
                description=(
                    "After an EDIT session has written a fix, propose it. You "
                    "supply your confidence inputs; the system scores the fix and "
                    "either opens a DRAFT PR (high confidence + localized + tests "
                    "pass) or returns the diff to post as a suggestion. Never "
                    "merges. Call this instead of code_session_end when the edit "
                    "is the proposed fix."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "title": {"type": "string", "description": "Short fix title"},
                        "rca": {"type": "string", "description": "The root cause this fixes"},
                        "rca_confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                        "exact_line_found": {"type": "boolean",
                                             "description": "Did you identify the exact offending line?"},
                        "pattern_matched": {"type": "boolean",
                                            "description": "Did a confirmed past pattern match?"},
                        "tests_passed": {"type": "boolean",
                                         "description": "Did the coding agent's tests pass? Omit if not run."},
                        "rollback": {"type": "string", "description": "How to roll back"},
                    },
                    "required": ["session_id", "title", "rca", "rca_confidence"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        try:
            if tool_name == "code_session_start":
                return self._start(params)
            if tool_name == "code_session_send":
                return self._send(params)
            if tool_name == "code_session_end":
                return self._end(params)
            if tool_name == "propose_fix":
                return self._propose_fix(params)
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR,
                              error=f"Unknown tool: {tool_name}")
        except Exception as e:
            err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR, error=err)

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _start(self, params: dict) -> ToolOutput:
        repo = params.get("repo", "")
        mode = params.get("mode", "read")
        if repo not in self.repos:
            return ToolOutput(tool_name="code_session_start", status=ToolStatus.ERROR,
                              error=f"repo '{repo}' not in allow-list {self.repos}")
        if mode == "edit" and not self.allow_edit:
            return ToolOutput(tool_name="code_session_start", status=ToolStatus.ERROR,
                              error="edit mode is disabled (toolsets.code_session.config.allow_edit)")
        sid = self._get_agent().start(str(self.repo_dir / repo), mode=mode)
        return ToolOutput(
            tool_name="code_session_start", status=ToolStatus.SUCCESS,
            output=f"Session {sid} open on {repo} ({mode} mode). "
                   f"Use code_session_send to converse.",
            invocation=f"code_session_start({repo}, {mode})")

    def _send(self, params: dict) -> ToolOutput:
        sid = params.get("session_id", "")
        msg = params.get("message", "").strip()
        if not msg:
            return ToolOutput(tool_name="code_session_send", status=ToolStatus.ERROR,
                              error="message required")
        answer = self._get_agent().send(sid, msg)
        return ToolOutput(tool_name="code_session_send", status=ToolStatus.SUCCESS,
                          output=answer, invocation=f"code_session_send({sid}, {msg[:50]})")

    def _end(self, params: dict) -> ToolOutput:
        sid = params.get("session_id", "")
        agent = self._get_agent()
        transcript = []
        try:
            transcript = agent.transcript(sid)
        except Exception:
            pass
        result = agent.end(sid)
        # Checkpoint the code session into the durable investigation so a
        # resumed run keeps the code-work context.
        self._checkpoint_session(sid, transcript, result)
        out = f"Session closed ({result['mode']})."
        if result.get("diff"):
            out += f"\nBranch: {result.get('branch')}\n\n--- DIFF ---\n{result['diff'][:20000]}"
        return ToolOutput(tool_name="code_session_end", status=ToolStatus.SUCCESS,
                          output=out, invocation=f"code_session_end({sid})")

    def _propose_fix(self, params: dict) -> ToolOutput:
        sid = params.get("session_id", "")
        title = params.get("title", "Argus fix")
        rca = params.get("rca", "")
        agent = self._get_agent()
        try:
            info = agent.session_info(sid)
        except Exception as e:
            return ToolOutput(tool_name="propose_fix", status=ToolStatus.ERROR,
                              error=f"unknown session: {e}")
        if info.get("mode") != "edit":
            return ToolOutput(tool_name="propose_fix", status=ToolStatus.ERROR,
                              error="propose_fix requires an edit-mode session")

        committed = agent.commit_fix(sid, f"Argus fix: {title[:60]}")
        if committed.get("error"):
            agent.end(sid)
            return ToolOutput(tool_name="propose_fix", status=ToolStatus.ERROR,
                              error=committed["error"])

        # Score the fix.
        from vishwakarma.core.fix_scorer import score_fix
        decision = score_fix(
            rca_confidence=params.get("rca_confidence", ""),
            exact_line_found=bool(params.get("exact_line_found")),
            pattern_matched=bool(params.get("pattern_matched")),
            diff_files=committed["diff_files"], diff_lines=committed["diff_lines"],
            tests_passed=params.get("tests_passed"),
        )

        # Mark the investigation as awaiting fix review (console + ops).
        try:
            from vishwakarma.core.toolcontext import current_incident
            from vishwakarma.storage.investigations import (
                get_investigation, finish_investigation)
            iid = current_incident.get()
            if iid and get_investigation(iid):
                finish_investigation(iid, "awaiting_fix_review")
        except Exception:
            pass

        diff_preview = committed["diff"][:8000]
        base_msg = (
            f"Fix confidence: {decision.confidence} (score {decision.score}; "
            f"{', '.join(decision.reasons)})\n"
            f"Diff: {committed['diff_files']} files / {committed['diff_lines']} lines on "
            f"branch {committed['branch']}\n\n{diff_preview}"
        )

        if decision.action == "draft_pr":
            from vishwakarma.core.pr_creator import (
                get_pr_creator, build_pr_body, parse_owner_repo)
            pc = get_pr_creator()
            if pc is None:
                agent.end(sid)
                return ToolOutput(
                    tool_name="propose_fix", status=ToolStatus.SUCCESS,
                    output="DECISION: draft_pr — but GitHub is not configured, so no "
                           "PR was opened. Post this fix as a suggestion:\n\n" + base_msg,
                    invocation=f"propose_fix({sid})")
            # owner/repo from the worktree's origin
            origin = self._origin_url(info["worktree"])
            owner_repo = parse_owner_repo(origin) if origin else None
            if not owner_repo:
                agent.end(sid)
                return ToolOutput(tool_name="propose_fix", status=ToolStatus.ERROR,
                                  error=f"could not parse owner/repo from origin {origin!r}")
            body = build_pr_body(rca, "", params.get("rollback", ""), decision)
            res = pc.create_draft_pr(
                owner=owner_repo[0], repo=owner_repo[1],
                worktree_path=info["worktree"], branch=committed["branch"],
                title=title, body=body)
            agent.end(sid)
            if res.get("error"):
                return ToolOutput(tool_name="propose_fix", status=ToolStatus.ERROR,
                                  error=f"PR creation failed: {res['error']}\n\n{base_msg}")
            return ToolOutput(
                tool_name="propose_fix", status=ToolStatus.SUCCESS,
                output=f"DRAFT PR opened: {res['url']} (#{res['number']})\n\n{base_msg}",
                invocation=f"propose_fix({sid}) → PR")

        # propose_only
        agent.end(sid)
        return ToolOutput(
            tool_name="propose_fix", status=ToolStatus.SUCCESS,
            output=f"DECISION: propose only (gate not met). Post these changes as a "
                   f"suggestion for human review:\n\n{base_msg}",
            invocation=f"propose_fix({sid})")

    @staticmethod
    def _origin_url(worktree: str) -> str:
        import subprocess
        try:
            r = subprocess.run(["git", "-C", worktree, "remote", "get-url", "origin"],
                               capture_output=True, text=True, timeout=20)
            return r.stdout.strip()
        except Exception:
            return ""

    def _checkpoint_session(self, sid: str, transcript: list, result: dict) -> None:
        try:
            from vishwakarma.core.toolcontext import current_incident
            from vishwakarma.storage.investigations import (
                get_investigation, checkpoint_investigation)
            incident_id = current_incident.get()
            if not incident_id or not get_investigation(incident_id):
                return
            existing = get_investigation(incident_id).get("code_session") or {}
            sessions = existing.get("sessions", []) if isinstance(existing, dict) else []
            sessions.append({
                "session_id": sid, "mode": result.get("mode"),
                "branch": result.get("branch"),
                "diff_len": len(result.get("diff", "")),
                "transcript": transcript[-40:],  # cap
            })
            checkpoint_investigation(incident_id, code_session={"sessions": sessions})
        except Exception as e:
            log.debug(f"code_session checkpoint skipped: {e}")
