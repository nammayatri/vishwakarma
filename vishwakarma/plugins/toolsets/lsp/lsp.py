"""
LSP code-intelligence toolset — IDE-grade navigation for the agent.

find_definition / find_references / hover via a language server (HLS for the
Haskell backend, pylsp for Python, …). Far better than grep for tracing a bug
through typed code: "where is getDriverOffers defined → who calls it → what's
its type".

Config (config.yaml):
  toolsets:
    lsp:
      enabled: true
      config:
        command: ["haskell-language-server-wrapper", "lsp"]   # the server
        root: /data/repos/backend
        language_id: haskell
"""
import logging

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class LSPToolset(Toolset):
    name = "lsp"
    description = (
        "Semantic code navigation via a language server: find where a symbol "
        "is defined, find all references/callers, and get type/signature info. "
        "Use to trace how a function flows through the codebase — more precise "
        "than text search in a typed language."
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = config or {}
        self.command = cfg.get("command", [])
        self.root = cfg.get("root", "")
        self.language_id = cfg.get("language_id", "")
        self._client = None

    def check_prerequisites(self) -> tuple[bool, str]:
        import shutil
        if not self.command:
            return False, "no LSP command configured (toolsets.lsp.config.command)"
        if shutil.which(self.command[0]) is None:
            return False, f"LSP server not found: {self.command[0]}"
        if not self.root:
            return False, "no root configured"
        return True, ""

    def _get_client(self):
        from vishwakarma.core.lsp_client import LSPClient
        if self._client is None or not self._client.alive:
            self._client = LSPClient(self.command, self.root, self.language_id)
            self._client.start()
        return self._client

    def get_tools(self) -> list[ToolDef]:
        pos = {
            "file": {"type": "string", "description": "Path (relative to the LSP root or absolute)"},
            "line": {"type": "integer", "description": "0-based line of the symbol"},
            "character": {"type": "integer", "description": "0-based column of the symbol"},
        }
        return [
            ToolDef(name="find_definition",
                    description="Where is the symbol at this position defined?",
                    parameters={"type": "object", "properties": pos,
                                "required": ["file", "line", "character"]}),
            ToolDef(name="find_references",
                    description="All references/callers of the symbol at this position.",
                    parameters={"type": "object", "properties": pos,
                                "required": ["file", "line", "character"]}),
            ToolDef(name="hover",
                    description="Type/signature/docs for the symbol at this position.",
                    parameters={"type": "object", "properties": pos,
                                "required": ["file", "line", "character"]}),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        from pathlib import Path
        f = params.get("file", "")
        line = int(params.get("line", 0))
        char = int(params.get("character", 0))
        path = f if Path(f).is_absolute() else str(Path(self.root) / f)
        if not Path(path).exists():
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR,
                              error=f"file not found: {path}")
        try:
            client = self._get_client()
            if tool_name == "find_definition":
                locs = client.definition(path, line, char)
                return self._loc_output(tool_name, locs, "definition")
            if tool_name == "find_references":
                locs = client.references(path, line, char)
                return self._loc_output(tool_name, locs, "reference")
            if tool_name == "hover":
                info = client.hover(path, line, char)
                if not info:
                    return ToolOutput(tool_name=tool_name, status=ToolStatus.NO_DATA,
                                      invocation=f"hover({f}:{line})")
                return ToolOutput(tool_name=tool_name, status=ToolStatus.SUCCESS,
                                  output=info, invocation=f"hover({f}:{line})")
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR,
                              error=f"Unknown tool: {tool_name}")
        except Exception as e:
            err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR, error=err)

    @staticmethod
    def _loc_output(tool_name: str, locs: list[dict], kind: str) -> ToolOutput:
        if not locs:
            return ToolOutput(tool_name=tool_name, status=ToolStatus.NO_DATA,
                              invocation=tool_name)
        lines = [f"{_short_uri(l['uri'])}:{l['line'] + 1}:{l['character'] + 1}" for l in locs]
        return ToolOutput(tool_name=tool_name, status=ToolStatus.SUCCESS,
                          output=f"{len(locs)} {kind}(s):\n" + "\n".join(lines[:50]),
                          invocation=tool_name)


def _short_uri(uri: str) -> str:
    return uri.replace("file://", "") if uri else uri
