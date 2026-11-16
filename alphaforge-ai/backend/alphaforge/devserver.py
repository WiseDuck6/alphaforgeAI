"""Local development server (standard library only).

    python -m alphaforge.devserver            # http://127.0.0.1:8000
The Vite dev server proxies /api to this port.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

os.environ.setdefault("ALPHAFORGE_AUTH_MODE", "header")

from .api import routes  # noqa: E402,F401
from .api.router import NO_CACHE_HEADERS, HttpError, Request, dispatch, parse_body  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


class Handler(BaseHTTPRequestHandler):
    def _handle(self, method: str):
        url = urlsplit(self.path)
        length = int(self.headers.get("content-length") or 0)
        try:
            body = parse_body(self.rfile.read(length) if length else None)
            req = Request(method=method, path=url.path, query=dict(parse_qsl(url.query)),
                          headers={k.lower(): v for k, v in self.headers.items()}, body=body)
            status, payload = dispatch(req)
        except HttpError as e:
            status, payload = e.status, {"error": e.message}
        data = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        # Responses depend on the caller (X-User-Id / Authorization), not only the URL, so no
        # cache between the browser and this server (code-server proxy, CDN) may store or reuse them.
        for name, value in NO_CACHE_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def log_message(self, fmt, *args):
        headers = getattr(self, "headers", None)
        user = (headers.get("x-user-id") if headers else None) or "-"
        logging.getLogger("alphaforge.http").info("%s user=%s %s", self.address_string(), user, fmt % args)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"AlphaForge API on http://127.0.0.1:{port}  (auth mode: {os.environ['ALPHAFORGE_AUTH_MODE']})")
    server.serve_forever()


if __name__ == "__main__":
    main()
