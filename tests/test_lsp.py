"""
LSP client + toolset tests against a fake language server (verifies the
JSON-RPC framing, handshake, didOpen, and definition/references/hover).

Run:  pytest tests/test_lsp.py -v
"""
import stat
import sys
from pathlib import Path

import pytest

from vishwakarma.core.models import ToolStatus

FAKE = Path(__file__).parent / "fake_lsp.py"


@pytest.fixture()
def fake_cmd(tmp_path):
    wrapper = tmp_path / "fakelsp"
    wrapper.write_text(f"#!/bin/bash\nexec {sys.executable} {FAKE}\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


@pytest.fixture()
def root(tmp_path):
    (tmp_path / "h.py").write_text("def accept_order(o):\n    return o\n\naccept_order(1)\n")
    return str(tmp_path)


def test_client_definition_references_hover(fake_cmd, root):
    from vishwakarma.core.lsp_client import LSPClient
    c = LSPClient([fake_cmd], root, "python")
    c.start()
    try:
        d = c.definition(str(Path(root) / "h.py"), 3, 0)
        assert d and d[0]["line"] == 2 and d[0]["character"] == 4
        r = c.references(str(Path(root) / "h.py"), 0, 4)
        assert len(r) == 2
        h = c.hover(str(Path(root) / "h.py"), 0, 4)
        assert "accept_order" in h
    finally:
        c.stop()


def test_toolset(fake_cmd, root):
    from vishwakarma.plugins.toolsets.lsp.lsp import LSPToolset
    ts = LSPToolset({"command": [fake_cmd], "root": root, "language_id": "python"})
    ok, _ = ts.check_prerequisites()
    assert ok

    out = ts.execute("find_definition", {"file": "h.py", "line": 3, "character": 0})
    assert out.status == ToolStatus.SUCCESS and "h.py:3:5" in str(out.output)

    out = ts.execute("find_references", {"file": "h.py", "line": 0, "character": 4})
    assert out.status == ToolStatus.SUCCESS and "2 reference(s)" in str(out.output)

    out = ts.execute("hover", {"file": "h.py", "line": 0, "character": 4})
    assert out.status == ToolStatus.SUCCESS and "accept_order ::" in str(out.output)

    out = ts.execute("find_definition", {"file": "nope.py", "line": 0, "character": 0})
    assert out.status == ToolStatus.ERROR


def test_prereq_fails_without_command():
    from vishwakarma.plugins.toolsets.lsp.lsp import LSPToolset
    ok, msg = LSPToolset({}).check_prerequisites()
    assert not ok and "command" in msg


def test_registered():
    from vishwakarma.core.toolset_manager import _PYTHON_TOOLSET_REGISTRY
    assert "lsp" in _PYTHON_TOOLSET_REGISTRY
