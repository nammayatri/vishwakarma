"""
Minimal fake LSP server for testing the client — speaks Content-Length-framed
JSON-RPC on stdio. Responds to initialize / definition / references / hover
with canned results so the client's protocol handling is verified end to end.
"""
import json
import sys


def _read(stdin):
    length = 0
    while True:
        line = stdin.readline()
        if not line:
            return None
        s = line.decode(errors="replace").strip()
        if s == "":
            break
        if s.lower().startswith("content-length:"):
            length = int(s.split(":", 1)[1])
    body = stdin.read(length)
    return json.loads(body.decode())


def _send(stdout, obj):
    data = json.dumps(obj).encode()
    stdout.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
    stdout.write(data)
    stdout.flush()


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        msg = _read(stdin)
        if msg is None:
            return
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            _send(stdout, {"jsonrpc": "2.0", "id": mid,
                           "result": {"capabilities": {"definitionProvider": True,
                                                       "referencesProvider": True,
                                                       "hoverProvider": True}}})
        elif method in ("initialized", "textDocument/didOpen"):
            continue   # notifications
        elif method == "textDocument/definition":
            uri = msg["params"]["textDocument"]["uri"]
            _send(stdout, {"jsonrpc": "2.0", "id": mid, "result": {
                "uri": uri,
                "range": {"start": {"line": 2, "character": 4},
                          "end": {"line": 2, "character": 10}}}})
        elif method == "textDocument/references":
            uri = msg["params"]["textDocument"]["uri"]
            _send(stdout, {"jsonrpc": "2.0", "id": mid, "result": [
                {"uri": uri, "range": {"start": {"line": 5, "character": 0},
                                       "end": {"line": 5, "character": 3}}},
                {"uri": uri, "range": {"start": {"line": 9, "character": 8},
                                       "end": {"line": 9, "character": 11}}}]})
        elif method == "textDocument/hover":
            _send(stdout, {"jsonrpc": "2.0", "id": mid, "result": {
                "contents": {"kind": "markdown", "value": "accept_order :: Order -> IO ()"}}})
        elif mid is not None:
            _send(stdout, {"jsonrpc": "2.0", "id": mid, "result": None})


if __name__ == "__main__":
    main()
