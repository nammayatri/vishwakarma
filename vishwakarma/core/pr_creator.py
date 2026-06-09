"""
Draft-PR creation — closes the fix loop.

After an OpenCode edit session writes a fix on an `argus/fix-*` branch in a
worktree, this pushes the branch and opens a DRAFT pull request via the
GitHub REST API. PRs are ALWAYS draft, never auto-merged; a human reviews and
merges. Idempotent per branch: if a PR for the head branch already exists, it
is returned instead of opening a duplicate.

Auth: a token (GitHub App installation token or PAT) via
github.token / GITHUB_TOKEN. Push uses the token in the remote URL for that
one command (not persisted).

This module has NO hard dependency on a live GitHub at import time — it's
fully testable against a fake REST endpoint + a local bare-repo "remote".
"""
import logging
import re
import subprocess

import requests

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.github.com"


class PRCreator:
    def __init__(self, token: str = "", api_base: str = DEFAULT_API_BASE,
                 default_base_branch: str = "main"):
        self.token = token
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.default_base = default_base_branch
        self._session = requests.Session()
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        self._session.headers["Accept"] = "application/vnd.github+json"

    @property
    def configured(self) -> bool:
        return bool(self.token)

    # ── Public ────────────────────────────────────────────────────────────────

    def create_draft_pr(
        self,
        owner: str,
        repo: str,
        worktree_path: str,
        branch: str,
        title: str,
        body: str,
        base: str = "",
        push_url: str = "",
    ) -> dict:
        """
        Push `branch` from `worktree_path` and open a DRAFT PR.
        Returns {"url": ..., "number": ..., "existing": bool} or {"error": ...}.

        push_url overrides the push remote (tests point this at a local bare
        repo). In production the branch's existing 'origin' is used with the
        token injected.
        """
        base = base or self.default_base
        if not self.configured:
            return {"error": "no GitHub token configured"}

        # Idempotency: existing PR for this head?
        existing = self._find_pr(owner, repo, branch)
        if existing:
            log.info(f"Draft PR already exists for {branch}: {existing['url']}")
            return {**existing, "existing": True}

        pushed = self._push_branch(worktree_path, branch, push_url, owner, repo)
        if pushed.get("error"):
            return pushed

        try:
            r = self._session.post(
                f"{self.api_base}/repos/{owner}/{repo}/pulls",
                json={"title": title, "head": branch, "base": base,
                      "body": body, "draft": True},
                timeout=30,
            )
            if r.status_code not in (200, 201):
                return {"error": f"PR create failed {r.status_code}: {r.text[:300]}"}
            data = r.json()
            log.info(f"Opened draft PR #{data.get('number')}: {data.get('html_url')}")
            return {"url": data.get("html_url"), "number": data.get("number"),
                    "existing": False}
        except Exception as e:
            return {"error": f"PR create exception: {e}"}

    # ── Internals ─────────────────────────────────────────────────────────────

    def _find_pr(self, owner: str, repo: str, branch: str) -> dict | None:
        try:
            r = self._session.get(
                f"{self.api_base}/repos/{owner}/{repo}/pulls",
                params={"head": f"{owner}:{branch}", "state": "open"}, timeout=20)
            if r.status_code == 200 and r.json():
                p = r.json()[0]
                return {"url": p.get("html_url"), "number": p.get("number")}
        except Exception as e:
            log.debug(f"PR lookup failed: {e}")
        return None

    def _push_branch(self, worktree_path: str, branch: str, push_url: str,
                     owner: str, repo: str) -> dict:
        remote = push_url
        if not remote:
            origin = self._origin(worktree_path)
            if origin.startswith("https://github.com") and self.token:
                # Inject the token into the https origin for this one push.
                remote = origin.replace("https://", f"https://x-access-token:{self.token}@")
            elif origin:
                remote = origin     # local/ssh origin (tests, self-hosted) — as-is
            else:
                remote = f"https://x-access-token:{self.token}@github.com/{owner}/{repo}.git"
        try:
            r = subprocess.run(
                ["git", "-C", worktree_path, "push", remote, f"HEAD:refs/heads/{branch}"],
                capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                return {"error": f"git push failed: {r.stderr.strip()[:300]}"}
            return {"ok": True}
        except Exception as e:
            return {"error": f"git push exception: {e}"}

    @staticmethod
    def _origin(worktree_path: str) -> str:
        try:
            r = subprocess.run(["git", "-C", worktree_path, "remote", "get-url", "origin"],
                               capture_output=True, text=True, timeout=20)
            return r.stdout.strip()
        except Exception:
            return ""


_creator: "PRCreator | None" = None


def init_pr_creator(enabled: bool, token: str, api_base: str = DEFAULT_API_BASE,
                    default_base: str = "main") -> None:
    global _creator
    _creator = PRCreator(token, api_base, default_base) if (enabled and token) else None
    if _creator:
        log.info("PR creator ready (draft-PR fix path enabled)")


def get_pr_creator() -> "PRCreator | None":
    return _creator


def build_pr_body(rca: str, evidence: str, rollback: str, fix_decision) -> str:
    """Standard draft-PR body: RCA + evidence + rollback + confidence."""
    return (
        f"## 🤖 Argus proposed fix\n\n"
        f"**Fix confidence:** {fix_decision.confidence} (score {fix_decision.score})\n"
        f"**Reasons:** {', '.join(fix_decision.reasons)}\n\n"
        f"### Root cause\n{rca}\n\n"
        f"### Evidence\n{evidence or '_see RCA_'}\n\n"
        f"### Rollback\n{rollback or 'Revert this PR.'}\n\n"
        f"---\n_Draft PR — review and merge manually. Generated by Argus._"
    )


def parse_owner_repo(remote_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a github remote URL."""
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote_url or "")
    return (m.group(1), m.group(2)) if m else None
