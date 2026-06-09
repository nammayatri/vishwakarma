"""
Minimal LSP (Language Server Protocol) client over stdio.

Gives the agent IDE-grade code navigation — go-to-definition, find-references,
hover (type info) — which is far better than grep for tracing a bug through a
typed codebase (HLS for the Haskell backend, pylsp for Python, etc.).

Speaks JSON-RPC with Content-Length framing, does the initialize handshake,
opens files on demand, and matches responses to requests by id. Designed to
be driven by the lsp toolset and tested against a fake server — no hard
dependency on any specific language server at import time.
"""
import json
import logging
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30


class LSPClient:
    def __init__(self, command: list[str], root: str, language_id: str = ""):
        self.command = command
        self.root = str(Path(root).resolve())
        self.language_id = language_id
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._opened: set[str] = set()
        self._initialized = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.root,
        )
        root_uri = Path(self.root).as_uri()
        self._request("initialize", {
            "processId": None, "rootUri": root_uri,
            "capabilities": {"textDocument": {
                "definition": {}, "references": {}, "hover": {}}},
        })
        self._notify("initialized", {})
        self._initialized = True

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
        self._initialized = False
        self._opened.clear()

    @property
    def alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    # ── Queries ───────────────────────────────────────────────────────────────

    def _ensure_open(self, file_path: str) -> str:
        uri = Path(file_path).resolve().as_uri()
        if uri not in self._opened:
            text = Path(file_path).read_text(errors="replace")
            self._notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": self.language_id or "plaintext",
                "version": 1, "text": text}})
            self._opened.add(uri)
        return uri

    def definition(self, file_path: str, line: int, character: int) -> list[dict]:
        uri = self._ensure_open(file_path)
        res = self._request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character}})
        return _as_locations(res)

    def references(self, file_path: str, line: int, character: int) -> list[dict]:
        uri = self._ensure_open(file_path)
        res = self._request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True}})
        return _as_locations(res)

    def hover(self, file_path: str, line: int, character: int) -> str:
        uri = self._ensure_open(file_path)
        res = self._request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character}})
        if not res:
            return ""
        contents = res.get("contents")
        if isinstance(contents, dict):
            return contents.get("value", "")
        if isinstance(contents, list):
            return "\n".join(c.get("value", str(c)) if isinstance(c, dict) else str(c)
                             for c in contents)
        return str(contents or "")

    # ── JSON-RPC framing ──────────────────────────────────────────────────────

    def _request(self, method: str, params: dict) -> dict | list | None:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            # Read messages until we get the matching response (skip
            # notifications/server requests).
            import time
            deadline = time.time() + REQUEST_TIMEOUT
            while time.time() < deadline:
                msg = self._read()
                if msg is None:
                    raise RuntimeError(f"LSP server closed during {method}")
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise RuntimeError(f"LSP {method} error: {msg['error']}")
                    return msg.get("result")
                # server→client request: reply with null so it doesn't block
                if "id" in msg and "method" in msg:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            raise TimeoutError(f"LSP {method} timed out")

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        header = f"Content-Length: {len(data)}\r\n\r\n".encode()
        self._proc.stdin.write(header + data)
        self._proc.stdin.flush()

    def _read(self) -> dict | None:
        # headers
        length = 0
        while True:
            line = self._proc.stdout.readline()
            if not line:
                return None
            line = line.decode(errors="replace").strip()
            if line == "":
                break
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        if length <= 0:
            return {}
        body = self._proc.stdout.read(length)
        return json.loads(body.decode(errors="replace"))


def _as_locations(result) -> list[dict]:
    """Normalize Location | Location[] | LocationLink[] → [{uri, line, character}]."""
    if not result:
        return []
    items = result if isinstance(result, list) else [result]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        uri = it.get("uri") or it.get("targetUri", "")
        rng = it.get("range") or it.get("targetRange") or {}
        start = rng.get("start", {})
        out.append({"uri": uri, "line": start.get("line", 0),
                    "character": start.get("character", 0)})
    return out
