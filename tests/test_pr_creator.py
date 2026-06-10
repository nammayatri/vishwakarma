"""
PR-creator tests — branch push (to a local bare-repo "remote") + draft-PR
creation (against a fake GitHub REST server), idempotency, and the gate.

Run:  pytest tests/test_pr_creator.py -v
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

FAKE = Path(__file__).parent / "fake_github.py"
_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def fake_gh():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(FAKE), "--port", str(port)])
    # wait for it
    for _ in range(40):
        try:
            import requests
            requests.get(f"http://127.0.0.1:{port}/repos/o/r/pulls", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    proc.kill()


@pytest.fixture()
def repo_with_fix(tmp_path):
    """A bare 'remote' + a worktree with a committed fix on argus/fix-x."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(work)], capture_output=True)
    (work / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], capture_output=True, env=_ENV)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], capture_output=True, env=_ENV)
    # seed the bare remote with main
    subprocess.run(["git", "-C", str(work), "push", str(bare), "main"], capture_output=True, env=_ENV)
    # make the fix branch
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "argus/fix-x"], capture_output=True, env=_ENV)
    (work / "app.py").write_text("x = 2  # fix\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], capture_output=True, env=_ENV)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "fix"], capture_output=True, env=_ENV)
    return {"bare": str(bare), "work": str(work)}


def test_create_draft_pr(fake_gh, repo_with_fix):
    from vishwakarma.core.pr_creator import PRCreator
    pc = PRCreator(token="fake-token", api_base=fake_gh)
    res = pc.create_draft_pr(
        owner="o", repo="r", worktree_path=repo_with_fix["work"],
        branch="argus/fix-x", title="Fix x", body="body",
        push_url=repo_with_fix["bare"])
    assert "error" not in res, res
    assert res["url"].endswith("/pull/1") and res["existing"] is False

    # branch landed on the bare remote
    out = subprocess.run(["git", "-C", repo_with_fix["bare"], "branch"],
                         capture_output=True, text=True).stdout
    assert "argus/fix-x" in out


def test_create_draft_pr_idempotent(fake_gh, repo_with_fix):
    from vishwakarma.core.pr_creator import PRCreator
    pc = PRCreator(token="t", api_base=fake_gh)
    args = dict(owner="o", repo="r", worktree_path=repo_with_fix["work"],
                branch="argus/fix-x", title="T", body="b",
                push_url=repo_with_fix["bare"])
    first = pc.create_draft_pr(**args)
    assert first["existing"] is False
    second = pc.create_draft_pr(**args)
    assert second["existing"] is True and second["number"] == first["number"]


def test_unconfigured_returns_error(repo_with_fix):
    from vishwakarma.core.pr_creator import PRCreator
    pc = PRCreator(token="")          # no token
    res = pc.create_draft_pr(owner="o", repo="r",
                             worktree_path=repo_with_fix["work"],
                             branch="argus/fix-x", title="T", body="b",
                             push_url=repo_with_fix["bare"])
    assert res.get("error") and "token" in res["error"]


def test_parse_owner_repo():
    from vishwakarma.core.pr_creator import parse_owner_repo
    assert parse_owner_repo("https://github.com/your-org/backend.git") == \
        ("your-org", "backend")
    assert parse_owner_repo("git@github.com:org/repo.git") == ("org", "repo")
    assert parse_owner_repo("not-a-url") is None


def test_build_pr_body():
    from vishwakarma.core.pr_creator import build_pr_body
    from vishwakarma.core.fix_scorer import score_fix
    d = score_fix("HIGH", exact_line_found=True, pattern_matched=True,
                  diff_files=1, diff_lines=5, tests_passed=True)
    body = build_pr_body("root cause text", "evidence text", "revert it", d)
    assert "Argus proposed fix" in body and "HIGH" in body
    assert "root cause text" in body and "Draft PR" in body
