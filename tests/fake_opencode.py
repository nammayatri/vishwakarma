"""
Fake `opencode serve` for tests — mimics the endpoints the CodeAgent adapter
uses, runs in the cwd it's spawned in (like the real one).

Behaviors keyed off the message text:
  "EDIT: <content>"  → writes fix.txt in cwd (simulates the coding agent
                       making a change in the worktree)
  "FAIL"             → returns an OpenCode-style error in info.error
  anything else      → echoes back "did: <text>"

Records the last-used agent name in .last_agent (cwd) so tests can assert
read→plan / edit→build.
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/global/health":
            return self._send(200, {"status": "ok"})
        return self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/session":
            return self._send(200, {"id": "ses_fake_1", "title": body.get("title", "")})

        m = re.match(r"^/session/([^/]+)/message$", self.path)
        if m:
            text = ""
            for p in body.get("parts", []):
                if p.get("type") == "text":
                    text = p.get("text", "")
            Path(".last_agent").write_text(body.get("agent", ""))

            if "FAIL" in text:
                return self._send(200, {
                    "info": {"error": {"name": "APIError",
                                       "data": {"message": "simulated provider failure"}}},
                    "parts": [],
                })
            if text.startswith("EDIT:"):
                Path("fix.txt").write_text(text[len("EDIT:"):].strip() + "\n")
            return self._send(200, {
                "info": {"tokens": {"output": 5}},
                "parts": [{"type": "text", "text": f"did: {text[:80]}"}],
            })
        return self._send(404, {})


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
