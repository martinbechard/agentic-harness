"""Read-only loopback server adapted from the existing backlog dashboard boundary."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = {
    "/": ("index.html", "text/html"),
    "/dashboard.js": ("dashboard.js", "text/javascript"),
    "/dashboard.css": ("dashboard.css", "text/css"),
}


def create_dashboard_server(collector, host="127.0.0.1", port=0):
    if host != "127.0.0.1":
        raise ValueError("Dashboard must bind to loopback")
    assets = Path(__file__).parent / "static"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            hosts = self.headers.get_all("Host", [])
            allowed = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            origins = self.headers.get_all("Origin", [])
            if (
                len(hosts) != 1
                or hosts[0] not in allowed
                or (origins and origins != ["http://" + hosts[0]])
                or self.headers.get("Sec-Fetch-Site", "none") not in {"none", "same-origin"}
            ):
                return self.reply(403, b"{}")
            target = urlsplit(self.path)
            if target.query or target.scheme or target.netloc:
                return self.reply(404, b"{}")
            if target.path == "/api/snapshot":
                try:
                    body = json.dumps(collector(), allow_nan=False).encode()
                except (OSError, ValueError, RuntimeError):
                    return self.reply(503, b'{"error":"Evidence unavailable"}')
                return self.reply(200, body)
            if target.path == "/api/health":
                return self.reply(200, b'{"read_only":true}')
            if target.path in ASSETS:
                name, kind = ASSETS[target.path]
                return self.reply(200, (assets / name).read_bytes(), kind)
            self.reply(404, b"{}")

        do_HEAD = do_GET

        def no_mutation(self):
            self.reply(405, b'{"error":"Read-only dashboard"}')

        do_POST = do_PUT = do_PATCH = do_DELETE = no_mutation

    return ThreadingHTTPServer((host, port), Handler)
