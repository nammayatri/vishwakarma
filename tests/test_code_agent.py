"""
CodeAgent adapter + code_session toolset tests.

Unit tests run against tests/fake_opencode.py (deterministic, no network,
no LLM). An opt-in integration test hits a REAL `opencode serve` when
VK_TEST_OPENCODE=1 and VK_GATEWAY_KEY are set.

Run:  pytest tests/test_code_agent.py -v
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

from vishwakarma.core.code_agent import OpenCodeAgent
from vishwakarma.core.models import ToolStatus

FAKE = Path(__file__).parent / "fake_opencode.py"


@pytest.fixture()
def fake_bin(tmp_path) -> str:
    """Wrapper script so the adapter can Popen the fake like a binary."""
    import sys
    wrapper = tmp_path / "opencode"
    wrapper.write_text(f"#!/bin/bash\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


@pytest.fixture()
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "init", "-b", "main", str(r)], capture_output=True)
    (r / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(r), "add", "-A"], capture_output=True, env=env)
    subprocess.run(["git", "-C", str(r), "commit", "-m", "init"],
                   capture_output=True, env=env)
    return r


@pytest.fixture()
def agent(fake_bin) -> OpenCodeAgent:
    a = OpenCodeAgent(opencode_bin=fake_bin, send_timeout=20)
    yield a
    # safety: kill any leftover sessions
    for sid in list(a._sessions):
        try:
            a.end(sid)
        except Exception:
            pass


def test_read_session_roundtrip(agent, repo):
    sid = agent.start(str(repo), mode="read")
    answer = agent.send(sid, "what does app.py do?")
    assert answer == "did: what does app.py do?"
    # read mode → plan agent (edits disallowed server-side)
    assert (repo / ".last_agent").read_text() == "plan"

    t = agent.transcript(sid)
    assert [m["role"] for m in t] == ["user", "assistant"]

    result = agent.end(sid)
    assert result["mode"] == "read" and result["diff"] == ""
    with pytest.raises(KeyError):
        agent.send(sid, "after end")


def test_edit_session_worktree_diff_and_cleanup(agent, repo):
    sid = agent.start(str(repo), mode="edit", session_name="t1")
    cs = agent._sessions[sid]
    wt = Path(cs.repo_path)
    assert wt != repo and wt.exists()           # isolated worktree
    assert cs.branch == "argus/fix-t1"
    assert (wt / ".last_agent").exists() is False

    agent.send(sid, "EDIT: the fix content")
    assert (wt / ".last_agent").read_text() == "build"  # edit → build agent
    assert (wt / "fix.txt").exists()
    assert not (repo / "fix.txt").exists()      # main clone untouched

    result = agent.end(sid)
    assert "fix.txt" in result["diff"]
    assert "the fix content" in result["diff"]
    assert result["branch"] == "argus/fix-t1"
    assert not wt.exists()                      # worktree removed

    # main repo still clean
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    assert "fix.txt" not in status


def test_provider_error_propagates(agent, repo):
    sid = agent.start(str(repo), mode="read")
    with pytest.raises(RuntimeError, match="simulated provider failure"):
        agent.send(sid, "FAIL please")
    agent.end(sid)


def test_session_budget(fake_bin, repo):
    a = OpenCodeAgent(opencode_bin=fake_bin, session_budget=0)
    sid = a.start(str(repo), mode="read")
    with pytest.raises(TimeoutError, match="budget"):
        a.send(sid, "anything")
    a.end(sid)


def test_invalid_inputs(agent, repo, tmp_path):
    with pytest.raises(ValueError, match="mode"):
        agent.start(str(repo), mode="yolo")
    with pytest.raises(ValueError, match="not a git repo"):
        agent.start(str(tmp_path), mode="read")
    with pytest.raises(KeyError):
        agent.send("ses_nope", "x")


# ── Toolset layer ──────────────────────────────────────────────────────────────

@pytest.fixture()
def toolset(fake_bin, repo, monkeypatch):
    from vishwakarma.plugins.toolsets.code_session.code_session import CodeSessionToolset
    ts = CodeSessionToolset({
        "repo_dir": str(repo.parent),
        "repos": [repo.name],
        "allow_edit": False,
    })
    # inject the fake-binary agent
    ts._agent = OpenCodeAgent(opencode_bin=fake_bin, send_timeout=20)
    return ts


def test_toolset_read_flow(toolset, repo):
    out = toolset.execute("code_session_start", {"repo": repo.name, "mode": "read"})
    assert out.status == ToolStatus.SUCCESS, out.error
    sid = str(out.output).split()[1]

    out = toolset.execute("code_session_send", {"session_id": sid, "message": "trace it"})
    assert out.status == ToolStatus.SUCCESS and "did: trace it" in str(out.output)

    out = toolset.execute("code_session_end", {"session_id": sid})
    assert out.status == ToolStatus.SUCCESS


def test_toolset_edit_gated(toolset, repo):
    out = toolset.execute("code_session_start", {"repo": repo.name, "mode": "edit"})
    assert out.status == ToolStatus.ERROR
    assert "allow_edit" in out.error


def test_toolset_repo_allowlist(toolset):
    out = toolset.execute("code_session_start", {"repo": "evil"})
    assert out.status == ToolStatus.ERROR and "allow-list" in out.error


def test_toolset_edit_mode_only_listed_when_enabled(fake_bin, repo):
    from vishwakarma.plugins.toolsets.code_session.code_session import CodeSessionToolset
    ts_ro = CodeSessionToolset({"repos": ["x"], "allow_edit": False})
    start_def = next(t for t in ts_ro.get_tools() if t.name == "code_session_start")
    assert start_def.parameters["properties"]["mode"]["enum"] == ["read"]

    ts_rw = CodeSessionToolset({"repos": ["x"], "allow_edit": True})
    start_def = next(t for t in ts_rw.get_tools() if t.name == "code_session_start")
    assert start_def.parameters["properties"]["mode"]["enum"] == ["read", "edit"]


# ── Opt-in integration against real OpenCode ──────────────────────────────────

@pytest.mark.skipif(
    os.environ.get("VK_TEST_OPENCODE") != "1" or not os.environ.get("VK_GATEWAY_KEY"),
    reason="set VK_TEST_OPENCODE=1 and VK_GATEWAY_KEY to run against real opencode",
)
def test_real_opencode_read_session(repo):
    a = OpenCodeAgent(
        api_base="https://llm-gateway.example.com/v1",
        api_key=os.environ["VK_GATEWAY_KEY"],
        model="open-large",
        provider_id="vk-gateway",
        send_timeout=120,
    )
    sid = a.start(str(repo), mode="read")
    try:
        answer = a.send(sid, "What does app.py contain? One short sentence.")
        assert "x" in answer.lower() or "1" in answer
    finally:
        a.end(sid)
