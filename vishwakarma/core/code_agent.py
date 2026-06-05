"""
CodeAgent — conversational coding-agent sessions the RCA agent drives.

The RCA agent acts as a human-proxy: it opens a session on a repo, sends a
question/instruction, blocks on the response, reads it, and decides the next
message — iterating the way a developer would. Read mode is for tracing and
understanding; edit mode (gated upstream) is for writing the fix.

v1 backend: OpenCode (`opencode serve`, OpenAI-compatible provider = the
existing LLM gateway). The adapter isolates the backend — Aider/Claude Code
can implement the same interface later.

Mechanics (probed against OpenCode 1.4.3):
  POST /session                      → {id: "ses_..."}
  POST /session/{id}/message        → BLOCKING; {info: {error?, tokens, cost},
                                       parts: [{type: text|tool|reasoning, ...}]}
  agent "plan"  — read-only (edit tools disallowed server-side)
  agent "build" — full tools (edit mode; always in a throwaway worktree)

Server scoping is per-directory: one `opencode serve` per session, cwd-bound
to the repo (read) or a worktree (edit). Killed on end().

Safety:
  - read sessions use the plan agent — OpenCode itself refuses edits.
  - edit sessions run in `git worktree` on branch argus/fix-<name>; the main
    clone is never touched; worktree removed on end().
  - provider API key is passed via the child process env, never written to disk.
"""
import json
import logging
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SERVER_START_TIMEOUT = 30      # seconds to wait for opencode serve to come up
DEFAULT_SEND_TIMEOUT = 420     # per message — OpenCode runs many internal steps
DEFAULT_SESSION_BUDGET = 2400  # wall-clock cap per session (40 min)


@dataclass
class CodeSession:
    session_id: str            # OpenCode session id
    mode: str                  # 'read' | 'edit'
    repo_path: str             # directory the server is scoped to (repo or worktree)
    port: int
    proc: subprocess.Popen
    branch: str = ""           # edit mode: the worktree branch
    base_repo: str = ""        # edit mode: the source clone the worktree came from
    started_at: float = field(default_factory=time.time)
    transcript: list = field(default_factory=list)   # [{role, text, at}] — checkpointable


class OpenCodeAgent:
    """Session-based adapter over a headless OpenCode server."""

    def __init__(
        self,
        provider_id: str = "vk-gateway",
        api_base: str = "",
        api_key: str = "",
        model: str = "open-large",
        opencode_bin: str = "opencode",
        send_timeout: int = DEFAULT_SEND_TIMEOUT,
        session_budget: int = DEFAULT_SESSION_BUDGET,
    ):
        self.provider_id = provider_id
        self.api_base = api_base
        self.api_key = api_key
        self.model = model
        self.opencode_bin = opencode_bin
        self.send_timeout = send_timeout
        self.session_budget = session_budget
        self._sessions: dict[str, CodeSession] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, repo_path: str, mode: str = "read", session_name: str = "") -> str:
        """
        Start a session on a repo. read → server scoped to the clone itself
        (plan agent, no edits). edit → throwaway worktree on a fix branch.
        Returns our session handle id.
        """
        if mode not in ("read", "edit"):
            raise ValueError(f"mode must be read|edit, got {mode}")
        repo = Path(repo_path).resolve()
        if not (repo / ".git").exists():
            raise ValueError(f"not a git repo: {repo}")

        branch = ""
        base_repo = ""
        workdir = repo
        if mode == "edit":
            name = session_name or f"s{int(time.time())}"
            branch = f"argus/fix-{name}"
            workdir = repo.parent / f"{repo.name}-wt-{name}"
            base_repo = str(repo)
            r = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", str(workdir), "-b", branch],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                raise RuntimeError(f"worktree add failed: {r.stderr.strip()[:300]}")

        port = _free_port()
        self._write_provider_config(workdir)
        proc = subprocess.Popen(
            [self.opencode_bin, "serve", "--port", str(port), "--pure"],
            cwd=str(workdir),
            env=self._child_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_healthy(port, SERVER_START_TIMEOUT)
            oc_session = _http(port, "POST", "/session",
                               {"title": session_name or f"argus-{mode}"})
            sid = oc_session["id"]
        except Exception:
            proc.kill()
            if mode == "edit":
                self._remove_worktree(base_repo, workdir)
            raise

        cs = CodeSession(session_id=sid, mode=mode, repo_path=str(workdir),
                         port=port, proc=proc, branch=branch, base_repo=base_repo)
        self._sessions[sid] = cs
        log.info(f"CodeAgent session {sid} started ({mode}, {workdir})")
        return sid

    def send(self, session_id: str, message: str) -> str:
        """
        Send one instruction/question; BLOCK until OpenCode finishes its
        internal steps; return the assistant's text. The RCA agent reads it
        and decides the next message.
        """
        cs = self._get(session_id)
        if time.time() - cs.started_at > self.session_budget:
            raise TimeoutError(
                f"session budget exceeded ({self.session_budget}s) — end the session")

        agent = "plan" if cs.mode == "read" else "build"
        body = {
            "model": {"providerID": self.provider_id, "modelID": self.model},
            "agent": agent,
            "parts": [{"type": "text", "text": message}],
        }
        cs.transcript.append({"role": "user", "text": message, "at": time.time()})
        resp = _http(cs.port, "POST", f"/session/{session_id}/message",
                     body, timeout=self.send_timeout)

        err = (resp.get("info") or {}).get("error")
        if err:
            detail = (err.get("data") or {}).get("message") or err.get("name") or str(err)
            raise RuntimeError(f"OpenCode error: {detail[:300]}")

        texts = [p.get("text", "") for p in resp.get("parts", [])
                 if p.get("type") == "text"]
        answer = "\n".join(t for t in texts if t).strip() or "(no text response)"
        cs.transcript.append({"role": "assistant", "text": answer, "at": time.time()})
        return answer

    def end(self, session_id: str) -> dict:
        """
        Close the session. edit mode → collect the diff from the worktree
        (uncommitted + committed-on-branch vs base), then clean up.
        Returns {mode, diff, branch, transcript_len}.
        """
        cs = self._get(session_id)
        diff = ""
        try:
            if cs.mode == "edit":
                diff = self._collect_diff(cs)
        finally:
            try:
                cs.proc.kill()
            except Exception:
                pass
            if cs.mode == "edit" and cs.base_repo:
                self._remove_worktree(cs.base_repo, Path(cs.repo_path))
            self._sessions.pop(session_id, None)
        log.info(f"CodeAgent session {session_id} ended ({cs.mode}, diff={len(diff)} chars)")
        return {"mode": cs.mode, "diff": diff, "branch": cs.branch,
                "transcript_len": len(cs.transcript)}

    def transcript(self, session_id: str) -> list:
        """For checkpointing into investigations.code_session."""
        return self._get(session_id).transcript

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get(self, session_id: str) -> CodeSession:
        cs = self._sessions.get(session_id)
        if cs is None:
            raise KeyError(f"unknown code session: {session_id}")
        if cs.proc.poll() is not None:
            raise RuntimeError(f"OpenCode server for {session_id} died")
        return cs

    def _child_env(self) -> dict:
        import os
        env = dict(os.environ)
        env["VK_GATEWAY_KEY"] = self.api_key  # consumed via {env:...} in config
        return env

    def _write_provider_config(self, workdir: Path) -> None:
        """
        Project-scoped opencode.json pointing at the gateway. The key is an
        env reference — it never lands on disk.
        Skipped if the project already has an opencode.json (e.g. tests).
        """
        cfg_path = workdir / "opencode.json"
        if cfg_path.exists() or not self.api_base:
            return
        cfg = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                self.provider_id: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "VK Gateway",
                    "options": {
                        "baseURL": self.api_base,
                        "apiKey": "{env:VK_GATEWAY_KEY}",
                    },
                    "models": {self.model: {"name": self.model}},
                }
            },
            "model": f"{self.provider_id}/{self.model}",
        }
        cfg_path.write_text(json.dumps(cfg, indent=2))

    def _collect_diff(self, cs: CodeSession) -> str:
        """Everything the session changed: stage all (captures new files), diff vs HEAD."""
        wt = cs.repo_path
        subprocess.run(["git", "-C", wt, "add", "-A"], capture_output=True, timeout=60)
        r = subprocess.run(
            ["git", "-C", wt, "diff", "--cached", "HEAD"],
            capture_output=True, text=True, timeout=60)
        return r.stdout

    @staticmethod
    def _remove_worktree(base_repo: str, workdir: Path) -> None:
        try:
            subprocess.run(
                ["git", "-C", base_repo, "worktree", "remove", "--force", str(workdir)],
                capture_output=True, timeout=60,
            )
        except Exception as e:
            log.warning(f"worktree cleanup failed for {workdir}: {e}")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(port: int, timeout: int) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            _http(port, "GET", "/global/health", timeout=3)
            return
        except Exception as e:
            last_err = e
            time.sleep(0.4)
    raise TimeoutError(f"opencode serve not healthy after {timeout}s: {last_err}")


def _http(port: int, method: str, path: str, body: dict | None = None,
          timeout: int = 30) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data) if data else {}
