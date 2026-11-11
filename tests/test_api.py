"""End-to-end API test: starts the server on a free port with a temporary DATA_DIR, then exercises
climb import/manual/export, project -> reconstruct (demo mode) -> mesh auto-link, mesh links,
geojson, and a harvest job whose "web photos" are served by the server itself (no internet needed).

    python3 tests/test_api.py

Needs Pillow; uses OpenCV+numpy when present (realistic multi-view synthetic photos and the
view-graph verdict), otherwise falls back to simple images and the count-based verdict.
"""
from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import struct
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import glbpack  # noqa: E402
import pipeline  # noqa: E402

try:
    import cv2
    import numpy as np
    HAVE_CV = True
except ImportError:  # pragma: no cover
    HAVE_CV = False
from PIL import Image, ImageDraw  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


ADMIN = None    # the admin page's address


def admin_set(changes: dict) -> None:
    urllib.request.urlopen(urllib.request.Request(ADMIN + "/api/admin/settings", data=json.dumps(changes).encode(),
                                                  headers={"Content-Type": "application/json"}, method="POST")).read()


def status_of(url: str) -> int:
    try:
        with urllib.request.urlopen(url) as r:
            r.read()    # all of it: closing early breaks the server's pipe
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def cache_of(url: str) -> str:
    """The Cache-Control a file is sent with."""
    with urllib.request.urlopen(url) as r:
        r.read()
        return r.headers["Cache-Control"]


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bouldermesh-test-"))
    passed = False
    port = free_port()
    B = f"http://127.0.0.1:{port}"
    global ADMIN
    admin_port = free_port()
    ADMIN = f"http://127.0.0.1:{admin_port}"
    env = dict(os.environ, DATA_DIR=str(tmp / "data"), PORT=str(port), HOST="127.0.0.1", HTTP_MIN_INTERVAL="0", ADMIN_PORT=str(admin_port),
               STOP_WAIT_MINUTES="0.2", HTTP_ALLOW_PRIVATE="1")     # the "web photos" are fetched from this server itself
    env.pop("HARVEST_DDG", None)
    srv =subprocess.Popen([sys.executable, str(ROOT / "backend" / "server.py")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = None
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(B + "/api/status", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        run(B, tmp)
        srv.terminate()     # a stop, as Docker sends it: it says what it waited for, and exits cleanly
        out = srv.communicate(timeout=30)[0]
        assert srv.returncode == 0 and "stopping: " in out, (srv.returncode, out[-3000:])
        print("stopped:", [line for line in out.splitlines() if line.startswith("stopping: ")])
        passed = True
    finally:
        if srv.poll() is None:
            srv.terminate()
            try:
                out = srv.communicate(timeout=5)[0]
            except subprocess.TimeoutExpired:
                srv.kill()
        if "Traceback" in (out or ""):
            print("SERVER OUTPUT:\n", out)
        if passed:      # its data is half a GB, in /tmp (memory, on hal): kept only when it failed, to look at
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"its data is kept in {tmp}")


def req(B, method, path, json_body=None, files=None, fields=None):
    data, headers = None, {}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    if files is not None:
        bnd = uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in (fields or {}).items():
            body.write(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for name, fname, blob in files:
            body.write(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\n"
                       f"Content-Type: application/octet-stream\r\n\r\n".encode())
            body.write(blob)
            body.write(b"\r\n")
        body.write(f"--{bnd}--\r\n".encode())
        data = body.getvalue()
        headers["Content-Type"] = f"multipart/form-data; boundary={bnd}"
    r = urllib.request.Request(B + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def simple_img(seed, w=800, h=600) -> bytes:
    # textured like a photo: flat images are rejected as title cards
    im = Image.merge("RGB", [Image.effect_noise((w, h), 40 + seed % 7)] * 3)
    d = ImageDraw.Draw(im)
    for i in range(40):
        x = (seed * 37 + i * 53) % w
        y = (seed * 91 + i * 29) % h
        d.ellipse((x, y, x + 60, y + 40), fill=((i * 40) % 255, (seed * 20) % 255, 120))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return b.getvalue()


def synthetic_photo_set():
    """14 views of one textured surface (+1 re-encoded duplicate, +1 too-small image)."""
    if not HAVE_CV:
        views = [simple_img(i + 10) for i in range(14)]
        # re-encode, so harvest's content-hash filenames don't collapse it into views[0]
        b = io.BytesIO()
        Image.open(io.BytesIO(views[0])).save(b, "JPEG", quality=55)
        dup = b.getvalue()
        small = simple_img(99, 300, 225)
        return views, dup, small
    rng = np.random.default_rng(1)
    base = cv2.normalize(cv2.GaussianBlur((rng.random((1400, 1800)) * 255).astype(np.uint8), (0, 0), 2.5),
                         None, 0, 255, cv2.NORM_MINMAX)

    def view():
        ang = np.deg2rad(rng.uniform(-25, 25))
        sc = rng.uniform(0.6, 1.0)
        M = np.array([[sc * np.cos(ang), -sc * np.sin(ang), rng.uniform(0, 300)],
                      [sc * np.sin(ang), sc * np.cos(ang), rng.uniform(0, 200)],
                      [rng.uniform(-2e-4, 2e-4), rng.uniform(-2e-4, 2e-4), 1]])
        return cv2.imencode(".jpg", cv2.warpPerspective(base, M, (1200, 900), borderValue=128),
                            [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()
    views = [view() for _ in range(14)]
    dup = cv2.imencode(".jpg", cv2.imdecode(np.frombuffer(views[0], np.uint8), 0), [cv2.IMWRITE_JPEG_QUALITY, 55])[1].tobytes()
    small = cv2.imencode(".png", (rng.random((225, 300)) * 255).astype(np.uint8))[1].tobytes()
    return views, dup, small


def run(B: str, tmp: Path) -> None:
    st, s = req(B, "GET", "/api/status")
    assert st == 200
    print("status:", {k: s[k] for k in ("real_pipeline", "opencv", "pillow", "ytdlp")})
    assert s["env"]["HARVEST_DDG"] is True and "ddg" in s["harvest_sources"], s
    assert s["gpu"] in (True, False) and s["gpu_why"], s

    # --- manual climb (kaya link) ------------------------------------------------------------
    st, c = req(B, "POST", "/api/climbs/manual", {"name": "Test Problem", "grade": "V5", "boulder_name": "Test Boulder",
                                                   "lat": 37.74172, "lng": -119.60355,
                                                   "url": "https://kaya-app.kayaclimb.com/climb/test",
                                                   "video": "https://youtu.be/H2EeJEvxpJI"})
    assert st == 201, c
    cid = c["id"]
    assert c["source"] == "kaya" and c["coord_source"] == "manual", c

    st, e = req(B, "POST", "/api/climbs/import", {"url": "https://example.com/x"})
    assert st == 400 and "unsupported" in e["error"], e

    # --- project linked to climb -> upload -> reconstruct -> mesh auto-linked --------------
    st, p = req(B, "POST", "/api/projects", {"name": "test boulder photos", "climb_id": cid})
    pid = p["id"]
    st, r = req(B, "POST", f"/api/projects/{pid}/photos", files=[("photos", f"p{i}.jpg", simple_img(i)) for i in range(3)])
    assert len(r["saved"]) == 3
    st, r = req(B, "POST", f"/api/projects/{pid}/reconstruct")
    assert st == 202, r
    pr = wait_project(B, pid)
    assert pr["status"] == "done", pr["log"]
    assert pr["log"].rstrip().endswith("done") and (tmp / "data" / "projects" / pid / "job.log").exists(), pr["log"]
    st, meshes = req(B, "GET", "/api/meshes")
    m = meshes[0]
    assert m["lat"] == 37.74172 and m["climbs"][0]["id"] == cid, m
    assert abs(time.time() - m["created"]) < 600, m
    print("reconstructed mesh auto-linked:", m["name"], m["lat"], m["lng"])
    t = m["timings"]      # how long it took, and where, kept with it
    assert t["where"] == "this server" and not t["remote"] and 0 <= t["wall"] < 600 and not t["from_log"], t
    assert (tmp / "data" / "meshes" / m["id"] / "timings.json").exists()
    assert m["sources"]["total"] == 3 and [s["source"] for s in m["sources"]["sources"]] == ["upload"], m["sources"]
    st, rec = req(B, "GET", f"/api/meshes/{m['id']}/sources")
    assert st == 200 and {p["file"] for p in rec["photos"]} == {f"p{i}.jpg" for i in range(3)}, rec
    assert m["replaced_by"] is None and req(B, "POST", f"/api/meshes/{m['id']}/make-current")[0] == 409, "not an older version"
    st, vids = req(B, "GET", "/api/videos")
    assert st == 200 and vids == [], vids
    assert req(B, "POST", "/api/campaigns/abcdef123456/harvest-again", {})[0] == 404
    assert req(B, "POST", "/api/campaigns/abcdef123456/rebuild", {"changed_only": True})[0] == 404
    assert req(B, "GET", f"/api/climbs/{cid}")[1]["videos_found"] == []
    st, b = req(B, "GET", f"/api/meshes/{m['id']}/beta")
    assert st == 200 and b["found"] is None and b["marked"] == {"holds": []} and m["beta"] is None, (b, m["beta"])
    assert m["marked"] == 0, m["marked"]
    st, jb = req(B, "GET", "/api/jobs")
    assert st == 200 and set(jb) == {"running", "queued", "area_climbs", "beta", "stopping"} and jb["stopping"] is None, jb
    assert jb["beta"] == {"running": None, "queued": 0, "youtube": None}, jb["beta"]
    st, r = req(B, "POST", "/api/meshes/beta-all")
    assert (st, r) == (202, {"queued": 0}) or st == 501, (st, r)   # a demo mesh keeps no cameras; 501 without the pose model
    assert req(B, "POST", "/api/meshes/beta-stop") == (200, {"unqueued": 0})
    cli = subprocess.run([sys.executable, str(ROOT / "cli.py"), "jobs", "--server", B], capture_output=True, text=True, timeout=60)
    assert cli.returncode in (0, 1) and "restart" in cli.stdout, cli    # 1 if a job is still going
    # deploy.sh's questions: what a restart would cost, taking no new work meanwhile, and carrying on (from here only)
    run_cli = lambda *args: subprocess.run([sys.executable, str(ROOT / "cli.py"), *args, "--server", B], capture_output=True,  # noqa: E731
                                           text=True, timeout=120)
    chk = run_cli("deploy-check")
    assert chk.returncode in (0, 3) and ("restart" in chk.stdout), chk
    st, dep = req(B, "GET", "/api/deploy")
    assert st == 200 and dep["draining"] is False and isinstance(dep["items"], list), dep
    assert run_cli("deploy-drain").returncode == 0 and req(B, "GET", "/api/deploy")[1]["draining"] is True
    assert run_cli("deploy-wait", "--max-minutes", "1").returncode in (0, 1)
    assert run_cli("deploy-drain", "--off").returncode == 0 and req(B, "GET", "/api/deploy")[1]["draining"] is False
    st, saved = req(B, "POST", f"/api/meshes/{m['id']}/holds", {"holds": [{"p": [0.1, 0.2, 0.3], "role": "start"}]})
    assert st == 200 and req(B, "GET", f"/api/meshes/{m['id']}/beta")[1]["marked"]["holds"] == saved["holds"], saved
    assert next(x for x in req(B, "GET", "/api/meshes")[1] if x["id"] == m["id"])["marked"] == 1, "counted in the list"
    assert req(B, "POST", f"/api/meshes/{m['id']}/holds", {"holds": [{"p": [0, 0, 0], "role": "jug"}]})[0] == 400
    assert req(B, "GET", "/api/meshes/abcdef123456/beta")[0] == 404
    assert m["scale"] is None, "a demo mesh keeps no cameras, so isn't sized"
    assert req(B, "POST", f"/api/meshes/{m['id']}/scale")[0] == 409 and req(B, "POST", "/api/meshes/abcdef123456/scale")[0] == 404
    assert req(B, "POST", "/api/meshes/scale-all", {}) == (202, {"queued": 0}), "nothing with cameras to size"
    assert req(B, "POST", f"/api/projects/{pid}/known-object", {"kind": "anvil"})[0] == 400
    assert req(B, "POST", f"/api/projects/{pid}/known-object", [1])[0] == 400
    st, r = req(B, "POST", f"/api/projects/{pid}/known-object", {"kind": "nalgene_wide_32"})
    assert st == 200 and r == {"known_object": {"kind": "nalgene_wide_32"}, "sizing": False}, r   # a demo mesh isn't sized
    assert req(B, "GET", f"/api/projects/{pid}")[1]["known_object"] == {"kind": "nalgene_wide_32"}
    assert req(B, "POST", f"/api/projects/{pid}/known-object", {})[1]["known_object"] is None
    assert req(B, "GET", f"/api/projects/{pid}")[1]["known_object"] is None
    assert "nalgene_wide_32" in req(B, "GET", "/api/status")[1]["known_objects"]

    st, cd = req(B, "GET", f"/api/climbs/{cid}")
    assert cd["meshes"][0]["id"] == m["id"] and cd["projects"][0]["id"] == pid
    assert cd["data"]["videos"][0]["url"].endswith("H2EeJEvxpJI")

    # --- upload mesh + links + patches + geojson ---------------------------------------------
    ply = tmp / "cube.ply"
    pipeline.write_ply(ply, [(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(255, 0, 0)] * 3, [(0, 1, 2)])
    atlas = io.BytesIO()
    Image.new("RGB", (3000, 1000), (200, 40, 40)).save(atlas, "PNG")
    st, up = req(B, "POST", "/api/meshes", files=[("files", "cube.ply", ply.read_bytes()), ("files", "atlas.png", atlas.getvalue())],
                 fields={"name": "uploaded", "climb_id": cid})
    for ask, want, fmt in (("", (3000, 1000), "PNG"), ("?max=2048", (2048, 683), "JPEG"), ("?max=2048", (2048, 683), "JPEG"),   # again: from its copy
                           ("?max=4096", (3000, 1000), "JPEG"), ("?max=8192", (3000, 1000), "JPEG"),     # as a JPEG, however large
                           ("?max=7", (3000, 1000), "PNG"), ("?max=x", (3000, 1000), "PNG")):
        with urllib.request.urlopen(f"{B}/meshes/{up['id']}/atlas.png{ask}") as r:
            im = Image.open(io.BytesIO(r.read()))
        assert im.size == want and im.format == fmt, (ask, im.size, im.format)
    smaller = tmp / "data" / "meshes" / up["id"] / "smaller"
    assert sorted(f.name for f in smaller.iterdir()) == ["atlas-2048.jpg", "atlas-4096.jpg"], "the copy at its own size made once"
    assert cache_of(f"{B}/meshes/{up['id']}/atlas.png?max=2048") == cache_of(f"{B}/meshes/{up['id']}/atlas.png") == "public, max-age=86400"
    with urllib.request.urlopen(f"{B}/meshes/{up['id']}/cube.ply?max=2048") as r:
        assert r.read() == ply.read_bytes(), "only images get shrunk"
    assert st == 201, up
    st, meshes = req(B, "GET", "/api/meshes")
    um = next(x for x in meshes if x["id"] == up["id"])
    assert um["lat"] == 37.74172 and um["climbs"][0]["id"] == cid, um
    assert um["sources"] is None and req(B, "GET", f"/api/meshes/{up['id']}/sources")[0] == 404, "an upload's photos aren't known"
    assert req(B, "POST", f"/api/meshes/{up['id']}/scale")[0] == 409, "nor can it be sized from them"

    # --- what the admin page lets the site show ------------------------------------------------------
    assert um["texture"] == "full" and req(B, "GET", "/api/status")[1]["policy"] == {"textures": "all", "sources": "all"}
    admin_set({"site.textures": "none"})
    assert status_of(f"{B}/meshes/{up['id']}/atlas.png") == 403 and status_of(f"{B}/meshes/{up['id']}/atlas.png?max=2048") == 403
    assert next(x for x in req(B, "GET", "/api/meshes")[1] if x["id"] == up["id"])["texture"] == "hidden"
    admin_set({"site.textures": "permitted", "site.sources": "permitted"})
    assert status_of(f"{B}/meshes/{up['id']}/atlas.png") == 200, "an upload is the user's own"
    assert status_of(f"{B}/photos/{pid}/p0.jpg") == 200, "and so are their photos"
    assert req(B, "GET", f"/api/projects/{pid}")[1]["access"]["p0.jpg"]["show"] == "show"
    admin_set({"site.textures": "all", "site.sources": "all"})
    st, _ = req(B, "DELETE", f"/api/meshes/{up['id']}/links/{cid}")
    st, _ = req(B, "POST", f"/api/meshes/{up['id']}/links", {"climb_id": cid, "note": "west face"})
    assert st == 201
    st, pm = req(B, "PATCH", f"/api/meshes/{up['id']}", {"lat": 1.0, "lng": 2.0})
    assert pm["lat"] == 1.0
    st, pc = req(B, "PATCH", f"/api/climbs/{cid}", {"lat": 37.7, "lng": -119.6})
    assert pc["coord_source"] == "manual" and pc["lat"] == 37.7
    st, gj = req(B, "GET", "/api/geojson")
    layers = sorted(f["properties"]["layer"] for f in gj["features"])
    assert layers == ["climb", "mesh", "mesh"], layers

    # --- hiding a mesh's stray pieces: a main piece of 400 faces and a stray one of 2, in a GLB
    if HAVE_CV:
        def sheet(nx, ny, x0):
            P = [(x0 + i, float(j), 0.0) for i in range(nx + 1) for j in range(ny + 1)]
            corner = lambda i, j: i * (ny + 1) + j  # noqa: E731
            I = [v for i in range(nx) for j in range(ny) for v in (corner(i, j), corner(i + 1, j), corner(i, j + 1), corner(i + 1, j), corner(i + 1, j + 1), corner(i, j + 1))]
            return P, I
        P, I = sheet(20, 10, 0.0)
        P2, I2 = sheet(1, 1, 50.0)
        I = I + [v + len(P) for v in I2]
        P = P + P2
        pos, idx = np.array(P, "<f4"), np.array(I, "<u4")
        blob = pos.tobytes() + idx.tobytes()
        gltf = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}], "buffers": [{"byteLength": len(blob)}],
                "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": pos.nbytes}, {"buffer": 0, "byteOffset": pos.nbytes, "byteLength": idx.nbytes}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3", "min": [0, 0, 0], "max": [51, 10, 0]},
                              {"bufferView": 1, "componentType": 5125, "count": idx.size, "type": "SCALAR"}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}]}
        text = json.dumps(gltf).encode()
        text += b" " * (-len(text) % 4)
        pad = blob + b"\0" * (-len(blob) % 4)
        glb = struct.pack("<III", 0x46546C67, 2, 28 + len(text) + len(pad)) + struct.pack("<II", len(text), 0x4E4F534A) + text + struct.pack("<II", len(pad), 0x004E4942) + pad
        st, up = req(B, "POST", "/api/meshes", files=[("files", "my-rock.glb", glb)], fields={"name": "with a stray piece"})
        assert st == 201, up
        mid = up["id"]
        st, none = req(B, "GET", f"/api/meshes/{mid}/cleanup")
        assert st == 404, "uploads aren't cleaned up unless asked"
        st, rec = req(B, "POST", f"/api/meshes/{mid}/cleanup", {})
        assert st == 200 and [p["faces"] for p in rec["pieces"]] == [400, 2] and rec["removed"] == [1], rec
        st, listed = req(B, "GET", "/api/meshes")
        c = [m for m in listed if m["id"] == mid][0]["cleanup"]
        assert (c["pieces"], c["hidden"], c["hidden_faces"], c["faces"], c["edited"]) == (2, 1, 2, 402, False), c
        with urllib.request.urlopen(f"{B}/meshes/{mid}/cleanup_faces.bin") as r:
            assert len(r.read()) == 2 * 402
        with urllib.request.urlopen(f"{B}/meshes/{mid}/my-rock.glb?download=1&cleaned=1") as r:
            cleaned = r.read()
            assert "mesh.cleaned.glb" in r.headers["Content-Disposition"] and len(cleaned) < len(glb), r.headers
        with urllib.request.urlopen(f"{B}/meshes/{mid}/my-rock.glb") as r:
            assert r.read() == glb, "the mesh's own file doesn't change"
        if glbpack.available():     # its packed copy, which the list says it has once it's made
            for _ in range(100):
                packed = next(m for m in req(B, "GET", "/api/meshes")[1] if m["id"] == mid)["pack"]
                if packed:
                    break
                time.sleep(0.1)
            assert packed and packed["full"] and packed["preview"] is None and packed["texture"] is None, packed
            with urllib.request.urlopen(f"{B}/meshes/{mid}/my-rock.glb?lod=full") as r:
                small = r.read()
            assert len(small) == packed["full"] and b"EXT_meshopt_compression" in small[:2000]
            with urllib.request.urlopen(f"{B}/meshes/{mid}/cleanup_faces.bin?lod=full") as r:
                order = np.frombuffer(r.read(), "<u2")
            with urllib.request.urlopen(f"{B}/meshes/{mid}/cleanup_faces.bin") as r:
                own = np.frombuffer(r.read(), "<u2")
            assert len(order) == 402 and (order == 1).sum() == 2 and np.array_equal(np.sort(order), np.sort(own)), "its pieces, in its order"
            assert status_of(f"{B}/meshes/{mid}/my-rock.glb?lod=preview") == 404, "too small for a preview"
            assert status_of(f"{B}/meshes/{mid}/my-rock.glb?lod=huge") == 400

            def packed_again(test):     # the admin page's preview size changed: made again, and shown as before meanwhile
                for _ in range(100):
                    now = next(m for m in req(B, "GET", "/api/meshes")[1] if m["id"] == mid)
                    assert now["pack"], "still shown packed while it's made again"
                    if test(now["pack"]):
                        return now
                    time.sleep(0.1)
                raise AssertionError(now)
            before = next(m for m in req(B, "GET", "/api/meshes")[1] if m["id"] == mid)["v"]
            admin_set({"viewer.preview_faces": 100})
            again = packed_again(lambda now: now["preview"])
            assert again["v"] != before and again["pack"]["full"] == packed["full"], (again, packed)
            assert cache_of(f"{B}/meshes/{mid}/cleanup_faces.bin?lod=preview&v={before}") == "no-cache", "not this version's"
            with urllib.request.urlopen(f"{B}/meshes/{mid}/cleanup_faces.bin?lod=preview&v={again['v']}") as r:
                preview = np.frombuffer(r.read(), "<u2")
                assert r.headers["Cache-Control"] == "public, max-age=31536000, immutable", r.headers["Cache-Control"]
            assert 50 <= len(preview) < 402 and set(preview.tolist()) <= {0, 1}, len(preview)
            assert status_of(f"{B}/meshes/{mid}/my-rock.glb?lod=preview&v={again['v']}") == 200
            admin_set({"viewer.preview_faces": 0})
            packed_again(lambda now: now["preview"] is None)
            assert status_of(f"{B}/meshes/{mid}/my-rock.glb?lod=preview") == 404, "no previews at all"
            with urllib.request.urlopen(ADMIN + "/api/admin/state") as r:
                progress = json.load(r)["packing"]
            assert progress["available"] and progress["total"] == progress["done"] >= 1 and progress["preview_faces"] == 0, progress
            assert progress["left"] == 0 and progress["eta_seconds"] is None, progress
            print("packed copies OK")
        # --- a mesh's version: its files kept a year at an address with it, until anything it's sent changes
        version = lambda: next(m for m in req(B, "GET", "/api/meshes")[1] if m["id"] == mid)["v"]  # noqa: E731
        v1 = version()
        assert cache_of(f"{B}/meshes/{mid}/my-rock.glb?v={v1}") == "public, max-age=31536000, immutable"
        assert cache_of(f"{B}/meshes/{mid}/cleanup_faces.bin?v={v1}") == "public, max-age=31536000, immutable"
        assert cache_of(f"{B}/meshes/{mid}/my-rock.glb") == "public, max-age=3600", "without it, an hour"
        assert cache_of(f"{B}/meshes/{mid}/my-rock.glb?v=0123456789ab") == "no-cache", "another version's: not kept"
        assert cache_of(f"{B}/meshes/{mid}/my-rock.glb?download=1&v={v1}") == "public, max-age=3600"
        st, _ = req(B, "PATCH", f"/api/meshes/{mid}/cleanup", {"removed": []})
        assert version() == v1, "which pieces are hidden comes from the API: its files stay as they were"
        st, _ = req(B, "DELETE", f"/api/meshes/{mid}/cleanup")
        v2 = version()
        assert v2 != v1 and cache_of(f"{B}/meshes/{mid}/my-rock.glb?v={v1}") == "no-cache", "its cleanup went"
        st, _ = req(B, "POST", f"/api/meshes/{mid}/cleanup", {})
        assert version() not in (v1, v2), "its pieces found again"
        print("mesh versions OK")
        # --- the meshes around one: this one 20 m north and 10 m east of the uploaded PLY's point
        assert req(B, "PATCH", f"/api/meshes/{um['id']}", {"lat": 34.0, "lng": -116.0})[0] == 200
        assert req(B, "PATCH", f"/api/meshes/{mid}", {"lat": 34.0 + 20 / 110574, "lng": -116.0 + 10 / (111320 * 0.8290376)})[0] == 200
        st, near = req(B, "GET", f"/api/meshes/{um['id']}/nearby?radius=50")
        assert st == 200 and [n["id"] for n in near["meshes"]] == [mid] and near["self"]["heading"] is None, near
        n = near["meshes"][0]
        assert abs(n["north_m"] - 20) < .1 and abs(n["east_m"] - 10) < .1 and abs(n["distance_m"] - 22.4) < .1 and n["v"], n
        assert req(B, "GET", f"/api/meshes/{um['id']}/nearby?radius=15")[1]["meshes"] == []
        for bad in ("2", "5000", "far"):
            assert req(B, "GET", f"/api/meshes/{um['id']}/nearby?radius={bad}")[0] == 400, bad
        assert req(B, "GET", "/api/meshes/000000000000/nearby")[0] == 404
        # which way it faces: turned by hand, and back
        st, h = req(B, "PATCH", f"/api/meshes/{mid}/heading", {"deg": 400})
        assert st == 200 and h["heading"] == {"deg": 40.0, "source": "edited", "detail": "turned by hand"}, h
        assert req(B, "GET", f"/api/meshes/{um['id']}/nearby?radius=50")[1]["meshes"][0]["heading"]["deg"] == 40.0
        assert req(B, "PATCH", f"/api/meshes/{mid}/heading", {"deg": "east"})[0] == 400
        st, h = req(B, "PATCH", f"/api/meshes/{mid}/heading", {"deg": None})
        assert st == 200 and h["heading"] is None and req(B, "GET", f"/api/meshes/{mid}/heading")[1]["heading"] is None, h
        print("nearby and facing OK")
        st, bad = req(B, "PATCH", f"/api/meshes/{mid}/cleanup", {"removed": [0, 1]})
        assert st == 400 and "at least one" in bad["error"], bad
        st, rec = req(B, "PATCH", f"/api/meshes/{mid}/cleanup", {"removed": []})
        assert st == 200 and rec["removed"] == [], rec
        with urllib.request.urlopen(f"{B}/meshes/{mid}/my-rock.glb?cleaned=1") as r:
            assert r.read() == glb, "nothing hidden: the original"
        st, _ = req(B, "DELETE", f"/api/meshes/{mid}/cleanup")
        st, rec = req(B, "GET", f"/api/meshes/{mid}/cleanup")
        assert st == 200 and rec["deleted"], rec
        st, _ = req(B, "PATCH", f"/api/meshes/{mid}/cleanup", {"removed": [1]})
        assert st == 400
        st, rec = req(B, "POST", f"/api/meshes/{mid}/cleanup", {})
        assert st == 200 and rec["removed"] == [1], rec
        st, _ = req(B, "DELETE", f"/api/meshes/{mid}")
        print("mesh cleanup routes OK")
    st, nomesh = req(B, "POST", "/api/meshes/000000000000/cleanup", {})
    assert st == 404, nomesh

    # --- mesh a whole area: the routes only, as starting one would read Mountain Project ------
    export = ('Route,Location,URL,"Avg Stars","Your Stars","Route Type",Rating,Pitches,Length,"Area Latitude","Area Longitude"\n'
              'One,"Rock > * Bouldering > Park",https://www.mountainproject.com/route/1/one,3.5,-1,Boulder,V1,,,34.0,-116.0\n'
              'Two,"Other > * Bouldering > Park",https://www.mountainproject.com/route/2/two,3.9,-1,Boulder,V2,,,34.0,-116.0\n')
    st, bad = req(B, "POST", "/api/campaigns", {"area": "Joshua Tree"})
    assert st == 400 and "Mountain Project area URL" in bad["error"], bad
    st, ca = req(B, "POST", "/api/campaigns", {"csv": export, "order": "rating", "start": False})
    assert st == 201 and ca["name"] == "Bouldering" and ca["status"] == "stopped" and ca["counts"] == {"pending": 2}, ca
    assert [i["name"] for i in ca["next"]] == ["Two", "One"], ca["next"]
    st, lst = req(B, "GET", "/api/campaigns")
    assert st == 200 and [c["id"] for c in lst] == [ca["id"]], lst
    st, stopped = req(B, "POST", f"/api/campaigns/{ca['id']}/stop", {"now": True})
    assert st == 200 and stopped["status"] == "stopped", stopped
    st, _ = req(B, "POST", "/api/campaigns/nothere0/start", {})
    assert st == 404, st
    st, none = req(B, "POST", f"/api/campaigns/{ca['id']}/retry", {})
    assert st == 200 and none["queued"] == 0 and none["status"] == "stopped", none
    st, _ = req(B, "POST", "/api/campaigns/nothere0/retry", {})
    assert st == 404, st
    st, _ = req(B, "POST", "/api/projects/0123456789ab/retry", {})
    assert st == 404, st
    st, _ = req(B, "DELETE", f"/api/campaigns/{ca['id']}")
    st, lst = req(B, "GET", "/api/campaigns")
    assert st == 200 and lst == [], lst

    # --- kaya export -------------------------------------------------------------------------
    csv_blob = b"Climb,Grade,Area,Latitude,Longitude,Beta\nThe Egg,V11,Index,47.82,-121.55,https://youtu.be/abcdefghijk\nNo Coords,V2,Index,,,\n"
    st, ke = req(B, "POST", "/api/climbs/kaya-export", files=[("file", "kaya.csv", csv_blob)])
    assert st == 201 and len(ke["imported"]) == 2, ke
    st, lst = req(B, "GET", "/api/climbs?q=egg")
    assert len(lst) == 1 and lst[0]["name"] == "The Egg"
    assert [c["name"] for c in req(B, "GET", "/api/climbs?q=" + urllib.parse.quote("index egg"))[1]] == ["The Egg"], "each word, anywhere"
    assert [c["name"] for c in req(B, "GET", "/api/climbs?q=" + urllib.parse.quote("problem test"))[1]] == ["Test Problem"]
    assert req(B, "GET", "/api/climbs?q=" + urllib.parse.quote("egg nowhere"))[1] == [], "every word has to match"
    assert [c["name"] for c in req(B, "GET", "/api/climbs?q=v11")[1]] == ["The Egg"], "grades too"
    n = req(B, "GET", "/api/climbs/count")[1]
    assert n["climbs"] == len(req(B, "GET", "/api/climbs")[1]) >= 3 and n["doubtful"] == 0, n
    assert req(B, "GET", "/api/climbs?doubtful=1")[1] == [] and all(c["coords_doubt"] is None for c in req(B, "GET", "/api/climbs")[1])
    st, only = req(B, "GET", "/api/climbs?meshed=1")
    st, every = req(B, "GET", "/api/climbs")
    assert st == 200 and [c["id"] for c in only] == [cid] and len(every) > 1 and all(c["n_links"] for c in only), (only, len(every))

    # --- harvest: climb photos served by this very server stand in for web photos ------------
    views, dup, small = synthetic_photo_set()
    photos = [("photos", f"h{i}.jpg", v) for i, v in enumerate(views)] + [("photos", "dup.jpg", dup), ("photos", "tiny.png", small)]
    st, hp = req(B, "POST", "/api/projects", {"name": "photo host"})
    st, _ = req(B, "POST", f"/api/projects/{hp['id']}/photos", files=photos)
    con = sqlite3.connect(tmp / "data" / "bouldermesh.sqlite")
    data = json.loads(con.execute("SELECT data FROM climbs WHERE id=?", (cid,)).fetchone()[0])
    data["images"] = [{"url": f"{B}/photos/{hp['id']}/{f}", "kind": "image", "page": None, "title": f, "credit": None,
                       "license": "test", "width": None, "height": None, "source": "test"} for _, f, _ in photos]
    con.execute("UPDATE climbs SET data=? WHERE id=?", (json.dumps(data), cid))
    con.commit()
    con.close()
    st, hv = req(B, "POST", f"/api/climbs/{cid}/harvest", {"sources": ["climb", "google", "youtube"], "reconstruct": True})
    assert st == 202, hv
    pr = wait_project(B, hv["project_id"], timeout=300)
    assert pr["status"] == "done", pr["status"] + "\n" + pr["log"]
    h = pr["harvest"]
    print("harvest:", h["images"], "images; verdict", h["diversity"]["verdict"], "unique", h["diversity"]["unique_images"])
    assert pr["progress"]["overall"] == 100 and {s["state"] for s in pr["progress"]["steps"]} == {"done"}, pr["progress"]
    assert [s["key"] for s in pr["progress"]["steps"]][:3] == ["find", "download", "video"], pr["progress"]
    assert all(s["frac"] == 1 for s in pr["progress"]["steps"]), pr["progress"]
    assert h["diversity"]["unique_images"] == 14, h["diversity"]
    if HAVE_CV:
        assert h["diversity"]["verdict"] == "good" and h["diversity"]["view_graph"]["largest_component"] >= 12, h["diversity"]
    rej = sorted(p.name for p in (tmp / "data" / "projects" / hv["project_id"] / "rejected").iterdir())
    assert any(n.startswith("dup_") for n in rej) and any(n.startswith("small_") for n in rej), rej
    st, meshes = req(B, "GET", "/api/meshes")
    hm = [x for x in meshes if x["source"] == f"project:{hv['project_id']}"][0]
    assert hm["climbs"][0]["id"] == cid and hm["lat"] == 37.7, hm
    print("ALL API TESTS PASSED")


def wait_project(B, pid, timeout=60):
    for _ in range(int(timeout * 2)):
        time.sleep(0.5)
        st, pr = req(B, "GET", f"/api/projects/{pid}")
        if pr["status"] in ("done", "failed", "harvested"):
            return pr
    raise TimeoutError(pid)


if __name__ == "__main__":
    main()
