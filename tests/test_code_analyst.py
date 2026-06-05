"""
code_analyst toolset tests — run against a generated fixture git repo.

The fixture simulates the real investigation flow: an "old" commit, a
"breaking deploy" commit at a known timestamp, and a stack-trace target —
then each tool is exercised the way the RCA agent would use it.

Run:  pytest tests/test_code_analyst.py -v
"""
import subprocess
import time
from pathlib import Path

import pytest

from vishwakarma.core.models import ToolStatus
from vishwakarma.plugins.toolsets.code_analyst.code_analyst import CodeAnalystToolset


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    base_env = {
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "f@x",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "f@x",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    if env:
        base_env.update(env)
    r = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, env=base_env)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory):
    """
    Build origin repo with: initial commit (T-2d) → breaking commit (T0) on
    handler.py line that a stack trace will point at.
    """
    origin = tmp_path_factory.mktemp("origin")
    subprocess.run(["git", "init", "-b", "main", str(origin)], capture_output=True)

    src = origin / "src"
    src.mkdir()
    (src / "handler.py").write_text(
        "def accept_order(order):\n"
        "    # auto-accept flow\n"
        "    return order.accept()\n"
    )
    (origin / "README.md").write_text("fixture\n")
    _git(origin, "add", "-A")
    two_days_ago = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 2 * 86400))
    _git(origin, "commit", "-m", "initial: auto-accept flow",
         env={"GIT_AUTHOR_DATE": two_days_ago, "GIT_COMMITTER_DATE": two_days_ago})

    # The "breaking deploy" — changes auto-accept to manual click
    (src / "handler.py").write_text(
        "def accept_order(order):\n"
        "    # BREAKING: now requires manual click\n"
        "    show_manual_screen(order)\n"
        "    return None\n"
    )
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "feat: manual OA screen for iOS")
    breaking_sha = _git(origin, "rev-parse", "HEAD").strip()

    return {"origin": origin, "breaking_sha": breaking_sha}


@pytest.fixture(scope="module")
def toolset(fixture_repo, tmp_path_factory) -> CodeAnalystToolset:
    repo_dir = tmp_path_factory.mktemp("repos")
    return CodeAnalystToolset({
        "repo_dir": str(repo_dir),
        "default_branch": "main",
        "repos": [{"name": "app", "url": str(fixture_repo["origin"]), "branch": "main"}],
    })


def test_repo_sync_clones_then_ffs(toolset, fixture_repo):
    out = toolset.execute("repo_sync", {"repo": "app"})
    assert out.status == ToolStatus.SUCCESS, out.error
    assert "Cloned app" in str(out.output)

    # New commit on origin → sync fast-forwards
    origin = fixture_repo["origin"]
    (origin / "new_file.txt").write_text("post-clone change\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "post-clone commit")

    out = toolset.execute("repo_sync", {"repo": "app"})
    assert out.status == ToolStatus.SUCCESS, out.error
    assert "→" in str(out.output)  # moved to new sha

    out = toolset.execute("repo_sync", {"repo": "app"})
    assert "up to date" in str(out.output)


def test_repo_allow_list(toolset):
    out = toolset.execute("repo_sync", {"repo": "not-configured"})
    assert out.status == ToolStatus.ERROR
    assert "allow-list" in out.error


def test_git_blame_finds_breaking_commit(toolset, fixture_repo):
    out = toolset.execute("git_blame", {
        "repo": "app", "file_path": "src/handler.py", "line_start": 3, "line_end": 3,
    })
    assert out.status == ToolStatus.SUCCESS, out.error
    assert fixture_repo["breaking_sha"][:8] in str(out.output)
    assert "show_manual_screen" in str(out.output)


def test_git_log_around_incident_window(toolset):
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    out = toolset.execute("git_log_around", {
        "repo": "app", "timestamp": now_iso, "window_hours": 1,
    })
    assert out.status == ToolStatus.SUCCESS, out.error
    # breaking commit (now-ish) in window; initial commit (-2d) outside it
    assert "manual OA screen" in str(out.output)
    assert "initial: auto-accept" not in str(out.output)


def test_deploy_diff_shows_the_change(toolset, fixture_repo):
    out = toolset.execute("deploy_diff", {
        "repo": "app", "ref": fixture_repo["breaking_sha"],
    })
    assert out.status == ToolStatus.SUCCESS, out.error
    body = str(out.output)
    assert "show_manual_screen" in body          # the added line
    assert "manual OA screen" in body            # commit subject header
    assert "-    return order.accept()" in body  # the removed line


def test_deploy_diff_rejects_funny_refs(toolset):
    out = toolset.execute("deploy_diff", {"repo": "app", "ref": "HEAD; rm -rf /"})
    assert out.status == ToolStatus.ERROR


def test_code_search(toolset):
    out = toolset.execute("code_search", {"repo": "app", "pattern": "show_manual_screen"})
    assert out.status == ToolStatus.SUCCESS, out.error
    assert "handler.py" in str(out.output)

    out = toolset.execute("code_search", {"repo": "app", "pattern": "zzz_no_match_zzz"})
    assert out.status == ToolStatus.NO_DATA


def test_stacktrace_to_source(toolset, fixture_repo):
    out = toolset.execute("stacktrace_to_source", {
        "repo": "app", "file_path": "src/handler.py", "line": 3, "context": 2,
    })
    assert out.status == ToolStatus.SUCCESS, out.error
    body = str(out.output)
    assert ">>>" in body and "show_manual_screen" in body
    assert fixture_repo["breaking_sha"][:8] in body  # blame included

    # suffix matching for build-path style frames
    out = toolset.execute("stacktrace_to_source", {
        "repo": "app", "file_path": "/builds/x/y/handler.py", "line": 1,
    })
    assert out.status == ToolStatus.SUCCESS, out.error


def test_path_traversal_blocked(toolset):
    out = toolset.execute("git_blame", {"repo": "app", "file_path": "../../etc/passwd"})
    assert out.status == ToolStatus.ERROR
    assert "escapes repo root" in out.error


def test_write_git_ops_refused(toolset):
    with pytest.raises(ValueError):
        toolset._git("app", "push", "origin", "main")
    with pytest.raises(ValueError):
        toolset._git("app", "commit", "-m", "nope")
