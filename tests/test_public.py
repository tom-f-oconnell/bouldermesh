#!/usr/bin/env python3
"""What the site does when exposed: the editing token and read-only modes, refusing other sites' pages, the page's
Content-Security-Policy and the other headers, files served as what they are or as downloads, 304s, bad lengths, and
the fetcher's refusal of private addresses. Starts the server three times on free ports; offline.

    python3 tests/test_public.py
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from climbs import common  # noqa: E402

TOKEN = "an-editing-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """The server on a free port with its own data directory, stopped as Docker would stop it."""

    def __init__(self, tmp: Path, **env: str):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        full = dict(os.environ, DATA_DIR=str(tmp / "data"), PORT=str(self.port), HOST="127.0.0.1", ADMIN_PORT=str(free_port()),
                    HTTP_MIN_INTERVAL="0", STOP_WAIT_MINUTES="0.1", **env)
        for k in ("EDIT_TOKEN", "READ_ONLY", "CSP"):
            if k not in env:
                full.pop(k, None)
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "backend" / "server.py")], env=full,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            try:
                urllib.request.urlopen(self.url + "/api/status", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("the server didn't start: " + self.proc.communicate(timeout=5)[0][-2000:])

    def stop(self) -> str:
        self.proc.terminate()
        try:
            out = self.proc.communicate(timeout=30)[0]
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out = self.proc.communicate()[0]
        assert "Traceback" not in out, out[-3000:]
        return out


def req(B: str, method: str, path: str, body: dict | None = None, headers: dict | None = None, files=None):
    """(status, headers, decoded JSON or bytes)."""
    data, hdrs = None, dict(headers or {})
    if body is not None:
        data, hdrs["Content-Type"] = json.dumps(body).encode(), "application/json"
    if files is not None:
        bnd, buf = uuid.uuid4().hex, io.BytesIO()
        for name, fname, blob in files:
            buf.write(f'--{bnd}\r\nContent-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'
                      f"Content-Type: application/octet-stream\r\n\r\n".encode() + blob + b"\r\n")
        buf.write(f"--{bnd}--\r\n".encode())
        data, hdrs["Content-Type"] = buf.getvalue(), f"multipart/form-data; boundary={bnd}"
    try:
        with urllib.request.urlopen(urllib.request.Request(B + path, data=data, headers=hdrs, method=method)) as r:
            raw, status, rh = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, rh = e.read(), e.code, e.headers
    if rh.get("Content-Type", "").startswith("application/json"):
        return status, rh, json.loads(raw or b"{}")
    return status, rh, raw


def raw_request(port: int, text: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(text)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    return out


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PLY = b"ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n"


def with_token(tmp: Path) -> None:
    """EDIT_TOKEN set, READ_ONLY=auto as the public setup has it: a token site."""
    srv = Server(tmp, EDIT_TOKEN=TOKEN, READ_ONLY="auto")
    B, port = srv.url, srv.port
    try:
        # the page, with its policy: a hash for each inline script, nothing framed, and the other headers
        st, h, page = req(B, "GET", "/")
        assert st == 200 and h["Content-Type"].startswith("text/html") and h["Cache-Control"] == "no-cache", (st, h)
        csp = h["Content-Security-Policy"]
        inline = re.findall(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", (ROOT / "frontend" / "index.html").read_bytes(), re.S)
        want = {f"'sha256-{base64.b64encode(hashlib.sha256(m).digest()).decode()}'" for m in inline}
        assert len(inline) == 2 and want <= set(re.findall(r"'sha256-[^']+'", csp)), (want, csp)
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0], csp
        assert b'onclick="' not in page, "an inline handler, which the policy would block"
        assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY" and h["Referrer-Policy"], h
        assert "Python" not in h.get("Server", ""), h.get("Server")
        since = h["Last-Modified"]
        st, h, _ = req(B, "GET", "/", headers={"If-Modified-Since": since})
        assert st == 304, st
        st, h, s = req(B, "GET", "/api/status")
        assert st == 200 and s["editing"] == "token" and s["editor"] is False and h["Cache-Control"] == "no-cache", s
        assert "Content-Security-Policy" not in h, "the policy is for the page"
        st, _, s = req(B, "GET", "/api/status", headers=AUTH)
        assert s["editor"] is True, s

        # changes: refused without the token, before the body is read; with it, as before; never from another site's page
        st, h, e = req(B, "POST", "/api/projects", {"name": "x"})
        assert st == 401 and "token" in e["error"] and h["WWW-Authenticate"].startswith("Bearer"), (st, e, h)
        st, _, e = req(B, "POST", "/api/projects", {"name": "x"}, headers={"Authorization": "Bearer wrong"})
        assert st == 401, (st, e)
        st, _, e = req(B, "POST", "/api/projects", {"name": "x"}, headers={"Authorization": f"Bearer {TOKEN} "})
        assert st == 401, (st, e)
        st, _, e = req(B, "PATCH", "/api/climbs/abc", {"name": "x"})
        assert st == 401, (st, e)
        st, _, e = req(B, "DELETE", "/api/climbs/abc")
        assert st == 401, (st, e)
        st, _, e = req(B, "POST", "/api/projects", {"name": "x"}, headers={**AUTH, "Origin": "https://evil.example"})
        assert st == 403 and "another site" in e["error"], (st, e)
        st, _, e = req(B, "POST", "/api/projects", {"name": "x"}, headers={**AUTH, "Sec-Fetch-Site": "cross-site"})
        assert st == 403, (st, e)
        st, _, p = req(B, "POST", "/api/projects", {"name": "same site"}, headers={**AUTH, "Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"})
        assert st == 201, (st, p)
        # behind a proxy that names the site in X-Forwarded-Host
        st, _, p = req(B, "POST", "/api/projects", {"name": "proxied"}, headers={**AUTH, "Origin": "https://rocks.example", "X-Forwarded-Host": "rocks.example"})
        assert st == 201, (st, p)
        st, _, p = req(B, "POST", "/api/projects", {"name": "plain"}, headers=AUTH)
        assert st == 201, (st, p)
        assert len(req(B, "GET", "/api/projects")[2]) == 3

        # what's the client's fault is a 400, not a 500
        st, _, e = req(B, "POST", "/api/climbs/import", {"url": "ftp://x"}, headers=AUTH)
        assert st == 400 and "unsupported" in e["error"], (st, e)
        out = raw_request(port, f"POST /api/projects HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {TOKEN}\r\n"
                                f"Content-Type: application/json\r\nContent-Length: -5\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.0 400") and b"negative" in out, out[:300]
        out = raw_request(port, f"POST /api/projects HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 99999999999\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.0 401"), out[:300]     # refused without reading any of it

        # an uploaded mesh: the mesh and its textures are kept, nothing that could be a page; what's served and how
        st, _, up = req(B, "POST", "/api/meshes", headers=AUTH, files=[("files", "cube.ply", PLY), ("files", "evil.html", b"<script>1</script>"),
                                                                       ("files", "tex.png", PNG), ("files", "run.svg", b"<svg onload=alert(1)>")])
        assert st == 201 and up["filename"] == "cube.ply" and sorted(up["skipped"]) == ["evil.html", "run.svg"], up
        mid = up["id"]
        assert req(B, "GET", f"/meshes/{mid}/evil.html")[0] == 404
        st, h, body = req(B, "GET", f"/meshes/{mid}/tex.png")
        assert st == 200 and h["Content-Type"] == "image/png" and body == PNG and h["Cache-Control"].startswith("public") and "Content-Disposition" not in h, h
        assert req(B, "GET", f"/meshes/{mid}/tex.png", headers={"If-Modified-Since": h["Last-Modified"]})[0] == 304
        st, h, _ = req(B, "GET", f"/meshes/{mid}/cube.ply")
        assert st == 200 and h["Content-Type"] == "application/octet-stream" and "Content-Disposition" not in h, h
        (tmp / "data" / "meshes" / mid / "note.txt").write_text("hello")     # something else in the folder downloads as bytes
        st, h, _ = req(B, "GET", f"/meshes/{mid}/note.txt")
        assert st == 200 and h["Content-Type"] == "application/octet-stream" and "attachment" in h["Content-Disposition"], h
        assert req(B, "GET", f"/meshes/{mid}/note.txt", headers={"If-Modified-Since": h["Last-Modified"]})[0] == 200
        st, h, _ = req(B, "GET", "/static/index.html")
        assert st == 200 and h["Content-Type"].startswith("text/html"), h      # the site's own files, as themselves
        assert req(B, "GET", "/static/../backend/server.py")[0] in (403, 404)
        assert req(B, "DELETE", f"/api/meshes/{mid}", headers=AUTH)[0] == 200
    finally:
        out = srv.stop()
    assert "editing: with the token" in out and out.count("refused ") >= 7, out[-2000:]
    print("token site OK")


def read_only(tmp: Path) -> None:
    """READ_ONLY=1: nothing changes, token or not. READ_ONLY=auto without a token: the same."""
    for env in ({"READ_ONLY": "1", "EDIT_TOKEN": TOKEN}, {"READ_ONLY": "auto"}):
        srv = Server(tmp, **env)
        try:
            st, _, s = req(srv.url, "GET", "/api/status", headers=AUTH)
            assert s["editing"] == "none" and s["editor"] is False, s
            st, _, e = req(srv.url, "POST", "/api/projects", {"name": "x"}, headers=AUTH)
            assert st == 403 and "view-only" in e["error"], (st, e)
            assert req(srv.url, "DELETE", "/api/climbs/abc", headers=AUTH)[0] == 403
            assert req(srv.url, "GET", "/api/meshes")[0] == 200
        finally:
            out = srv.stop()
        assert "editing: none" in out, out[-1000:]
    srv = Server(tmp)     # as before: open
    try:
        st, _, s = req(srv.url, "GET", "/api/status")
        assert s["editing"] == "open" and s["editor"] is True, s
        assert req(srv.url, "POST", "/api/projects", {"name": "x"})[0] == 201
        assert req(srv.url, "POST", "/api/projects", {"name": "x"}, headers={"Origin": "https://evil.example"})[0] == 403
    finally:
        out = srv.stop()
    assert "editing: open" in out, out[-1000:]
    print("read-only and open OK")


def fetcher() -> None:
    """Only http(s), and never a private, loopback or link-local address, however it's written."""
    def refused(url: str) -> bool:
        try:
            common.check_destination(url)
            return False
        except common.HttpError as e:
            assert e.status == 0, e
            return True
    common.ALLOW_PRIVATE = False
    for url in ("file:///etc/passwd", "ftp://example.com/x", "http://127.0.0.1/", "http://localhost:8470/api/status", "http://[::1]/",
                "http://10.0.0.5:36184/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/",
                "http://100.64.0.1/", "http://2130706433/"):
        assert refused(url), url
    assert not refused("https://www.mountainproject.com/route/105835104/midnight-lightning")
    assert not refused("https://no-such-host.invalid/x")      # left for the request to report
    common.ALLOW_PRIVATE = True
    assert not refused("http://127.0.0.1/") and refused("file:///etc/passwd")
    common.ALLOW_PRIVATE = False
    print("fetcher OK")


def main() -> None:
    fetcher()
    with tempfile.TemporaryDirectory(prefix="bouldermesh-public-") as td:
        with_token(Path(td) / "a")
        read_only(Path(td) / "b")
    print("ALL PUBLIC TESTS PASSED")


if __name__ == "__main__":
    main()
