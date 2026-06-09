"""
propose_fix end-to-end: edit session → commit fix → score → draft PR (or
propose-only when the gate isn't met). Uses the fake OpenCode binary, a fake
GitHub server, and a local bare-repo remote.

Run:  pytest tests/test_propose_fix.py -v
"""
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vishwakarma.core.models import ToolStatus

FAKE_OC = Path(__file__).parent / "fake_opencode.py"
FAKE_GH = Path(__file__).parent / "fake_github.py"
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
    proc = subprocess.Popen([sys.executable, str(FAKE_GH), "--port", str(port)])
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
def fake_oc_bin(tmp_path):
    wrapper = tmp_path / "opencode"
    wrapper.write_text(f"#!/bin/bash\nexec {sys.executable} {FAKE_OC} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


@pytest.fixture()
def repos(tmp_path):
    """A repo cache dir with one repo whose origin is a local bare remote."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)
    repo_dir = tmp_path / "repos"
    repo_dir.mkdir()
    app = repo_dir / "backend"
    subprocess.run(["git", "init", "-b", "main", str(app)], capture_output=True)
    (app / "h.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "-C", str(app), "add", "-A"], capture_output=True, env=_ENV)
    subprocess.run(["git", "-C", str(app), "commit", "-m", "init"], capture_output=True, env=_ENV)
    # origin = a github URL so owner/repo parses; push is redirected to the
    # bare remote in the test via monkeypatching PRCreator._origin.
    subprocess.run(["git", "-C", str(app), "remote", "add", "origin",
                    "https://github.com/example-app/backend.git"], capture_output=True, env=_ENV)
    subprocess.run(["git", "-C", str(app), "push", str(bare), "main"], capture_output=True, env=_ENV)
    return {"repo_dir": str(repo_dir), "bare": str(bare)}


def _toolset(fake_oc_bin, repos):
    from vishwakarma.plugins.toolsets.code_session.code_session import CodeSessionToolset
    from vishwakarma.core.code_agent import OpenCodeAgent
    ts = CodeSessionToolset({"repo_dir": repos["repo_dir"], "repos": ["backend"],
                             "allow_edit": True})
    ts._agent = OpenCodeAgent(opencode_bin=fake_oc_bin, send_timeout=20)
    return ts


def test_propose_fix_opens_draft_pr(fake_gh, fake_oc_bin, repos, monkeypatch):
    # PR creator pushes to the bare remote, not real github
    from vishwakarma.core import pr_creator
    pr_creator.init_pr_creator(True, "fake-token", fake_gh, "main")
    # redirect push target to the local bare remote
    monkeypatch.setattr(pr_creator.PRCreator, "_origin",
                        staticmethod(lambda wt: repos["bare"]))

    ts = _toolset(fake_oc_bin, repos)
    start = ts.execute("code_session_start", {"repo": "backend", "mode": "edit"})
    sid = str(start.output).split()[1]
    ts.execute("code_session_send", {"session_id": sid, "message": "EDIT: the fix"})

    out = ts.execute("propose_fix", {
        "session_id": sid, "title": "Fix f()", "rca": "wrong return value",
        "rca_confidence": "HIGH", "exact_line_found": True, "pattern_matched": True,
        "tests_passed": True, "rollback": "revert"})
    assert out.status == ToolStatus.SUCCESS, out.error
    assert "DRAFT PR opened" in str(out.output)
    assert "/pull/" in str(out.output)


def test_propose_fix_propose_only_when_tests_unknown(fake_oc_bin, repos):
    from vishwakarma.core import pr_creator
    pr_creator.init_pr_creator(False, "", "", "main")   # github disabled

    ts = _toolset(fake_oc_bin, repos)
    start = ts.execute("code_session_start", {"repo": "backend", "mode": "edit"})
    sid = str(start.output).split()[1]
    ts.execute("code_session_send", {"session_id": sid, "message": "EDIT: change"})

    out = ts.execute("propose_fix", {
        "session_id": sid, "title": "T", "rca": "x", "rca_confidence": "HIGH",
        "exact_line_found": True})   # tests_passed omitted → can't PR
    assert out.status == ToolStatus.SUCCESS, out.error
    assert "propose only" in str(out.output).lower()


def test_propose_fix_no_changes(fake_oc_bin, repos):
    from vishwakarma.core import pr_creator
    pr_creator.init_pr_creator(False, "", "", "main")
    ts = _toolset(fake_oc_bin, repos)
    start = ts.execute("code_session_start", {"repo": "backend", "mode": "edit"})
    sid = str(start.output).split()[1]
    # no EDIT message → nothing changed
    out = ts.execute("propose_fix", {"session_id": sid, "title": "T", "rca": "x",
                                     "rca_confidence": "LOW"})
    assert out.status == ToolStatus.ERROR and "no changes" in out.error
