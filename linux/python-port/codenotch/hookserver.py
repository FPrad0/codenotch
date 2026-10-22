"""Local event server for codenotch-hook: POST /event?e=<event>&ppid=<pid>, body = the Claude
Code hook's stdin JSON (ported from server.rs). Loopback only, POST only, and browser requests
from other origins are refused so a web page cannot forge session state.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import config
from .sessions import HookEvent

MAX_BODY = 256 * 1024


def is_allowed_origin(origin: str) -> bool:
    s = (origin or "").strip()
    if not s or s.lower() == "null":
        return False
    if s.startswith("http://"):
        rest = s[7:]
    elif s.startswith("https://"):
        rest = s[8:]
    elif "://" in s:
        return False
    else:
        rest = s
    if any(c in rest for c in "@/\\?#"):
        return False
    if rest.startswith("["):
        end = rest.find("]")
        if end < 0:
            return False
        host, after = rest[:end + 1], rest[end + 1:]
        if after and not after.startswith(":"):
            return False
        port = after[1:] if after else None
    else:
        host, _, port = rest.partition(":")
        port = port if ":" in rest else None
    if port is not None:
        if not re.fullmatch(r"\d{1,5}", port) or not (0 < int(port) <= 65535):
            return False
    return host.lower() in ("127.0.0.1", "localhost") or host == "[::1]"


def is_forbidden(headers) -> bool:
    origins = headers.get_all("Origin") or []
    if len(origins) > 1:
        return True
    if origins and not is_allowed_origin(origins[0]):
        return True
    site = headers.get("Sec-Fetch-Site")
    return bool(site and site.strip().lower() == "cross-site")


def parse_event(path: str, body: str) -> HookEvent:
    q = parse_qs(urlsplit(path).query)
    try:
        v = json.loads(body) if body else {}
    except ValueError:
        v = {}
    if not isinstance(v, dict):
        v = {}

    def s(k):
        x = v.get(k)
        return x if isinstance(x, str) else ""

    tool_input = v.get("tool_input") if isinstance(v.get("tool_input"), dict) else {}
    cmd = tool_input.get("command")
    try:
        ppid = int((q.get("ppid") or ["0"])[0])
    except ValueError:
        ppid = 0
    return HookEvent(
        e=(q.get("e") or [""])[0],
        session_id=s("session_id") or "unknown",
        ppid=ppid, cwd=s("cwd"), prompt=s("prompt"), message=s("message"),
        tool_name=s("tool_name"), tool_cmd=cmd if isinstance(cmd, str) else "",
        model=s("model"), src="hook",
    )


def start(port: int, on_event) -> bool:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _reply(self, code: int, text: str):
            data = text.encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the hook hangs up as soon as it knows the event arrived

        def do_GET(self):
            if self.path.startswith("/event"):
                return self._reply(405, "method not allowed")
            self._reply(200, "ok")

        def do_POST(self):
            if not self.path.startswith("/event"):
                return self._reply(200, "ok")
            if is_forbidden(self.headers):
                return self._reply(403, "forbidden")
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            except ValueError:
                length = 0
            body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
            try:
                on_event(parse_event(self.path, body))
            except Exception as e:  # a bad event must never take the server down
                config.log(f"hookserver: {e}")
            self._reply(200, "ok")

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            config.log(f"hookserver: request from {client_address} failed")

    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError as e:
        config.log(f"hookserver: cannot bind 127.0.0.1:{port} ({e}) — is another instance running?")
        return False
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="hookserver", daemon=True).start()
    return True
