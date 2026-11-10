#!/usr/bin/env python3
"""The admin page and what it controls: settings and credentials, which photos are free to copy, the admin
server's refusals, textures stripped from meshes, and meshes coloured from only permitted photos. Offline."""
from __future__ import annotations

import base64
import json
import os
import pathlib
import socket
import stat
import struct
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import admin  # noqa: E402
import policy  # noqa: E402
import settings as settings_mod  # noqa: E402


def must_raise(fn, kind, text=""):
    try:
        fn()
    except kind as e:
        assert text in str(e), (text, e)
        return
    raise AssertionError(f"no {kind.__name__}")


# --- which photos are free to copy ---------------------------------------------------------------
for source, lic, want in [("commons", "CC BY-SA 3.0", True), ("commons", "Public domain", True), ("commons", "CC0", True),
                          ("flickr", "CC BY-NC-ND 2.0", False), ("flickr", "CC BY-ND 2.0", False), ("flickr", "CC BY-NC-SA 2.0", True),
                          ("flickr", "All rights reserved", False), ("flickr", "No known copyright restrictions", True),
                          ("youtube", "Creative Commons Attribution license (reuse allowed)", True),
                          ("youtube", "YouTube standard licence (all rights reserved)", False), ("vimeo", "by-nd", False), ("vimeo", "by-nc", True),
                          ("mountainproject", "© uploader (Mountain Project user content)", False), ("ddg", "unknown (web image; check page)", False),
                          ("upload", "the user's own photo", True), ("commons", None, False)]:
    assert policy.permitted({"source": source, "license": lic}) == want, (source, lic)
assert not policy.permitted(None)
free, mixed = {"photos": [{"source": "commons", "license": "CC0"}]}, {"photos": [{"source": "commons", "license": "CC0"}, {"source": "ddg"}]}
assert policy.mesh_permitted(free) and not policy.mesh_permitted(mixed) and policy.mesh_permitted(None) is None
assert [policy.texture_mode("all", False, mixed, False), policy.texture_mode("none", True, free, True), policy.texture_mode("permitted", False, free, False),
        policy.texture_mode("permitted", False, mixed, True), policy.texture_mode("permitted", False, mixed, False),
        policy.texture_mode("permitted", True, None, False)] == ["full", "hidden", "full", "permitted", "hidden", "full"]
ddg = {"source": "ddg"}
assert [policy.source_access(s, ddg) for s in ("all", "link", "permitted")] == ["show", "link", "hide"]
assert policy.source_access("permitted", {"source": "upload"}) == "show"
print("policy OK")

# --- settings and credentials --------------------------------------------------------------------
from cryptography.fernet import Fernet  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    s = settings_mod.Settings(d / "db.sqlite", d)
    assert s.get("compute.remote_workers") == 2 and s.get("site.textures") == "all" and s.get("compute.provider") == "none"
    s.update({"compute.remote_workers": 3, "site.sources": "link", "compute.ssh_hosts": [" a@b ", "", "c@d:2222"]})
    assert s.get("compute.remote_workers") == 3 and s.all()["site.sources"] == "link" and s.get("compute.ssh_hosts") == ["a@b", "c@d:2222"]
    for bad, text in (({"nope": 1}, "no setting"), ({"site.textures": "some"}, "one of"), ({"compute.local_workers": 9}, "from 0 to 4"),
                      ({"compute.aws.spot": "yes"}, "true or false"), ({"compute.remote_workers": 1.5}, "whole number"),
                      ({"storage.cap_gb": "lots"}, "a number"), ({"storage.cap_gb": -2.5}, "from 0"), ({"site.map_km": 0}, "from 0.05")):
        must_raise(lambda: s.update(bad), ValueError, text)
    assert s.get("compute.local_workers") == 1, "nothing saved from a bad update"
    assert s.get("storage.cap_gb") == settings_mod.DEFAULTS["storage.cap_gb"]
    s.update({"storage.cap_gb": 47.5})
    assert s.get("storage.cap_gb") == 47.5 and isinstance(s.update({"storage.cap_gb": 40})["storage.cap_gb"], float)
    assert s.get("site.map_km") == 1 and s.update({"site.map_km": 2.5})["site.map_km"] == 2.5

    os.environ.pop("BOULDERMESH_SECRETS_KEY", None)
    os.environ.pop("AWS_ACCESS_KEY_ID", None)
    assert not s.can_store_secrets() and s.secrets_status()["credentials"]["aws.access_key_id"] is None
    must_raise(lambda: s.set_secrets({"aws.access_key_id": "AKIAX"}), RuntimeError, "BOULDERMESH_SECRETS_KEY")
    os.environ["AWS_ACCESS_KEY_ID"] = "FROMENV"
    assert s.secret("aws.access_key_id") == "FROMENV" and s.secrets_status()["credentials"]["aws.access_key_id"] == "environment"
    os.environ["BOULDERMESH_SECRETS_KEY"] = Fernet.generate_key().decode()
    st = s.set_secrets({"aws.access_key_id": "AKIASAVED", "aws.secret_access_key": "shhh-very-secret"})
    path = d / settings_mod.SECRETS_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode)
    assert b"shhh-very-secret" not in path.read_bytes() and b"AKIASAVED" not in path.read_bytes(), "saved in the clear"
    assert s.secret("aws.secret_access_key") == "shhh-very-secret" and s.secret("aws.access_key_id") == "AKIASAVED", "saved wins over the environment"
    assert st["credentials"] == {"aws.access_key_id": "saved", "aws.secret_access_key": "saved", "ssh.private_key": None}
    assert "shhh" not in json.dumps(s.secrets_status())
    s.set_secrets({"aws.secret_access_key": ""})
    assert s.secret("aws.secret_access_key") is None, "an empty value removes one"
    must_raise(lambda: s.set_secrets({"aws.token": "x"}), ValueError, "no credential")
    os.environ["BOULDERMESH_SECRETS_KEY"] = Fernet.generate_key().decode()     # the wrong key
    assert s.secrets_status()["error"] and s.secret("aws.access_key_id") == "FROMENV"
    os.environ.pop("AWS_ACCESS_KEY_ID")
print("settings OK")

# --- the admin server ----------------------------------------------------------------------------
assert admin.allowed_host("127.0.0.1", False) and admin.allowed_host("10.0.0.5", False) and admin.allowed_host("192.168.1.9", False)
assert not admin.allowed_host("8.8.8.8", False) and not admin.allowed_host("0.0.0.0", False) and admin.allowed_host("0.0.0.0", True)
assert admin.allowed_host("localhost", False) and not admin.allowed_host("example.com", False)
# in Docker it listens on every address of the container, and Compose publishes it on ADMIN_BIND: that must be private too
assert admin.refusal("127.0.0.1", None, False) is None and admin.refusal("0.0.0.0", "10.0.0.5", True) is None
assert admin.refusal("0.0.0.0", None, True) is None and admin.refusal("0.0.0.0", "127.0.0.1", True) is None
assert "ADMIN_BIND=0.0.0.0" in admin.refusal("0.0.0.0", "0.0.0.0", True) and "ADMIN_BIND=8.8.8.8" in admin.refusal("0.0.0.0", "8.8.8.8", True)
assert "isn't a loopback" in admin.refusal("0.0.0.0", None, False) and "isn't a loopback" in admin.refusal("8.8.8.8", None, True)
assert admin.start(None, "8.8.8.8", 1) is None, "refuses a public address"


def free_port() -> int:
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    s = settings_mod.Settings(d / "db.sqlite", d)
    applied = []
    ctx = admin.Context(s, lambda: {"settings": s.all(), "secrets": s.secrets_status()}, lambda: applied.append(1),
                        {"ping": lambda body: {"result": f"pong {body.get('n')}"}})
    port = free_port()
    httpd = admin.start(ctx, "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"

    def call(path, body=None, kind="application/json", headers=None):
        data = None if body is None else (json.dumps(body) if not isinstance(body, str) else body).encode()
        req = urllib.request.Request(base + path, data=data, method="GET" if body is None else "POST",
                                     headers={**({"Content-Type": kind} if body is not None else {}), **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    st, page = call("/")
    assert st == 200 and b"BoulderMesh admin" in page
    st, body = call("/api/admin/settings", {"site.textures": "none"})
    assert st == 200 and json.loads(body)["settings"]["site.textures"] == "none" and applied == [1]
    assert call("/api/admin/settings", {"site.textures": "bogus"})[0] == 400
    assert call("/api/admin/settings", '{"site.textures": "all"}', kind="text/plain")[0] == 403, "a form from another site"
    assert call("/api/admin/settings", {"site.textures": "all"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert call("/api/admin/state", headers={"Host": "evil.example.com"})[0] == 403, "DNS rebinding"
    assert call("/api/admin/state", headers={"Host": f"hal:{port}"})[0] == 200, "a LAN name"
    assert s.get("site.textures") == "none", "nothing refused got through"
    assert json.loads(call("/api/admin/actions/ping", {"n": 3})[1]) == {"result": "pong 3"}
    assert call("/api/admin/actions/nope", {})[0] == 404
    httpd.shutdown()

    os.environ["ADMIN_PASSWORD"] = "pw"
    port = free_port()
    httpd = admin.start(ctx, "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"
    assert call("/api/admin/state")[0] == 401
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    assert call("/api/admin/state", headers=auth)[0] == 200
    httpd.shutdown()
    os.environ.pop("ADMIN_PASSWORD")
print("admin server OK")

# --- Cloudflare's ranges against the Caddyfile's trusted_proxies ---------------------------------
import proxy_ranges  # noqa: E402

caddy = (ROOT / "deploy" / "Caddyfile").read_text()
mine = proxy_ranges.trusted(caddy)
assert len(mine) == 22 and "173.245.48.0/20" in mine and "2c0f:f248::/32" in mine, mine
assert proxy_ranges.trusted("{\n\tservers {\n\t\ttrusted_proxies static private_ranges 10.0.0.1 # 1.2.3.0/24\n\t}\n}") == ["10.0.0.1/32"]
v4, v6 = [r for r in mine if ":" not in r], [r for r in mine if ":" in r]


def cloudflare(ipv4, ipv6):
    return lambda url: json.dumps({"success": True, "errors": [], "result": {"ipv4_cidrs": ipv4, "ipv6_cidrs": ipv6}}).encode()


with tempfile.TemporaryDirectory() as td:
    f = pathlib.Path(td) / "Caddyfile"
    f.write_text(caddy)
    said = []
    w = proxy_ranges.Watch(f, fetch=cloudflare(v4, v6), log=said.append)
    st = w.check()
    assert st["published"] == st["trusted"] == 22 and not st["missing"] and not st["extra"] and st["checked"] and not said, st
    w.fetch = cloudflare(v4[1:] + ["198.51.100.0/24"], v6)     # one added, one dropped
    st = w.check()
    assert st["missing"] == ["198.51.100.0/24"] and st["extra"] == [v4[0]] and len(said) == 1 and "198.51.100.0/24" in said[0], (st, said)
    w.check()
    assert len(said) == 1, "said once"
    w.fetch = lambda url: (_ for _ in ()).throw(OSError("offline"))
    st = w.check()
    assert "offline" in st["error"] and st["failing_since"] and st["missing"] == ["198.51.100.0/24"], "the last comparison stands"
    w.fetch = cloudflare(v4[1:] + ["198.51.100.0/24"], v6)
    f.write_text(caddy.replace(" ".join(v4), " ".join(v4[1:] + ["198.51.100.0/24"])))
    st = w.check()
    assert not st["missing"] and not st["extra"] and not st["error"] and "again" in said[-1], (st, said)
    w2 = proxy_ranges.Watch(f, fetch=lambda url: (_ for _ in ()).throw(AssertionError("asked Cloudflare for nothing")))
    f.write_text(caddy.replace("trusted_proxies", "# trusted_proxies"))
    assert w2.check() is None, "nothing trusted, nothing to compare or show"
print("proxy ranges OK")

# --- textures: stripped, and coloured from only permitted photos ----------------------------------
try:
    import numpy as np
    from PIL import Image
except ImportError:
    print("numpy or Pillow not installed: skipping the mesh tests")
    print("ALL ADMIN TESTS PASSED")
    sys.exit(0)
import meshclean  # noqa: E402
import retexture  # noqa: E402


def write_glb(path: pathlib.Path, P, I) -> None:
    """One textured primitive, its image inside the file, as a texture that must not survive stripping."""
    png = b"\x89PNG\r\n\x1a\n" + b"pixels" * 10
    uv = (P[:, :2] / max(1.0, float(P.max()))).astype("<f4")
    blob = bytearray()
    views = []
    for data in (P.astype("<f4"), uv, I.astype("<u4"), np.frombuffer(png, np.uint8)):
        blob += b"\0" * (-len(blob) % 4)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": data.nbytes})
        blob += data.tobytes()
    blob += b"\0" * (-len(blob) % 4)
    j = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}], "buffers": [{"byteLength": len(blob)}],
         "bufferViews": views, "images": [{"bufferView": 3, "mimeType": "image/png"}], "textures": [{"source": 0}],
         "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
         "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(P), "type": "VEC3", "min": P.min(0).tolist(), "max": P.max(0).tolist()},
                       {"bufferView": 1, "componentType": 5126, "count": len(P), "type": "VEC2"},
                       {"bufferView": 2, "componentType": 5125, "count": I.size, "type": "SCALAR"}],
         "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}]}
    text = json.dumps(j).encode()
    text += b" " * (-len(text) % 4)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(text) + 8 + len(blob)))
        f.write(struct.pack("<II", len(text), 0x4E4F534A) + text + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))


# a 1 x 1 square in z = 0 made of a grid of vertices, seen face-on by a camera 2 units in front of it
n = 11
xs, ys = np.meshgrid(np.linspace(-0.5, 0.5, n), np.linspace(-0.5, 0.5, n))
P = np.c_[xs.ravel(), ys.ravel(), np.zeros(n * n)]
I = np.array([[r * n + c, r * n + c + 1, (r + 1) * n + c] for r in range(n - 1) for c in range(n - 1)] +
             [[r * n + c + 1, (r + 1) * n + c + 1, (r + 1) * n + c] for r in range(n - 1) for c in range(n - 1)])
cam = {"q": [1.0, 0.0, 0.0, 0.0], "t": [0.0, 0.0, 2.0], "lens": {"model": 2, "width": 400, "height": 400, "params": [400.0, 200.0, 200.0, 0.0]}}
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    mesh, photos = d / "mesh", d / "project" / "photos"
    mesh.mkdir()
    photos.mkdir(parents=True)
    write_glb(mesh / "mesh.glb", P, I)
    stripped = meshclean.strip_textures(mesh / "mesh.glb", mesh / "mesh.untextured.glb")
    js, bufs = meshclean._read_glb(stripped)
    assert "images" not in js and "textures" not in js and b"pixels" not in stripped.read_bytes(), "nothing of the texture"
    assert (meshclean._geometry(js, bufs)[1] == meshclean._geometry(*meshclean._read_glb(mesh / "mesh.glb"))[1]).all(), "same faces"

    img = np.zeros((400, 400, 3), np.uint8)
    img[:, :200] = (255, 0, 0)     # red on the left of the photo, blue on the right
    img[:, 200:] = (0, 0, 255)
    Image.fromarray(img).save(photos / "commons_a.jpg", quality=98)
    Image.fromarray(img).save(photos / "ddg_b.jpg", quality=98)
    (mesh / "cameras.json").write_text(json.dumps([{"name": "ddg_b.jpg", **cam}]))
    record = {"basis": "placed", "photos": [{"file": "ddg_b.jpg", "source": "ddg", "license": "unknown"}]}
    (mesh / "photo_sources.json").write_text(json.dumps(record))
    assert retexture.retexture(mesh, photos, print)["note"] == "none of its placed photos is clearly free to copy"
    assert not retexture.has_variant(mesh)
    (mesh / "cameras.json").write_text(json.dumps([{"name": "commons_a.jpg", "q": cam["q"], "t": cam["t"]}]))
    record["photos"].append({"file": "commons_a.jpg", "source": "commons", "license": "CC BY-SA 4.0"})
    (mesh / "photo_sources.json").write_text(json.dumps(record))
    assert "rebuild" in retexture.retexture(mesh, photos, print)["note"], "no lens kept"
    (mesh / "cameras.json").write_text(json.dumps([{"name": "commons_a.jpg", **cam}, {"name": "ddg_b.jpg", **cam}]))
    (mesh / "cleanup.json").write_text("{}")
    info = retexture.retexture(mesh, photos, print)
    assert info["photos"] == ["commons_a.jpg"] and info["seen"] > 0.9, info
    jc, bc = meshclean._read_glb(mesh / "permitted" / "mesh.glb")
    assert "images" not in jc and (mesh / "permitted" / "cleanup.json").exists()
    prim = jc["meshes"][0]["primitives"][0]
    C, Q = meshclean._accessor(jc, bc, prim["attributes"]["COLOR_0"]), meshclean._accessor(jc, bc, prim["attributes"]["POSITION"])
    left, right = C[Q[:, 0] < -0.1], C[Q[:, 0] > 0.1]
    assert (left[:, 0] > 0.8).all() and (left[:, 2] < 0.1).all() and (right[:, 2] > 0.8).all() and (right[:, 0] < 0.1).all(), (left[:3], right[:3])
    assert (meshclean._geometry(jc, bc)[1] == meshclean._geometry(*meshclean._read_glb(mesh / "mesh.glb"))[1]).all(), "same faces, same order"
    made = (mesh / "permitted" / "mesh.glb").read_bytes()
    colours = retexture.vertex_colours
    retexture.vertex_colours = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())    # stopped partway, as by a restart
    try:
        retexture.retexture(mesh, photos, print)
        raise AssertionError("not stopped")
    except KeyboardInterrupt:
        pass
    retexture.vertex_colours = colours
    assert (mesh / "permitted" / "mesh.glb").read_bytes() == made and retexture.load(mesh)["photos"] == ["commons_a.jpg"], "the old one stays"
    assert retexture.retexture(mesh, photos, print)["photos"] == ["commons_a.jpg"] and not (mesh / "permitted.partial").exists()
print("textures OK")
print("ALL ADMIN TESTS PASSED")
