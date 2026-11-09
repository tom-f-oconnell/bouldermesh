"""The admin page: where the site's compute runs, credentials for it, and what the main site shows.

Served by its own small server, on ADMIN_PORT, which is never reachable from the main site's port, and
which listens only on loopback or a private (LAN) address: ADMIN_HOST, by default 127.0.0.1. In Docker
it listens on the container's own network, and docker-compose.yml publishes it only on ADMIN_BIND, the
host's loopback or LAN address. A public address is refused, for ADMIN_BIND too.

Anyone who can reach it can change settings (credentials are write-only: they're never sent back).
ADMIN_PASSWORD adds a password. As a page on the LAN could be visited while browsing elsewhere, changes
must be sent as JSON from the page itself: other sites' forms and scripts are refused.
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

PAGE = Path(__file__).resolve().parents[1] / "frontend" / "admin.html"


def allowed_host(host: str, in_docker: bool) -> bool:
    """Whether the admin page may listen on this address: loopback or private, or every address of a
    container, which Docker then publishes on one address of the host."""
    if host in ("0.0.0.0", "::"):
        return in_docker
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    return ip.is_loopback or ip.is_private


class Context:
    """What the admin page can see and do, given by the server."""

    def __init__(self, settings, state: Callable[[], dict], apply: Callable[[], None],
                 actions: dict[str, Callable[[dict], dict]]):
        self.settings, self.state, self.apply, self.actions = settings, state, apply, actions


def handler_for(ctx: Context, password: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BoulderMeshAdmin"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _authorised(self) -> bool:
            if not password:
                return True
            got = self.headers.get("Authorization", "")
            want = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
            if hmac.compare_digest(got.encode(), want.encode()):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="BoulderMesh admin"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _same_site(self) -> bool:
            """Refuse requests another site's page made: by their Origin, and by a Host that isn't one
            this server answers to (DNS rebinding)."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if not (_is_ip(host) or _lan_name(host)):
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin.split("//", 1)[-1] == self.headers.get("Host")

        def do_GET(self):
            if not self._authorised():
                return
            if not self._same_site():
                return self.json({"error": "refused"}, 403)
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/admin/state":
                return self.json(ctx.state())
            self.json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._authorised():
                return
            if not self._same_site() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.json({"error": "refused: send JSON from the admin page"}, 403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self.json({"error": "not JSON"}, 400)
            try:
                if self.path == "/api/admin/settings":
                    ctx.settings.update(body)
                    ctx.apply()
                    return self.json(ctx.state())
                if self.path == "/api/admin/secrets":
                    ctx.settings.set_secrets(body)
                    ctx.apply()
                    return self.json(ctx.state())
                name = self.path.removeprefix("/api/admin/actions/")
                if name in ctx.actions:
                    return self.json(ctx.actions[name](body))
            except (ValueError, RuntimeError) as e:
                return self.json({"error": str(e)}, 400)
            except Exception as e:
                return self.json({"error": f"{type(e).__name__}: {e}"}, 500)
            self.json({"error": "not found"}, 404)

    return Handler


def _lan_name(host: str) -> bool:
    """A name only a LAN would resolve, which a rebinding attack (a public domain of its own) can't be."""
    extra = {h.strip().lower() for h in os.environ.get("ADMIN_HOSTNAMES", "").split(",") if h.strip()}
    return host == "localhost" or "." not in host or host.endswith((".local", ".lan", ".home.arpa")) or host in extra


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def refusal(host: str, published: str | None, in_docker: bool) -> str | None:
    """Why the admin page mustn't be served: the address it would listen on, or in Docker the host's address it would
    be published on (ADMIN_BIND, which Compose passes in), is public. None if it may."""
    if not allowed_host(host, in_docker):
        return f"{host} isn't a loopback or private address"
    if in_docker and published and not allowed_host(published, False):
        return f"ADMIN_BIND={published} would publish it on a public address"
    return None


def start(ctx: Context, host: str, port: int) -> ThreadingHTTPServer | None:
    """Serve the admin page in the background, or refuse to on a public address."""
    in_docker = Path("/.dockerenv").exists()
    why = refusal(host, os.environ.get("ADMIN_BIND"), in_docker)
    if why:
        print(f"admin page NOT started: {why}", flush=True)
        return None
    try:
        httpd = ThreadingHTTPServer((host, port), handler_for(ctx, os.environ.get("ADMIN_PASSWORD") or None))
    except OSError as e:    # the site carries on without it
        print(f"admin page NOT started on {host}:{port}: {e}", flush=True)
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"admin page on http://{host}:{port}" + (" (published by Docker on ADMIN_BIND)" if in_docker else ""), flush=True)
    return httpd
