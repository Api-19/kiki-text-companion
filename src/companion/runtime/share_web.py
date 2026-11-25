"""Loopback-only HTTP plumbing shared by the local share pages."""

from __future__ import annotations

import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_MAX_BODY = 8_192
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class LoopbackServer(HTTPServer):
    """A single-threaded server that only accepts its own token and origin."""

    timeout = 0.25

    def __init__(
        self, page, *, assets_dir: Path, static: dict[str, tuple[str, str]],
        token_header: str,
    ) -> None:
        self.assets = {
            route: ((assets_dir / filename).read_bytes(), content_type)
            for route, (filename, content_type) in static.items()
        }
        self.page = page
        self.token_header = token_header
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), _Handler)
        self.host_header = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.host_header}"

    def server_bind(self) -> None:
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = self.server_address

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address

    def handle_error(self, request, client_address):
        if not isinstance(sys.exception(), OSError):
            self.page.closed = True
            print("本机网页发生错误。")


class _Handler(BaseHTTPRequestHandler):
    server: LoopbackServer

    def log_message(self, format, *args):
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_error(self, code, message=None, explain=None) -> None:
        self._json(code, {"message": "本机网页请求无效。"}, write_body=self.command != "HEAD")

    def _send(self, status: int, payload: bytes, content_type: str, *, write_body=True) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if write_body:
            self.wfile.write(payload)

    def _json(self, status: int, body: dict, *, write_body=True) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8", write_body=write_body)

    def _host_ok(self) -> bool:
        return self.headers.get_all("Host") == [self.server.host_header]

    def do_GET(self) -> None:
        if not self._host_ok():
            self.send_error(403)
        elif self.path in self.server.assets:
            payload, content_type = self.server.assets[self.path]
            self._send(200, payload, content_type)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        tokens = self.headers.get_all(self.server.token_header, [])
        if (
            not self._host_ok()
            or self.headers.get_all("Origin") != [self.server.origin]
            or len(tokens) != 1 or not tokens[0].isascii()
            or not secrets.compare_digest(tokens[0], self.server.token)
        ):
            self.send_error(403)
            return

        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get_all("Content-Type") != ["application/json"]
            or "Transfer-Encoding" in self.headers
            or len(lengths) != 1
            or not lengths[0].isascii() or not lengths[0].isdecimal() or len(lengths[0]) > 4
            or not 0 < int(lengths[0]) <= _MAX_BODY
        ):
            self.send_error(400)
            return
        try:
            data = json.loads(self.rfile.read(int(lengths[0])))
            status, body = self.server.page.handle(self.path, data)
        except (ValueError, RecursionError):
            self.send_error(400)
            return
        if status == 404:
            self.send_error(404)
            return
        self._json(status, body)
