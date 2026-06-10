"""
Fake GitHub REST endpoint for PR-creation tests.

  GET  /repos/{o}/{r}/pulls?head=...   → [] or [existing pr]
  POST /repos/{o}/{r}/pulls            → {html_url, number, draft}

State is in-process: a created PR is remembered so the idempotency GET finds
it. Run as: python fake_github.py --port N
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

_PRS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        from urllib.parse import unquote
        m = re.match(r"/repos/([^/]+)/([^/]+)/pulls", self.path)
        if m:
            head = ""
            if "head=" in self.path:
                head = unquote(self.path.split("head=")[1].split("&")[0])
            matching = [p for p in _PRS if p["_head_q"] == head] if head else _PRS
            return self._send(200, matching)
        return self._send(404, {})

    def do_POST(self):
        m = re.match(r"/repos/([^/]+)/([^/]+)/pulls", self.path)
        if not m:
            return self._send(404, {})
        owner, repo = m.group(1), m.group(2)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if not body.get("draft"):
            return self._send(422, {"message": "expected draft=true"})
        num = len(_PRS) + 1
        pr = {
            "html_url": f"https://github.com/{owner}/{repo}/pull/{num}",
            "number": num, "draft": True,
            "_head_q": f"{owner}:{body.get('head')}",
        }
        _PRS.append(pr)
        return self._send(201, pr)


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
