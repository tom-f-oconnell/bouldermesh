#!/usr/bin/env python3
"""What the site does when exposed: the editing token and read-only modes, refusing other sites' pages, the page's
Content-Security-Policy and the other headers, files served as what they are or as downloads, 304s, bad lengths, and
the fetcher's refusal of private addresses, HEAD requests, the sender's address from a proxy's header, the build deploy/build.sh records, which remote
machines then run, and files sent in parts, including a video turned into a project's photos. Starts the server three times on free ports; offline.

    python3 tests/test_public.py
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
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
        self.port, self.admin_port = free_port(), free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        full = dict(os.environ, DATA_DIR=str(tmp / "data"), PORT=str(self.port), HOST="127.0.0.1", ADMIN_PORT=str(self.admin_port),
                    HTTP_MIN_INTERVAL="0", STOP_WAIT_MINUTES="0.1", **env)
        for k in ("EDIT_TOKEN", "READ_ONLY", "CSP", "CLIENT_IP_HEADER", "BOULDERMESH_COMMIT", "BOULDERMESH_IMAGE", "ADMIN_SSH",
                  "BOULDERMESH_WORKER_IMAGE", "ADMIN_BIND", "ADMIN_PUBLISHED_PORT", "ADMIN_URL", "ADMIN_PASSWORD", "ADMIN_HOST",
                  "SITE_URL", "SITE_HOST"):
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


def req(B: str, method: str, path: str, body: dict | bytes | None = None, headers: dict | None = None, files=None,
        form: dict | None = None):
    """(status, headers, decoded JSON or bytes)."""
    data, hdrs = None, dict(headers or {})
    if isinstance(body, bytes):
        data, hdrs["Content-Type"] = body, "application/octet-stream"
    elif body is not None:
        data, hdrs["Content-Type"] = json.dumps(body).encode(), "application/json"
    if files is not None:
        bnd, buf = uuid.uuid4().hex, io.BytesIO()
        for k, v in (form or {}).items():
            buf.write(f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
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
    """EDIT_TOKEN set, READ_ONLY=auto and CLIENT_IP_HEADER=X-Real-IP as the public setup has them: a token site."""
    srv = Server(tmp, EDIT_TOKEN=TOKEN, READ_ONLY="auto", CLIENT_IP_HEADER="X-Real-IP", SITE_URL="https://rocks.example/")
    B, port = srv.url, srv.port
    try:
        # the page, with its policy: a hash for each inline script, nothing framed, and the other headers
        st, h, page = req(B, "GET", "/")
        assert st == 200 and h["Content-Type"].startswith("text/html") and h["Cache-Control"] == "no-cache, no-transform", (st, h)
        csp = h["Content-Security-Policy"]
        inline = re.findall(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", (ROOT / "frontend" / "index.html").read_bytes(), re.S)
        want = {f"'sha256-{base64.b64encode(hashlib.sha256(m).digest()).decode()}'" for m in inline}
        assert len(inline) == 3 and want <= set(re.findall(r"'sha256-[^']+'", csp)), (want, csp)
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0], csp
        assert b'onclick="' not in page, "an inline handler, which the policy would block"
        assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY" and h["Referrer-Policy"], h
        assert "Python" not in h.get("Server", ""), h.get("Server")
        st, _, _ = req(B, "GET", "/", headers={"If-None-Match": h["ETag"]})
        assert st == 304, st

        # the page as served: its styles and module script in files named by what's in them, kept a year; the page
        # itself small, and compressed here, as its no-transform keeps Caddy and Cloudflare from doing that
        source = (ROOT / "frontend" / "index.html").read_text()
        assets = re.findall(r'(?:src|href)="/static/(app\.[0-9a-f]{16}\.(js|css))"', page.decode())
        assert sorted(ext for _, ext in assets) == ["css", "js"] and len(page) < len(source) / 5, (assets, len(page))
        assert b"<style>" not in page and b'<script type="module">' not in page and b'<script type="importmap">' in page
        for name, ext in assets:
            st, h, body = req(B, "GET", f"/static/{name}")
            assert st == 200 and h["Cache-Control"] == "public, max-age=31536000, immutable" and body.decode() in source, (name, h)
            assert h["Content-Type"].startswith("text/javascript" if ext == "js" else "text/css"), h
            st, h, gz = req(B, "GET", f"/static/{name}", headers={"Accept-Encoding": "gzip"})
            assert h["Content-Encoding"] == "gzip" and gzip.decompress(gz) == body and h["Vary"] == "Accept-Encoding", h
            assert req(B, "GET", f"/static/{name}", headers={"If-None-Match": h["ETag"]})[0] == 304
        st, h, _ = req(B, "GET", "/static/app.0123456789abcdef.js")
        assert st == 404 and h["Cache-Control"] == "no-store", "an older page's, gone after a restart: kept by no one"
        st, h, gz = req(B, "GET", "/", headers={"Accept-Encoding": "gzip, br"})
        assert h["Content-Encoding"] == "gzip" and gzip.decompress(gz) == page and h["Cache-Control"] == "no-cache, no-transform", h
        assert b'<meta property="og:title" content="BoulderMesh">' in page and b'href="https://rocks.example/"' in page, "its preview"
        assert f"connect-src 'self' data: blob: http://localhost:{srv.admin_port}" in csp, \
            "buffers embedded in a mesh load as data: URLs; the Admin button may look for the admin page"
        st, h, s = req(B, "GET", "/api/status")
        assert st == 200 and s["editing"] == "token" and s["editor"] is False and h["Cache-Control"] == "no-cache", s
        assert s["admin"] is None, "where the admin page is, and how to reach it, are for editors"
        assert s["map_km"] == 1 and "volunteer_url" in s, "how far the map opens around a boulder, and the Volunteer button's page"
        assert "Content-Security-Policy" not in h, "the policy is for the page"
        st, _, s = req(B, "GET", "/api/status", headers=AUTH)
        assert s["editor"] is True, s
        p = srv.admin_port
        assert s["admin"] == {"url": f"http://localhost:{p}", "local": f"http://localhost:{p}", "lan": False, "password": False,
                              "ssh": f"ssh -N -L {p}:127.0.0.1:{p} <you>@<this server>", "ssh_known": False}, s["admin"]

        # what went wrong in a visitor's browser is kept, from anyone viewing it, but only from its own pages, small, not too often
        report = {"kind": "mesh", "message": "THREE.GLTFLoader: Failed to load buffer " + "x" * 2000, "mesh": "abc123", "file": "mesh.glb"}
        st, _, r = req(B, "POST", "/api/client-errors", report, headers={"User-Agent": "TestBrowser/1", "X-Real-IP": "198.51.100.7"})
        assert st == 202 and r == {"kept": True}, (st, r)
        kept = [json.loads(x) for x in (tmp / "data" / "client-errors.jsonl").read_text().splitlines()]
        assert kept[-1]["kind"] == "mesh" and kept[-1]["mesh"] == "abc123" and len(kept[-1]["message"]) == 1000, kept[-1]
        assert kept[-1]["browser"] == "TestBrowser/1" and kept[-1]["editor"] is False and "198.51.100.7" not in json.dumps(kept), kept[-1]
        assert req(B, "POST", "/api/client-errors", report, headers={"Origin": "https://evil.example"})[0] == 403
        assert req(B, "POST", "/api/client-errors", b"x" * 9000)[0] == 413
        assert req(B, "POST", "/api/client-errors", b"[1]")[0] == 400
        codes = [req(B, "POST", "/api/client-errors", report, headers={"X-Real-IP": "198.51.100.8"})[0] for _ in range(21)]
        assert codes == [202] * 20 + [429], codes
        assert req(B, "POST", "/api/client-errors", report, headers={"X-Real-IP": "198.51.100.9"})[0] == 202, "others still can"

        # the editing token to a phone: the admin page's QR has a code for it, not the token, which works once and is replaced
        A = f"http://127.0.0.1:{srv.admin_port}"
        st, _, pr = req(A, "GET", "/api/admin/state")
        assert pr["pairing"] == {"editing": "token", "site_url": "https://rocks.example", "waiting": False, "given": None}, pr["pairing"]
        st, _, a = req(A, "POST", "/api/admin/actions/edit-pair", {})
        assert st == 200 and a["seconds"] == 300, (st, a)
        try:
            import segno  # noqa: F401
            assert a["qr"].startswith("<svg"), a["qr"]
        except ImportError:
            assert a["qr"] is None, "without segno, no QR code"
        assert a["text"].startswith("https://rocks.example/#pair=") and TOKEN not in json.dumps(a), "a link with a code, not the token"
        assert req(A, "GET", "/api/admin/state")[2]["pairing"]["waiting"] is True
        b = req(A, "POST", "/api/admin/actions/edit-pair", {})[2]
        old, code = a["text"].split("#pair=")[1], b["text"].split("#pair=")[1]
        assert old != code and (b["qr"] != a["qr"] or not a["qr"]), "a new code each time"
        pair = lambda c, **h: req(B, "POST", "/api/pair", {"code": c}, headers={"X-Real-IP": "198.51.100.50", "User-Agent": "Phone/1", **h})  # noqa: E731
        st, _, e = pair(old)
        assert st == 403 and "admin page" in e["error"], ("the code it replaced", st, e)
        assert pair(code, Origin="https://evil.example")[0] == 403, "only from the site's own pages"
        assert req(B, "POST", "/api/pair", b"x" * 2000)[0] == 400 and req(B, "POST", "/api/pair", b"[1]")[0] == 400
        st, _, t = pair(code)
        assert st == 200 and t == {"token": TOKEN}, (st, t)
        assert pair(code)[0] == 403, "once only"
        pr = req(A, "GET", "/api/admin/state")[2]["pairing"]
        assert pr["waiting"] is False and pr["given"]["to"] == "198.51.100.50 (Phone/1)" and pr["given"]["at"] >= b["made"], pr
        assert "camera=(self)" in req(B, "GET", "/")[1]["Permissions-Policy"], "Edit…'s Scan button uses the camera"

        # changes: refused without the token, before the body is read; with it, as before; never from another site's page
        st, h, e = req(B, "POST", "/api/projects", {"name": "x"})
        assert st == 401 and "token" in e["error"] and h["WWW-Authenticate"].startswith("Bearer"), (st, e, h)
        st, _, e = req(B, "POST", "/api/projects", {"name": "x"}, headers={"Authorization": "Bearer wrong"})
        assert st == 401, (st, e)
        st, _, e = req(B, "POST", "/api/projects", {"name": "x"}, headers={"Authorization": f"Bearer {TOKEN} "})
        assert st == 401, (st, e)
        st, _, e = req(B, "PATCH", "/api/climbs/abc", {"name": "x"}, headers={"X-Real-IP": "203.0.113.7"})
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
        # each mesh a visitor's browser shows is kept, with how it was asked for; not one that isn't there
        view = {"mesh": mid, "via": "random", "seconds": 2.345, "note": "Texture reduced to 4096 pixels"}
        st, _, r = req(B, "POST", "/api/mesh-views", view, headers={"X-Real-IP": "198.51.100.20"})
        assert st == 202, (st, r)
        seen = [json.loads(x) for x in (tmp / "data" / "mesh-views.jsonl").read_text().splitlines()]
        assert {k: seen[-1].get(k) for k in ("mesh", "via", "seconds", "note", "editor")} == \
            {"mesh": mid, "via": "random", "seconds": 2.3, "note": "Texture reduced to 4096 pixels", "editor": False}, seen[-1]
        assert seen[-1]["name"] and "198.51.100.20" not in json.dumps(seen), seen[-1]
        assert req(B, "POST", "/api/mesh-views", {"mesh": "0123456789ab"})[0] == 404
        # a visitor's rating and comment: from anyone, as reports are; one each per browser, wherever from, replaced as changed
        rate = lambda body, ip="198.51.100.30", **h: req(B, "POST", f"/api/meshes/{mid}/rating", body, headers={"X-Real-IP": ip, **h})  # noqa: E731
        st, _, r = rate({"browser": "a" * 32, "rating": 1, "comment": "holes all over"})
        assert st == 200 and r["rating"] == {"mean": 1.0, "n": 1}, (st, r)
        assert rate({"browser": "a" * 32, "rating": 3, "comment": " better now "})[2]["rating"] == {"mean": 3.0, "n": 1}, "replaced"
        assert rate({"browser": "b" * 32, "rating": 2})[2]["rating"] == {"mean": 2.5, "n": 2}, "another browser"
        assert rate({"browser": "a" * 32, "rating": 1, "comment": "worse on my phone"}, ip="198.51.100.31")[2]["rating"] == \
            {"mean": 1.5, "n": 2}, "the same browser from another address: still its one"
        assert rate({"browser": "b" * 32, "rating": None, "comment": "  "})[2]["rating"] == {"mean": 1.0, "n": 1}, "taken back: gone"
        assert rate({"browser": "c" * 32, "comment": "only a comment"})[2]["rating"] == {"mean": 1.0, "n": 1}, "a comment isn't a rating"
        assert next(m for m in req(B, "GET", "/api/meshes")[2] if m["id"] == mid)["rating"] == {"mean": 1.0, "n": 1}
        with sqlite3.connect(tmp / "data" / "bouldermesh.sqlite") as con:
            kept = con.execute("SELECT browser, address, rating, comment FROM mesh_ratings WHERE mesh_id=? ORDER BY browser", (mid,)).fetchall()
        assert kept == [("a" * 32, "198.51.100.31", 1, "worse on my phone"), ("c" * 32, "198.51.100.30", None, "only a comment")], kept
        for bad in ({"browser": "a" * 32, "rating": 4}, {"browser": "a" * 32, "rating": True}, {"browser": "x", "rating": 1}, [1]):
            assert rate(bad)[0] == 400, bad
        assert req(B, "POST", "/api/meshes/0123456789ab/rating", {"browser": "a" * 32, "rating": 1})[0] == 404
        assert rate({"browser": "a" * 32, "rating": 1}, Origin="https://evil.example")[0] == 403
        out = subprocess.run([sys.executable, str(ROOT / "cli.py"), "mesh-ratings", "--data", str(tmp / "data")],
                             capture_output=True, text=True, check=True).stdout
        assert f"1.00  {mid} " in out and ": 0 good, 0 OK, 1 bad" in out and "(bad):\n    worse on my phone" in out and "only a comment" in out, out
        assert "198.51.100" not in out and "a" * 32 not in out, "not who"
        codes = [rate({"browser": "d" * 32, "rating": 2}, ip="198.51.100.40")[0] for _ in range(121)]
        assert codes[:120] == [200] * 120 and codes[120] == 429, "not too often from one address"
        assert req(B, "POST", "/api/mesh-views", view, headers={"Origin": "https://evil.example"})[0] == 403
        out = subprocess.run([sys.executable, str(ROOT / "cli.py"), "mesh-views", "--data", str(tmp / "data")],
                             capture_output=True, text=True, check=True).stdout
        assert f"{mid} " in out and "from random in 2.3 s" in out and "1 view(s) of 1 mesh(es); by how: random 1" in out, out
        # a scene splat beside it is listed and served; the page's policy lets the splat viewer's worker run
        (tmp / "data" / "meshes" / mid / "scene.splat").write_bytes(b"\0" * 64)
        assert next(m for m in req(B, "GET", "/api/meshes")[2] if m["id"] == mid)["splat"] is True
        st, h, body = req(B, "GET", f"/meshes/{mid}/scene.splat")
        assert st == 200 and h["Content-Type"] == "application/octet-stream" and len(body) == 64 and "Content-Disposition" not in h, h
        assert "worker-src 'self' blob:" in csp and "'wasm-unsafe-eval'" in csp and "'unsafe-eval'" not in csp.replace("'wasm-unsafe-eval'", ""), csp
        assert req(B, "GET", f"/meshes/{mid}/evil.html")[0] == 404
        st, h, body = req(B, "GET", f"/meshes/{mid}/tex.png")
        assert st == 200 and h["Content-Type"] == "image/png" and body == PNG and h["Cache-Control"].startswith("public") and "Content-Disposition" not in h, h
        assert req(B, "GET", f"/meshes/{mid}/tex.png", headers={"If-Modified-Since": h["Last-Modified"]})[0] == 304
        # HEAD: a GET's headers without its body; a method the server hasn't got is a 501, not a dropped connection
        for path, length in ((f"/meshes/{mid}/tex.png", len(PNG)), ("/", None), ("/api/status", None)):
            out = raw_request(port, f"HEAD {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            head, _, body = out.partition(b"\r\n\r\n")
            got = int(re.search(rb"Content-Length: (\d+)", head)[1])
            assert head.startswith(b"HTTP/1.0 200") and body == b"" and got == (length or got) > 0, out[:400]
        out = raw_request(port, f"PUT / HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.0 501"), out[:300]
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
        with sqlite3.connect(tmp / "data" / "bouldermesh.sqlite") as con:
            assert not con.execute("SELECT 1 FROM mesh_ratings WHERE mesh_id=?", (mid,)).fetchall(), "its ratings go with it"
    finally:
        out = srv.stop()
    assert "editing: with the token" in out and out.count("refused ") >= 7, out[-2000:]
    assert "refused PATCH /api/climbs/abc from 203.0.113.7" in out and '"HEAD / HTTP/1.0" 200' in out, out[-2000:]
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
            assert req(srv.url, "POST", "/api/meshes/0123456789ab/rating", {"browser": "a" * 32, "rating": 1})[0] == 404, \
                "rating isn't changing the site: taken, here as there's no such mesh"
            assert req(srv.url, "GET", "/api/meshes")[0] == 200
            st, _, e = req(f"http://127.0.0.1:{srv.admin_port}", "POST", "/api/admin/actions/edit-pair", {})
            assert st == 400 and "no editing token" in e["error"], (st, e)
            assert req(srv.url, "POST", "/api/pair", {"code": "x" * 22})[0] == 400
        finally:
            out = srv.stop()
        assert "editing: none" in out, out[-1000:]
    srv = Server(tmp, ADMIN_SSH="tom@203.0.113.7", ADMIN_HOST="10.1.2.3")     # as before: open
    try:
        st, _, s = req(srv.url, "GET", "/api/status")
        assert s["editing"] == "open" and s["editor"] is True, s
        p = srv.admin_port
        assert (s["admin"]["url"], s["admin"]["lan"], s["admin"]["ssh"]) == \
            (f"http://10.1.2.3:{p}", True, f"ssh -N -L {p}:10.1.2.3:{p} tom@203.0.113.7"), "a LAN address, forwarded from elsewhere"
        assert req(srv.url, "POST", "/api/projects", {"name": "x"})[0] == 201
        assert req(srv.url, "POST", "/api/projects", {"name": "x"}, headers={"Origin": "https://evil.example"})[0] == 403
    finally:
        out = srv.stop()
    assert "editing: open" in out, out[-1000:]
    print("read-only and open OK")


def build(tmp: Path) -> None:
    """A build from deploy/build.sh says which it is, and remote machines run it unless the admin page names another."""
    own = "ghcr.io/someone/bouldermesh:abc1234"
    srv = Server(tmp, BOULDERMESH_COMMIT="abc1234", BOULDERMESH_IMAGE=own)
    admin = f"http://127.0.0.1:{srv.admin_port}"
    try:
        assert req(srv.url, "GET", "/api/status")[2]["version"] == "abc1234"
        st = req(admin, "GET", "/api/admin/state")[2]
        assert st["version"] == "abc1234" and st["own_image"] == own and st["worker_image"] == own, st
        st = req(admin, "POST", "/api/admin/settings", {"compute.image": "registry.example/other:1"})[2]
        assert st["worker_image"] == "registry.example/other:1" and st["own_image"] == own, st
        st = req(admin, "POST", "/api/admin/settings", {"compute.image": ""})[2]
        assert st["worker_image"] == own, st
    finally:
        out = srv.stop()
    assert "BoulderMesh abc1234 on" in out, out[-1000:]
    # the server's CPU build, whose machines run the same commit's GPU build
    srv = Server(tmp, BOULDERMESH_COMMIT="abc1234", BOULDERMESH_IMAGE=own, BOULDERMESH_WORKER_IMAGE=own + "-gpu")
    try:
        st = req(f"http://127.0.0.1:{srv.admin_port}", "GET", "/api/admin/state")[2]
        assert st["own_image"] == own and st["machines_image"] == st["worker_image"] == own + "-gpu", st
    finally:
        srv.stop()
    srv = Server(tmp)     # any other build
    try:
        st = req(f"http://127.0.0.1:{srv.admin_port}", "GET", "/api/admin/state")[2]
        assert st["version"] is None and st["own_image"] is None and st["worker_image"] == "", st
    finally:
        srv.stop()
    print("build OK")


def in_parts(tmp: Path) -> None:
    """A file too big for one request (Cloudflare's 100 MB) goes in parts, which a request then names: a mesh's, and a
    video's, which becomes a project's photos."""
    srv = Server(tmp, EDIT_TOKEN=TOKEN, READ_ONLY="auto", MAX_UPLOAD_MB="2")
    B = srv.url
    try:
        assert req(B, "POST", "/api/uploads", {"name": "x.ply", "size": 10})[0] == 401, "the token, as for any change"
        assert req(B, "POST", "/api/uploads", {"name": "x.ply", "size": 3 * 2**20}, headers=AUTH)[0] == 400, "over MAX_UPLOAD"
        st, _, up = req(B, "POST", "/api/uploads", {"name": "../big.ply", "size": len(PLY)}, headers=AUTH)
        assert st == 201 and up["part"] == 64 * 2**20, up
        uid = up["id"]
        assert req(B, "POST", f"/api/uploads/{uid}?offset=0", PLY[:50], headers=AUTH)[2]["received"] == 50
        st, _, e = req(B, "POST", f"/api/uploads/{uid}?offset=0", PLY[:50], headers=AUTH)     # sent twice
        assert st == 409 and e["received"] == 50, (st, e)
        assert req(B, "GET", f"/api/uploads/{uid}")[2] == {"name": "big.ply", "size": len(PLY), "received": 50}
        st, _, e = req(B, "POST", "/api/meshes", headers=AUTH, files=[], form={"uploads": uid})
        assert st == 400 and "only in part" in e["error"], (st, e)
        assert req(B, "POST", f"/api/uploads/{uid}?offset=50", PLY[50:] + b"extra", headers=AUTH)[0] == 400, "past its size"
        assert req(B, "POST", f"/api/uploads/{uid}?offset=50", PLY[50:], headers=AUTH)[2]["received"] == len(PLY)
        st, _, m = req(B, "POST", "/api/meshes", headers=AUTH, files=[("files", "tex.png", PNG)], form={"uploads": uid, "name": "parts"})
        assert st == 201 and m["filename"] == "big.ply" and m["name"] == "parts", m
        assert req(B, "GET", f"/meshes/{m['id']}/big.ply")[2] == PLY and req(B, "GET", f"/meshes/{m['id']}/tex.png")[2] == PNG
        assert req(B, "GET", f"/api/uploads/{uid}")[0] == 404, "used up"
        assert req(B, "POST", "/api/meshes", headers=AUTH, files=[], form={"uploads": uid})[0] == 400

        if shutil.which("ffmpeg"):
            video = tmp / "walk.mp4"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
                            "-pix_fmt", "yuv420p", str(video)], check=True)
            blob = video.read_bytes()
            pid = req(B, "POST", "/api/projects", {"name": "walk-around"}, headers=AUTH)[2]["id"]
            uid = req(B, "POST", "/api/uploads", {"name": "walk.mp4", "size": len(blob)}, headers=AUTH)[2]["id"]
            req(B, "POST", f"/api/uploads/{uid}?offset=0", blob, headers=AUTH)
            st, _, r = req(B, "POST", f"/api/projects/{pid}/photos", headers=AUTH, files=[("photos", "a.png", PNG), ("photos", "notes.txt", b"x")],
                           form={"uploads": uid})
            assert st == 200 and r == {"saved": ["a.png"], "videos": ["walk.mp4"]}, r
            for _ in range(60):
                proj = req(B, "GET", f"/api/projects/{pid}")[2]
                if not proj["videos_waiting"]:
                    break
                time.sleep(0.5)
            frames = [f for f in proj["photos"] if f.startswith("vid_")]
            assert not proj["videos_waiting"] and 4 <= len(frames) <= 16 and "a.png" in proj["photos"], proj
            assert "frame(s) added to the photos" in proj["log"], proj["log"][-500:]
        else:
            print("ffmpeg not installed: skipping the video upload")
    finally:
        srv.stop()
    print("uploads in parts OK")


def scene_hidden(tmp: Path) -> None:
    """A scene splat is learnt from all of a mesh's photos, so where textures aren't all shown, neither is it."""
    srv = Server(tmp, SITE_TEXTURES="none")
    try:
        st, _, up = req(srv.url, "POST", "/api/meshes", files=[("files", "cube.ply", PLY)])
        assert st == 201, up
        (tmp / "data" / "meshes" / up["id"] / "scene.splat").write_bytes(b"\0" * 32)
        assert next(m for m in req(srv.url, "GET", "/api/meshes")[2] if m["id"] == up["id"])["splat"] is False
        st, _, e = req(srv.url, "GET", f"/meshes/{up['id']}/scene.splat")
        assert st == 403 and "scene" in e["error"], (st, e)
    finally:
        srv.stop()
    print("hidden scene OK")


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


def links_site(tmp: Path) -> None:
    """Links to climbs, boulders and meshes: /c/<name> and /b/<name> as the server names them, aliases after a rename,
    the page's preview for each, a boulder's meshes to pick from, and a deleted mesh's link."""
    srv = Server(tmp, SITE_URL="https://rocks.example")
    B, port = srv.url, srv.port
    try:
        mids = [req(B, "POST", "/api/meshes", files=[("files", f"m{i}.ply", PLY)])[2]["id"] for i in range(3)]
        mandala, pigsty = '["California", "Bishop", "Buttermilks", "Mandala Boulder"]', '["California", "Joshua Tree", "The Pig Sty"]'
        climbs = [("c1", "The Mandala", "V12", "Mandala Boulder", mandala), ("c2", "Unnamed", "V2", "The Pig Sty", pigsty),
                  ("c3", "Unnamed", "V3", "The Pig Sty", pigsty), ("c4", "Seven Spanish Angels", "V5", "Mandala Boulder", mandala),
                  ("c5", "Mandala Sit", "V12", "Mandala Boulder", mandala)]
        with sqlite3.connect(tmp / "data" / "bouldermesh.sqlite") as con:
            for n, (cid, name, grade, boulder, path) in enumerate(climbs):
                con.execute("INSERT INTO climbs(id,source,source_id,url,name,kind,grade,boulder_name,area_path,data,created) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (cid, "test", cid, None, name, "boulder", grade, boulder, path, "{}", n))
            for mid, cid in zip(mids, ("c1", "c2", "c4")):
                con.execute("INSERT INTO mesh_links VALUES(?,?,NULL,?)", (mid, cid, time.time()))
        listed = {m["id"]: m for m in req(B, "GET", "/api/meshes")[2]}
        assert [c["slug"] for c in listed[mids[0]]["climbs"]] == ["the-mandala"], listed[mids[0]]["climbs"]
        assert listed[mids[0]]["boulder_link"] == {"slug": "mandala-boulder", "name": "Mandala Boulder", "meshes": 2}, listed[mids[0]]
        assert listed[mids[1]]["boulder_link"]["meshes"] == 1 and listed[mids[1]]["climbs"][0]["slug"] == "unnamed"
        assert req(B, "GET", "/api/links/c/unnamed-the-pig-sty")[2]["id"] == "c3", "the second gets its boulder's name added"
        same = req(B, "GET", "/api/links/c/unnamed")[2]["same"]
        assert same == [{"slug": "unnamed-the-pig-sty", "name": "Unnamed", "grade": "V3", "boulder": "The Pig Sty"}], same

        # each link's page: its title and preview; another spelling redirects; one that's no one's is a 404
        st, h, page = req(B, "GET", "/c/the-mandala")
        assert st == 200 and b'<meta property="og:title" content="The Mandala V12">' in page and \
            b'<link rel="canonical" href="https://rocks.example/c/the-mandala">' in page and b"<title>The Mandala V12 \xc2\xb7 BoulderMesh</title>" in page, page[:1500]
        assert h["Cache-Control"] == "no-cache, no-transform" and "Content-Security-Policy" in h, h
        out = raw_request(port, f"GET /c/The-Mandala HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.0 301") and b"Location: /c/the-mandala\r\n" in out, out[:300]
        st, _, page = req(B, "GET", "/c/nobody-at-all")
        assert st == 404 and b'name="robots" content="noindex"' in page, st
        st, _, page = req(B, "GET", "/b/mandala-boulder")
        assert st == 200 and b"Mandala Boulder in 3D \xc2\xb7 Buttermilks, Bishop \xc2\xb7 2 meshes." in page, page[:1500]
        assert req(B, "GET", f"/m/{mids[1]}")[0] == 200 and req(B, "GET", "/m/0123456789ab")[0] == 404

        # a boulder's meshes to pick from: most of its climbs first, and its climbs no mesh places
        b = req(B, "GET", "/api/links/b/mandala-boulder")[2]
        assert sorted(m["id"] for m in b["meshes"]) == sorted([mids[0], mids[2]]) and b["missing"] == [{"id": "c5", "why": None}], b
        assert {m["id"]: m["home"] for m in b["meshes"]} == {mids[0]: ["c1"], mids[2]: ["c4"]} and b["area"] == ["Bishop", "Buttermilks"], b

        # a rename: a new name, and the old one an alias of it
        assert req(B, "PATCH", "/api/climbs/c1", {"name": "The Mandala Direct"})[0] == 200
        assert req(B, "GET", "/api/links/c/the-mandala-direct")[2]["id"] == "c1", "a name it hasn't got yet is looked for at once"
        out = raw_request(port, f"GET /c/the-mandala?x=1 HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.0 301") and b"Location: /c/the-mandala-direct?x=1\r\n" in out, out[:300]
        assert req(B, "GET", "/api/links/c/the-mandala")[2]["slug"] == "the-mandala-direct"

        # a deleted mesh: what its link opens instead
        assert req(B, "DELETE", f"/api/meshes/{mids[2]}")[0] == 200
        g = req(B, "GET", f"/api/links/m/{mids[2]}")[2]["gone"]
        assert g["climbs"] == ["c4"] and g["boulder_slug"] == "mandala-boulder" and g["successor"] is None, g
        assert req(B, "GET", f"/m/{mids[2]}")[0] == 200 and req(B, "GET", "/api/links/m/0123456789ab")[0] == 404
    finally:
        srv.stop()
    print("links OK")


def main() -> None:
    fetcher()
    with tempfile.TemporaryDirectory(prefix="bouldermesh-public-") as td:
        with_token(Path(td) / "a")
        read_only(Path(td) / "b")
        build(Path(td) / "c")
        in_parts(Path(td) / "d")
        scene_hidden(Path(td) / "e")
        links_site(Path(td) / "f")
    print("ALL PUBLIC TESTS PASSED")


if __name__ == "__main__":
    main()
