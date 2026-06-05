"""
Code-analyst toolset — Tier-1 code intelligence inside the RCA loop.

Turns "CPU is high" into "commit c4ddaff changed the forwarding logic at
10:34, here's the diff". Fast, cheap, read-only lookups for correlating an
incident to a code change; the deep conversational work (OpenCode sessions)
is the separate Tier-2 path.

Repo cache strategy: clone once into `repo_dir` (PVC in prod), then
`repo_sync` does fetch + fast-forward before an investigation so the agent
always reads current code. No full reclones.

Guardrails:
  - Repos must be declared in config (allow-list) — arbitrary URLs refused.
  - Read-only git only: clone/fetch/log/blame/show/diff. Never push/commit.
  - All paths resolved inside the repo root (no traversal).

Config (config.yaml):
  toolsets:
    code_analyst:
      enabled: true
      config:
        repo_dir: /data/repos
        default_branch: main
        repos:
          - name: example-app
            url: https://github.com/your-org/backend.git
            branch: main
            sparse: ["Backend"]        # optional sparse-checkout paths
"""
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)

GIT_TIMEOUT = 120          # per git command
CLONE_TIMEOUT = 900        # initial clone of a big monorepo
MAX_OUTPUT_CHARS = 24_000  # caller-side compression handles the rest


@register_toolset
class CodeAnalystToolset(Toolset):
    name = "code_analyst"
    description = (
        "Read-only code intelligence over the configured git repos: sync to "
        "latest, blame lines, list commits around an incident time, diff a "
        "deploy, structural code search, and map stack frames to source. "
        "Use to correlate an incident with the code change that caused it."
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = config or {}
        self.repo_dir = Path(cfg.get("repo_dir", "/data/repos"))
        self.default_branch = cfg.get("default_branch", "main")
        # name -> {url, branch, sparse}
        self.repos: dict[str, dict] = {
            r["name"]: r for r in cfg.get("repos", []) if r.get("name") and r.get("url")
        }

    # ── Toolset interface ─────────────────────────────────────────────────────

    def check_prerequisites(self) -> tuple[bool, str]:
        if shutil.which("git") is None:
            return False, "git binary not found"
        if not self.repos:
            return False, "no repos configured (toolsets.code_analyst.config.repos)"
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        repo_names = list(self.repos.keys())
        repo_param = {
            "type": "string",
            "description": f"Repo name. One of: {', '.join(repo_names)}",
            "enum": repo_names,
        }
        return [
            ToolDef(
                name="repo_sync",
                description=(
                    "Sync a repo to latest (clone once, then fetch + fast-forward). "
                    "Run this before reading code so you see current source."
                ),
                parameters={
                    "type": "object",
                    "properties": {"repo": repo_param},
                    "required": ["repo"],
                },
            ),
            ToolDef(
                name="git_blame",
                description=(
                    "Who last changed each line of a file region — returns commit, "
                    "author, date per line. Use to find the commit that introduced "
                    "a suspicious line."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": repo_param,
                        "file_path": {"type": "string", "description": "Path inside the repo"},
                        "line_start": {"type": "integer", "description": "First line (1-based)"},
                        "line_end": {"type": "integer", "description": "Last line (inclusive)"},
                    },
                    "required": ["repo", "file_path"],
                },
            ),
            ToolDef(
                name="git_log_around",
                description=(
                    "Commits in a time window around an incident (default ±24h). "
                    "Optionally limited to a path. Use with the alert's startsAt to "
                    "find what changed right before the incident."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": repo_param,
                        "timestamp": {
                            "type": "string",
                            "description": "Incident time, ISO8601 (e.g. 2026-06-05T10:35:00Z)",
                        },
                        "window_hours": {"type": "integer", "description": "Hours either side (default 24)"},
                        "path": {"type": "string", "description": "Optional path filter inside the repo"},
                    },
                    "required": ["repo", "timestamp"],
                },
            ),
            ToolDef(
                name="deploy_diff",
                description=(
                    "Diff what a deploy changed: give the deployed ref (commit sha or "
                    "tag — often embedded in the image tag) and get the change vs the "
                    "previous state. Optionally diff between two refs."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": repo_param,
                        "ref": {"type": "string", "description": "Deployed commit sha or tag"},
                        "base_ref": {
                            "type": "string",
                            "description": "Optional base to diff against (default: ref's parent)",
                        },
                        "stat_only": {
                            "type": "boolean",
                            "description": "true = file/line counts only (cheap overview)",
                        },
                    },
                    "required": ["repo", "ref"],
                },
            ),
            ToolDef(
                name="code_search",
                description=(
                    "Structural/text search across a repo (ast-grep when available, "
                    "ripgrep otherwise). Use for 'where is X handled?' lookups."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": repo_param,
                        "pattern": {"type": "string", "description": "Search pattern (regex for rg)"},
                        "path": {"type": "string", "description": "Optional subdirectory to search"},
                        "max_results": {"type": "integer", "description": "Default 50"},
                    },
                    "required": ["repo", "pattern"],
                },
            ),
            ToolDef(
                name="stacktrace_to_source",
                description=(
                    "Given a stack frame (file path + line from a log/stack trace), "
                    "return the surrounding source code with blame for the exact line."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": repo_param,
                        "file_path": {"type": "string", "description": "File from the stack frame"},
                        "line": {"type": "integer", "description": "Line number from the stack frame"},
                        "context": {"type": "integer", "description": "Context lines around it (default 15)"},
                    },
                    "required": ["repo", "file_path", "line"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        handlers = {
            "repo_sync": self._repo_sync,
            "git_blame": self._git_blame,
            "git_log_around": self._git_log_around,
            "deploy_diff": self._deploy_diff,
            "code_search": self._code_search,
            "stacktrace_to_source": self._stacktrace_to_source,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR,
                              error=f"Unknown tool: {tool_name}")
        try:
            return handler(params)
        except Exception as e:
            err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR, error=err)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _repo_cfg(self, name: str) -> dict:
        cfg = self.repos.get(name)
        if not cfg:
            raise ValueError(f"repo '{name}' not in allow-list ({list(self.repos)})")
        return cfg

    def _repo_path(self, name: str) -> Path:
        self._repo_cfg(name)  # validate allow-list
        return self.repo_dir / name

    def _safe_path(self, repo: str, file_path: str) -> Path:
        """Resolve a file path strictly inside the repo root (no traversal)."""
        root = self._repo_path(repo).resolve()
        p = (root / file_path.lstrip("/")).resolve()
        if not str(p).startswith(str(root)):
            raise ValueError(f"path escapes repo root: {file_path}")
        return p

    def _git(self, repo: str, *args: str, timeout: int = GIT_TIMEOUT) -> str:
        """Run a read-only git command in the repo. Push/commit are refused."""
        forbidden = {"push", "commit", "reset", "rebase", "merge", "checkout",
                     "branch", "tag", "remote", "config", "clean", "gc"}
        if args and args[0] in forbidden:
            raise ValueError(f"git {args[0]} not allowed (read-only toolset)")
        result = subprocess.run(
            ["git", "-C", str(self._repo_path(repo)), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:400]}")
        out = result.stdout
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(out)} chars total]"
        return out

    # ── Tools ─────────────────────────────────────────────────────────────────

    def _repo_sync(self, params: dict) -> ToolOutput:
        name = params["repo"]
        cfg = self._repo_cfg(name)
        path = self._repo_path(name)
        branch = cfg.get("branch", self.default_branch)

        if not (path / ".git").exists():
            # First clone — shallow-ish but with enough history for blame/log.
            self.repo_dir.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "clone", "--filter=blob:none", "--branch", branch,
                   cfg["url"], str(path)]
            log.info(f"code_analyst: cloning {name} ({cfg['url']})")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=CLONE_TIMEOUT)
            if result.returncode != 0:
                return ToolOutput(tool_name="repo_sync", status=ToolStatus.ERROR,
                                  error=f"clone failed: {result.stderr.strip()[:400]}")
            sparse = cfg.get("sparse")
            if sparse:
                subprocess.run(
                    ["git", "-C", str(path), "sparse-checkout", "set", *sparse],
                    capture_output=True, text=True, timeout=GIT_TIMEOUT,
                )
            return ToolOutput(tool_name="repo_sync", status=ToolStatus.SUCCESS,
                              output=f"Cloned {name} @ {branch}",
                              invocation=f"repo_sync({name})")

        # Existing clone — fetch + fast-forward only (never lose local state).
        self._git(name, "fetch", "--prune", "origin", branch)
        before = self._git(name, "rev-parse", "HEAD").strip()
        merge = subprocess.run(
            ["git", "-C", str(path), "merge", "--ff-only", f"origin/{branch}"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )  # merge --ff-only is the one safe 'write': it only moves the pointer forward
        after = self._git(name, "rev-parse", "HEAD").strip()
        if merge.returncode != 0:
            return ToolOutput(tool_name="repo_sync", status=ToolStatus.ERROR,
                              error=f"fast-forward failed: {merge.stderr.strip()[:200]}")
        changed = "up to date" if before == after else f"{before[:9]} → {after[:9]}"
        return ToolOutput(tool_name="repo_sync", status=ToolStatus.SUCCESS,
                          output=f"{name} synced to origin/{branch} ({changed})",
                          invocation=f"repo_sync({name})")

    def _git_blame(self, params: dict) -> ToolOutput:
        repo = params["repo"]
        file_path = params["file_path"]
        self._safe_path(repo, file_path)
        args = ["blame", "--date=iso"]
        if params.get("line_start"):
            end = params.get("line_end") or params["line_start"]
            args += ["-L", f"{params['line_start']},{end}"]
        args.append(file_path)
        out = self._git(repo, *args)
        return ToolOutput(tool_name="git_blame", status=ToolStatus.SUCCESS, output=out,
                          invocation=f"git_blame({repo}:{file_path})")

    def _git_log_around(self, params: dict) -> ToolOutput:
        repo = params["repo"]
        ts = params["timestamp"]
        hours = int(params.get("window_hours") or 24)
        # Compute the window in Python — git approxidate parsing of
        # "<ts> +N hours" is ambiguous (can read +N as a timezone offset).
        from datetime import datetime, timedelta
        raw = ts.strip().replace("Z", "+00:00")
        try:
            center = datetime.fromisoformat(raw)
        except ValueError:
            raise ValueError(f"timestamp must be ISO8601, got {ts!r}")
        since = (center - timedelta(hours=hours)).isoformat()
        until = (center + timedelta(hours=hours)).isoformat()
        args = [
            "log", f"--since={since}", f"--until={until}",
            "--date=iso", "--pretty=format:%h %ad %an  %s", "--no-merges",
        ]
        if params.get("path"):
            self._safe_path(repo, params["path"])
            args += ["--", params["path"]]
        out = self._git(repo, *args)
        if not out.strip():
            return ToolOutput(tool_name="git_log_around", status=ToolStatus.NO_DATA,
                              invocation=f"git_log_around({repo} @ {ts} ±{hours}h)")
        return ToolOutput(tool_name="git_log_around", status=ToolStatus.SUCCESS, output=out,
                          invocation=f"git_log_around({repo} @ {ts} ±{hours}h)")

    def _deploy_diff(self, params: dict) -> ToolOutput:
        repo = params["repo"]
        ref = params["ref"].strip()
        if not re.fullmatch(r"[\w.\-/]+", ref):
            raise ValueError(f"suspicious ref: {ref!r}")
        base = (params.get("base_ref") or f"{ref}~1").strip()
        if not re.fullmatch(r"[\w.\-/~^]+", base):
            raise ValueError(f"suspicious base_ref: {base!r}")
        args = ["diff", f"{base}..{ref}"]
        if params.get("stat_only"):
            args.insert(1, "--stat")
        out = self._git(repo, *args)
        header = self._git(repo, "show", "--no-patch",
                           "--pretty=format:%h %ad %an  %s", "--date=iso", ref)
        body = f"Deployed commit: {header}\n\n{out}" if out.strip() else \
               f"Deployed commit: {header}\n\n(no diff vs {base})"
        return ToolOutput(tool_name="deploy_diff", status=ToolStatus.SUCCESS, output=body,
                          invocation=f"deploy_diff({repo} {base}..{ref})")

    def _code_search(self, params: dict) -> ToolOutput:
        repo = params["repo"]
        pattern = params["pattern"]
        max_results = int(params.get("max_results") or 50)
        root = self._repo_path(repo)
        search_dir = root
        if params.get("path"):
            search_dir = self._safe_path(repo, params["path"])

        if shutil.which("ast-grep") or shutil.which("sg"):
            binary = shutil.which("ast-grep") or shutil.which("sg")
            cmd = [binary, "run", "--pattern", pattern, str(search_dir)]
        else:
            cmd = ["rg", "--line-number", "--max-count", "5",
                   "--max-columns", "300", "-m", str(max_results),
                   pattern, str(search_dir)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        # rg exit 1 = no matches (not an error)
        if result.returncode not in (0, 1):
            return ToolOutput(tool_name="code_search", status=ToolStatus.ERROR,
                              error=result.stderr.strip()[:400])
        out = result.stdout
        if not out.strip():
            return ToolOutput(tool_name="code_search", status=ToolStatus.NO_DATA,
                              invocation=f"code_search({repo}, {pattern[:40]})")
        # Make paths repo-relative for readability
        out = out.replace(str(root) + os.sep, "")
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return ToolOutput(tool_name="code_search", status=ToolStatus.SUCCESS, output=out,
                          invocation=f"code_search({repo}, {pattern[:40]})")

    def _stacktrace_to_source(self, params: dict) -> ToolOutput:
        repo = params["repo"]
        file_path = params["file_path"]
        line = int(params["line"])
        ctx = int(params.get("context") or 15)
        p = self._safe_path(repo, file_path)
        if not p.exists():
            # Stack traces often carry build paths — try matching by suffix.
            candidates = list(self._repo_path(repo).rglob(Path(file_path).name))
            if len(candidates) == 1:
                p = candidates[0]
                file_path = str(p.relative_to(self._repo_path(repo)))
            elif candidates:
                listing = "\n".join(str(c.relative_to(self._repo_path(repo))) for c in candidates[:10])
                return ToolOutput(tool_name="stacktrace_to_source", status=ToolStatus.ERROR,
                                  error=f"{file_path} ambiguous; candidates:\n{listing}")
            else:
                return ToolOutput(tool_name="stacktrace_to_source", status=ToolStatus.ERROR,
                                  error=f"{file_path} not found in {repo}")

        lines = p.read_text(errors="replace").splitlines()
        lo, hi = max(0, line - 1 - ctx), min(len(lines), line + ctx)
        numbered = [
            f"{'>>>' if i + 1 == line else '   '} {i + 1:5d}  {lines[i]}"
            for i in range(lo, hi)
        ]
        blame = ""
        try:
            blame = self._git(repo, "blame", "--date=iso",
                              "-L", f"{line},{line}", file_path).strip()
        except Exception:
            pass
        out = f"{file_path}:{line}\n\n" + "\n".join(numbered)
        if blame:
            out += f"\n\nBlame for line {line}:\n{blame}"
        return ToolOutput(tool_name="stacktrace_to_source", status=ToolStatus.SUCCESS,
                          output=out, invocation=f"stacktrace_to_source({repo}:{file_path}:{line})")
