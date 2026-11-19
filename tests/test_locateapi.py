#!/usr/bin/env python3
"""Lining a mesh up with no taps on the server (locateapi.py): the request's checks, the poses' form, the limits, the
packs' queue and the heavier way, with place() and build_pack() stood in for; and, when a mesh with a kept
reconstruction and its photos is given (LOCATE_MESH=<mesh folder> LOCATE_PHOTOS=<photos folder>), its own photos
placed back where its model has them, by locate.py."""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import tarfile
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import locateapi as la  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp(prefix="bouldermesh-locate-"))
rng = np.random.default_rng(3)


def jpeg(w=640, h=480) -> str:
    img = (rng.random((h, w)) * 255).astype(np.uint8)
    return base64.b64encode(cv2.imencode(".jpg", img)[1].tobytes()).decode()


def refused(f, *a, code=None):
    try:
        f(*a)
    except la.Refused as e:
        assert code is None or e.code == code, (e.code, e.why)
        return e
    raise AssertionError("not refused")


# --- the request ------------------------------------------------------------------------------------------------------
lens = {"fx": 1000, "fy": 1000, "cx": 640, "cy": 480, "w": 1280, "h": 960}     # given for twice the picture's size
got = la.frames_of({"frames": [{"id": 1, "jpeg_b64": jpeg(), "lens": lens}]})
assert got[0]["grey"].shape == (480, 640) and got[0]["lens"] == {"fx": 500, "fy": 500, "cx": 320, "cy": 240, "w": 640, "h": 480}, got[0]["lens"]
refused(la.frames_of, {"frames": []}, code=400)
refused(la.frames_of, {"frames": [{"id": i, "jpeg_b64": jpeg(), "lens": lens} for i in range(5)]}, code=400)
refused(la.frames_of, {"frames": [{"id": 1, "jpeg_b64": jpeg(), "lens": lens}] * 2}, code=400)
refused(la.frames_of, {"frames": [{"id": 1, "jpeg_b64": jpeg(), "lens": {**lens, "fx": -1}}]}, code=400)
refused(la.frames_of, {"frames": [{"id": 1, "jpeg_b64": jpeg(), "lens": {**lens, "w": 960}}]}, code=400)     # not its shape
refused(la.frames_of, {"frames": [{"id": 1, "jpeg_b64": base64.b64encode(b"GIF89a....").decode(), "lens": lens}]}, code=400)
refused(la.frames_of, {"frames": [{"id": 1, "jpeg_b64": "not base64!", "lens": lens}]}, code=400)
refused(la.frames_of, {"frames": [{"id": 1, "jpeg_b64": "A" * (la.MAX_JPEG * 2), "lens": lens}]}, code=413)
print("request OK")


# --- poses: COLMAP's world to camera, answered as the camera's centre and its turn to the mesh -------------------------
def rot(axis, deg):
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    return cv2.Rodrigues(a * np.radians(deg))[0]


for R in (np.eye(3), rot([0, 1, 0], 180), rot([1, 2, 3], 130), rot([1, 0, 0], -179), rot([0, 0, 1], 90)):
    C = np.array([1.0, -2.0, 3.0])
    a = la.pose_answer({"R": R.tolist(), "t": (-R @ C).tolist(), "inliers": 40, "rms_px": 1.25})
    w, x, y, z = a["q"]
    Q = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                  [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                  [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])
    assert np.allclose(Q, R.T, atol=1e-6) and np.allclose(a["c"], C) and w >= 0, (R, a)
    p = np.array([0.3, 0.1, 9.0])           # a point seen by the camera lands the same either way
    assert np.allclose(R @ p + (-R @ C), Q.T @ (p - np.array(a["c"])), atol=1e-5)
print("poses OK")

# --- limits -----------------------------------------------------------------------------------------------------------
now = [0.0]
lim = la.Limits(per_minute=3, at_once=2, clock=lambda: now[0])
lim.begin("a")
assert refused(lim.begin, "a", code=429).extra["retry_s"] == 1, "one at a time"
lim.begin("b")
refused(lim.begin, "c", code=503)                   # two at once from everyone
lim.end("a"), lim.end("b")
lim.begin("a"); lim.end("a"); lim.begin("a"); lim.end("a")
e = refused(lim.begin, "a", code=429)                # 3 a minute
assert 55 <= e.extra["retry_s"] <= 61, e.extra
now[0] = 61
lim.begin("a"); lim.end("a")
print("limits OK")


# --- packs: made in the background, one queue, again when the reconstruction is newer ---------------------------------
def mesh(name: str, photos=("a.jpg", "b.jpg")) -> pathlib.Path:
    d = tmp / name
    (d / "model").mkdir(parents=True)
    import struct
    data = struct.pack("<Q", len(photos))
    for n in photos:
        data += struct.pack("<I7dI", 1, 1, 0, 0, 0, 0, 0, 0, 1) + n.encode() + b"\0" + struct.pack("<Q", 1) + struct.pack("<ddq", 1, 2, -1)
    (d / "model" / "images.bin").write_bytes(data)
    with tarfile.open(d / "reconstruction.tar.gz", "w:gz") as t:
        t.add(d / "model" / "images.bin", "model/images.bin")
    return d


meshes = {"m1": mesh("m1"), "m2": mesh("m2")}
built, slow = [], [0.0]


def resolve(mid, photos):
    if mid not in meshes:
        raise la.Refused(404, "not found")
    return meshes[mid], [tmp / "photos"] if photos else []


def build(mesh_dir, dirs, out, **opts):
    time.sleep(slow[0])
    built.append((mesh_dir.name, pathlib.Path(out).name, opts))
    if mesh_dir.name == "m2":
        raise ValueError("no photos placed")
    (mesh_dir / (out + ".bin")).write_bytes(json.dumps({"mesh": mesh_dir.name, "opts": opts}).encode())


packs = la.Packs(resolve, build, lambda p: json.loads(p.read_bytes()), log=lambda s: None)


def wait():
    for _ in range(100):
        if not packs.making:
            return
        time.sleep(0.05)
    raise AssertionError("still making")


assert packs.get("m1") is None and packs.path("m1") is None, "queued, not made twice"
wait()
assert packs.get("m1") == {"mesh": "m1", "opts": {}} and built == [("m1", "locate", {})], built
assert la.pack.__doc__ and packs.path("m1").name == "locate.bin"
assert packs.get("m2") is None
wait()
assert "no photos placed" in refused(packs.get, "m2", code=409).why, "a pack that couldn't be made isn't tried again..."
refused(packs.get, "nope", code=404)
(meshes["m1"] / "locate.bin").write_bytes(b"not a pack")     # one it can't read is made again
assert packs.get("m1") is None
wait()
assert packs.get("m1") == {"mesh": "m1", "opts": {}}
n = len(built)
time.sleep(0.02)
(meshes["m1"] / "reconstruction.tar.gz").touch()    # ...till its reconstruction changes; nor is an older pack used
assert packs.get("m1") is None
wait()
assert len(built) == n + 1 and packs.get("m1") is not None
(meshes["m2"] / "reconstruction.tar.gz").unlink()
assert "no reconstruction" in refused(packs.get, "m2", code=409).why
print("packs OK")

# --- the answer: placed as a phone would, then the heavier way -------------------------------------------------------
R, C = rot([0, 1, 0], 30), np.array([0.0, 1.0, -4.0])
POSE = {"R": R.tolist(), "t": (-R @ C).tolist(), "inliers": 80, "rms_px": 1.5, "matched": 200}
calls = []


def place(pk, grey, lens, **opts):
    """Placed as a phone would, frame 1; the heavier way, frame 2 as well; neither, frame 3: told apart by a white corner."""
    calls.append(opts)
    one, two = grey[:16, :16].mean() > 200, grey[:16, 16:32].mean() > 200
    return POSE if one or (two and opts) else None


def frame(fid):
    img = (rng.random((480, 640)) * 150).astype(np.uint8)
    if fid in (1, 2):
        img[:16, 16 * (fid - 1):16 * fid] = 255
    return {"id": fid, "jpeg_b64": base64.b64encode(cv2.imencode(".jpg", img)[1].tobytes()).decode(),
            "lens": {"fx": 500, "fy": 500, "cx": 320, "cy": 240, "w": 640, "h": 480}}


meshes["m3"] = mesh("m3")
lim = la.Limits()
body = {"frames": [frame(1), frame(2), frame(3)]}
got, code = la.answer("m3", body, "x", packs, lim, place)
assert code == 503 and got["retry_s"] >= 5, (code, got)
wait()
calls.clear()
got, code = la.answer("m3", body, "x", packs, lim, place)
assert code == 200 and [(a["id"], a["how"]) for a in got["located"]] == [(1, "phone"), (2, "more")], got
assert np.allclose(got["located"][0]["c"], C) and got["located"][0]["inliers"] == 80 and isinstance(got["ms"], int)
assert calls == [{}] * 3 + [la.MORE_OPTS] * 2, calls
assert not lim.busy, "its turn ended"
assert la.answer("m3", {"frames": []}, "x", packs, lim, place)[1] == 400
la.BUDGET_S = 0.0       # no time left: not tried the heavier way
got, _ = la.answer("m3", body, "x", packs, lim, place)
assert [a["how"] for a in got["located"]] == ["phone"], got
la.BUDGET_S = 6.0
print("answer OK")

# --- what a mesh needs ------------------------------------------------------------------------------------------------
(tmp / "ph").mkdir()
(tmp / "ph" / "a.jpg").write_bytes(b"x")
r = la.readiness(meshes["m1"], tmp / "ph")
assert not r["ok"] and r["placed"] == 2 and r["here"] == 1 and r["missing"] == ["b.jpg"], r
assert la.readiness(meshes["m1"], tmp / "ph", away={"b.jpg"})["ok"]
assert la.readiness(meshes["m2"], tmp / "ph")["why"] == "no reconstruction kept"
print("readiness OK")

# --- a real mesh: its own photos placed back where its model has them --------------------------------------------------
BACKUP = pathlib.Path("/data/tom/bouldermesh-backups/current/data")     # hal's copy of the server's data, if here
real_mesh = os.environ.get("LOCATE_MESH") or (str(BACKUP / "meshes/102cf17adbd0") if (BACKUP / "meshes/102cf17adbd0").is_dir() else "")
if real_mesh:
    import shutil
    import struct
    import locate
    import reconstruction
    from modelcheck import read_model
    md = pathlib.Path(real_mesh)
    photos = pathlib.Path(os.environ.get("LOCATE_PHOTOS") or BACKUP / "projects" / json.loads((md / "inputs.json").read_text())["project"] / "photos")
    work = tmp / "real"
    shutil.copytree(md, work / "mesh", ignore=shutil.ignore_patterns("locate*", "*.glb", "*.png", "thumb", "beta*"))
    reconstruction.unpack(work / "mesh", work / "rec")
    truth = {}
    with open(work / "rec" / "model" / "images.bin", "rb") as f:     # each photo's t (world to camera)
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            v = struct.unpack("<I7dI", f.read(64))
            name = b""
            while (c := f.read(1)) != b"\0":
                name += c
            f.seek(24 * struct.unpack("<Q", f.read(8))[0], 1)
            truth[name.decode()] = np.array(v[5:8])
    cams, _, _ = read_model(work / "rec" / "model")
    held = sorted(n for n in cams if (photos / n).is_file())[1::4][:8]      # left out of the pack, then placed against it
    spread = np.ptp(np.array([-cams[n]["R"].T @ truth[n] for n in cams]), axis=0).max()
    real = la.Packs(lambda mid, ph: (work / "mesh", [photos] if ph else []),
                    lambda m, dirs, out, **k: locate.build_pack(m, dirs, out, exclude=set(held), **k), locate.load_pack)
    t0 = time.time()
    assert real.get("x") is None
    while real.making:
        time.sleep(0.5)
    print(f"real mesh {md.name}: pack made in {time.time() - t0:.0f} s, {real.path('x').stat().st_size // 1024} KB")
    def place_one(n) -> bool | None:
        """Place a held-out photo: whether where its model has it (None: not placed)."""
        img = cv2.imread(str(photos / n), cv2.IMREAD_GRAYSCALE)
        k = min(1.0, 1280 / max(img.shape))
        img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else img
        c = cams[n]
        sx = img.shape[1] / c["w"]
        lens = {"fx": c["f"] * sx, "fy": c["f"] * sx, "cx": c["cx"] * sx, "cy": c["cy"] * sx, "w": img.shape[1], "h": img.shape[0]}
        body = {"frames": [{"id": 0, "jpeg_b64": base64.b64encode(cv2.imencode(".jpg", img)[1].tobytes()).decode(), "lens": lens}]}
        got, code = la.answer("x", body, "t", real, la.Limits(per_minute=99), locate.place)
        assert code == 200, got
        if not got["located"]:
            print(f"  {n}: not placed ({got['ms']} ms)")
            return None
        a = got["located"][0]
        w, x, y, z = a["q"]
        Q = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                      [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                      [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])
        deg = np.degrees(np.arccos(np.clip((np.trace(Q @ c["R"]) - 1) / 2, -1, 1)))
        off = np.linalg.norm(np.array(a["c"]) + c["R"].T @ truth[n]) / spread
        print(f"  {n}: {a['inliers']} inliers, {a['rms_px']} px, {deg:.2f}° and {off * 100:.2f}% of the cameras' spread off, "
              f"{got['ms']} ms ({a['how']})")
        assert deg < 5 and off < 0.05, "placed far from where the model has it"
        return bool(deg < 1 and off < 0.02)

    ok = sum(bool(place_one(n)) for n in held)     # within 1°: the model's own cameras of web photos are only so good
    assert ok >= len(held) * 2 // 3, f"only {ok} of {len(held)} placed where the model has them"
    print(f"real mesh OK: {ok} of {len(held)} placed")

print("locateapi OK")
