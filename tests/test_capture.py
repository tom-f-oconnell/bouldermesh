#!/usr/bin/env python3
"""What the phone apps are given of a mesh (captureplan.py), and what a mesh learns from the photos they take
(phonedata.py): offline, on a made-up rock."""
from __future__ import annotations

import json
import math
import os
import pathlib
import struct
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = pathlib.Path(tempfile.mkdtemp(prefix="bouldermesh-capture-"))
os.environ["DATA_DIR"] = str(tmp)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import captureplan  # noqa: E402
import orientation  # noqa: E402
import phonedata  # noqa: E402
import scale  # noqa: E402

rng = np.random.default_rng(7)


def quat(R) -> list[float]:
    """A rotation matrix as w, x, y, z."""
    w = math.sqrt(max(0.0, 1 + R[0, 0] + R[1, 1] + R[2, 2])) / 2
    if w > 1e-6:
        return [w, (R[2, 1] - R[1, 2]) / (4 * w), (R[0, 2] - R[2, 0]) / (4 * w), (R[1, 0] - R[0, 1]) / (4 * w)]
    i = int(np.argmax(np.diag(R)))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(max(1e-12, 1 + R[i, i] - R[j, j] - R[k, k])) * 2
    q = [0.0, 0.0, 0.0, 0.0]
    q[0], q[1 + i], q[1 + j], q[1 + k] = (R[k, j] - R[j, k]) / s, s / 4, (R[j, i] + R[i, j]) / s, (R[k, i] + R[i, k]) / s
    return q


def camera(name: str, centre, target, f: float = 3000.0, size=(4000, 3000)) -> dict:
    """A photo placed at centre, looking at target, upright: as cameras.json has them (world to camera, +Z the way it looks)."""
    c, fwd = np.asarray(centre, float), np.asarray(target, float) - np.asarray(centre, float)
    fwd = fwd / np.linalg.norm(fwd)
    right = np.cross(fwd, [0.0, 1.0, 0.0])
    right = right / np.linalg.norm(right)
    R = np.array([right, np.cross(fwd, right), fwd])
    return {"name": name, "q": quat(R), "t": list(-R @ c), "lens": {"model": 2, "width": size[0], "height": size[1],
                                                                  "params": [f, size[0] / 2, size[1] / 2, 0.0]}}


# --- a wall of rock: part of a cylinder 2 units in radius and 3 tall, facing +Z, open at its sides and top -------------
A, H = 25, 16
angles, heights = np.linspace(-1.0, 1.0, A), np.linspace(0.0, 3.0, H)
V = np.array([[2 * math.sin(a), y, 2 * math.cos(a)] for a in angles for y in heights])
F = []
for i in range(A - 1):
    for j in range(H - 1):
        a, b, c, d = i * H + j, (i + 1) * H + j, (i + 1) * H + j + 1, i * H + j + 1
        F += [[a, b, c], [a, c, d]]
F = np.array(F)
C, N, area = captureplan.geometry(V, F)
assert float((N * C * [1, 0, 1]).sum(axis=1).min()) > 0, "its faces face outward"
cams = [camera(f"cap_{i:03d}.jpg", [6 * math.sin(a), 1.5, 6 * math.cos(a)], [0, 1.5, 0]) for i, a in enumerate(np.linspace(-0.5, 0.5, 9))]
best, views = captureplan.coverage(C, N, cams)
front = np.abs(np.arctan2(C[:, 0], C[:, 2])) < 0.4
assert np.isfinite(best[front]).all() and views[front].min() >= 2, "what faces the cameras is seen, from more than one direction"
# 4 units off, 3000 pixels of focal length: about 1.3 thousandths of a unit a pixel, more where it's seen at a slant
assert 0.0011 < float(np.median(best[front])) < 0.0018, float(np.median(best[front]))
behind = [camera("back.jpg", [0, 1.5, -6], [0, 1.5, 0])]
assert not np.isfinite(captureplan.coverage(C, N, behind)[0]).any(), "nothing is seen from behind: its faces face away"
print("coverage OK")

holds = [{"p": [0.0, 1.0, 2.0], "role": "start", "from": "video", "climbs": ["c1"]},
         {"p": [0.02, 1.0, 2.0], "role": "start", "from": "marked", "climbs": []},        # the same hold, marked too
         {"p": [0.5, 2.2, 1.936], "role": "finish", "from": "video", "climbs": ["c1"]}]
plan, guide = captureplan.build(V, F, cams, holds, [{"id": "c1", "name": "Alpha", "grade": "V2", "view": None}], 1.2)
T = plan["targets"]
kinds = {k: [t for t in T if t["kind"] == k] for k in ("patch", "hold", "edge")}
assert len(kinds["hold"]) == 2 and all(t["prio"] == 3 and t["climbs"] == ["c1"] for t in kinds["hold"]), kinds["hold"]
assert all(t["have_gsd"] and t["have_views"] >= 2 for t in kinds["hold"]), kinds["hold"]
assert kinds["edge"] and all(t["out"][1] > -0.4 for t in kinds["edge"]), "its open sides and top are edges, its foot isn't"
near = [t for t in kinds["patch"] if t["prio"] == 2]
assert near and all(min(np.linalg.norm(np.array(t["p"]) - np.array(h["p"])) for h in holds) * 1.2 < 1.5 for t in near), "by a hold"
assert any(t["prio"] == 1 for t in kinds["patch"]), "the rest is rock all the same"
assert plan["frame"]["metres_per_unit"] == 1.2 and not plan["frame"]["scale_guessed"] and plan["need"]["hold"]["gsd_mm"] < plan["need"]["patch"]["gsd_mm"]
magic, nv, nf, npatch = struct.unpack("<4sIII", guide[:16])
assert magic == b"BMG1" and (nv, nf) == (len(V), len(F)) and npatch == len(kinds["patch"])
assert len(guide) == 16 + nv * 12 + nf * 12 + nf * 2 + nf * 4 + nf
patch_of = np.frombuffer(guide, "<u2", nf, 16 + nv * 12 + nf * 12)
assert set(patch_of.tolist()) - {65535} == set(range(npatch)), "every patch has faces"
guessed, _ = captureplan.build(V, F, cams, [], [], None)
assert guessed["frame"]["scale_guessed"] and abs(guessed["frame"]["metres_per_unit"] * guessed["frame"]["size"] - captureplan.GUESS_ACROSS_M) < 1e-3
assert all(t["prio"] == 2 for t in guessed["targets"] if t["kind"] == "patch" and t["have_views"]), "no holds: what was seen matters"
big_v, big_f = captureplan.decimate(np.repeat(V, 1, axis=0), np.concatenate([F] * 1), 200)
assert len(big_f) <= 200 and big_f.max() < len(big_v)
print("plans and guides OK")

# --- a photo's readings, in its own file --------------------------------------------------------------------------------
photos = tmp / "photos"
photos.mkdir()


def save(name: str, blob: dict | None, code: bytes = b"ASCII\0\0\0") -> pathlib.Path:
    exif = Image.Exif()
    if blob is not None:
        exif.get_ifd(phonedata.EXIF_IFD)[phonedata.USER_COMMENT] = code + phonedata.MARK + json.dumps(blob).encode()
    Image.new("RGB", (64, 48), (90, 90, 90)).save(photos / name, exif=exif)
    return photos / name


p = save("cap_000.jpg", {"v": 1, "down": [0.0, 0.98, 0.2], "ar": {"s": "a", "p": [1, 2, 3]}})
assert phonedata.read(p)["ar"]["p"] == [1, 2, 3] and phonedata.read(save("plain.jpg", None)) is None
assert phonedata.parse("BMSH1:{\"down\": [0, 1, 0]}\0\0") == {"down": [0, 1, 0]} and phonedata.parse(b"BMSH1:not json") is None
r = orientation.reading(p)
assert r["from"] == "app" and abs(np.linalg.norm(r["down"]) - 1) < 1e-9 and abs(r["pitch"] + math.degrees(math.asin(0.2 / math.hypot(0.98, 0.2)))) < 0.01, r
assert phonedata.down({"down": [0, 0, 0]}) is None and phonedata.down({"down": "x"}) is None and orientation.reading(photos / "plain.jpg") is None
print("readings in photos OK")

# --- how big: the phone's own places against the model's ---------------------------------------------------------------
true_scale, turn = 1.3, 0.7
Ry = np.array([[math.cos(turn), 0, math.sin(turn)], [0, 1, 0], [-math.sin(turn), 0, math.cos(turn)]])
ring = [camera(f"cap_{i:03d}.jpg", [5 * math.sin(a), 1.2 + 0.3 * math.sin(3 * a), 5 * math.cos(a)], [0, 1.5, 0]) for i, a in enumerate(np.linspace(-1.2, 1.2, 14))]
blobs = {}
for i, cam in enumerate(ring):
    c = phonedata._centre(cam)
    blobs[cam["name"]] = {"t": 1000.0 + 12 * i, "ar": {"s": "one", "p": list(true_scale * Ry @ c + [3, 0.1, -2] + rng.normal(0, 0.01, 3)), "track": "normal"}}
blobs[ring[4]["name"]]["ar"]["p"] = [9.0, 9.0, 9.0]      # tracking that jumped
blobs[ring[9]["name"]]["ar"]["track"] = "limited"        # and one it said not to trust
(est,) = phonedata.scale_estimates(ring, blobs)
assert abs(est["metres_per_unit"] / true_scale - 1) < 0.01 and est["n"] == 12 and est["of"] == 13 and est["sd"] < 0.03, est
assert phonedata.scale_estimates(ring[:3], blobs) == [], "too few"
still = {c["name"]: {"ar": {"s": "s", "p": [0.01 * i, 0, 0]}} for i, c in enumerate(ring)}
assert phonedata.scale_estimates(ring, still) == [], "taken from one spot: nothing to go by"
both = scale.combine([{"kind": "video", "metres_per_unit": 1.6, "sd": 0.12}, est])
assert abs(both["metres_per_unit"] / true_scale - 1) < 0.02 and both["method"] == "phone" and both["sd"] < 0.05, both
print("size from the phone's places OK")

# --- where: their GPS fixes, around it -----------------------------------------------------------------------------------
lat0, lng0, heading = 34.01, -116.16, 40.0
for cam in ring:
    c = phonedata._centre(cam) * true_scale
    h = math.radians(heading)
    east, north = c[0] * math.cos(h) - c[2] * math.sin(h), -c[0] * math.sin(h) - c[2] * math.cos(h)
    east, north = east + rng.normal(0, 1.0), north + rng.normal(0, 1.0)
    blobs[cam["name"]]["gps"] = {"lat": lat0 + math.degrees(north / phonedata.EARTH),
                                 "lng": lng0 + math.degrees(east / (phonedata.EARTH * math.cos(math.radians(lat0)))), "acc": 3.0}
blobs[ring[2]["name"]]["gps"]["lat"] += 0.001      # a fix 110 m off
where = phonedata.position(ring, blobs, true_scale)
off = math.hypot((where["lat"] - lat0) * 111320, (where["lng"] - lng0) * 111320 * math.cos(math.radians(lat0)))
assert off < 1.5 and where["n"] == 13 and where["heading_from"] == "gps", (off, where)
assert abs((where["heading_deg"] - heading + 180) % 360 - 180) < 12, where
close = {k: {**v, "gps": {**v["gps"], "lat": lat0, "lng": lng0}} for k, v in blobs.items()}
told = phonedata.position(ring, close, true_scale, north=heading)
assert told["heading_deg"] is None and told["heading_from"] == "given", "fixes all at one point say where, not which way"
assert phonedata.position(ring[:2], blobs, true_scale) is None
print("place from GPS OK")

# --- the server's: a mesh's plan made, kept, and made again when its holds change ------------------------------------------
import pipeline  # noqa: E402
import server  # noqa: E402

server.init_db()
mid = "ca" * 6
mdir = server.MESHES / mid
mdir.mkdir(parents=True)
pipeline.write_ply(mdir / "mesh.ply", [tuple(v) for v in V], [(120, 110, 100)] * len(V), [tuple(int(i) for i in f) for f in F])
(mdir / "cameras.json").write_text(json.dumps(cams))
server.q("INSERT INTO meshes(id,name,filename,source,created,lat,lng) VALUES(?,?,?,?,?,?,?)", mid, "A Wall", "mesh.ply", "project:x", 1.0,
         34.0, -116.0, write=True)
assert server.capture_plan("nosuchmesh00") is None
first = server.capture_plan(mid)
assert first["mesh"] == mid and first["name"] == "A Wall" and first["lat"] == 34.0 and first["guide_url"].endswith(first["guide"]["sha1"])
assert not [t for t in first["targets"] if t["kind"] == "hold"] and (mdir / "capture" / "guide.bin").exists()
made = (mdir / "capture" / "plan.json").stat().st_mtime_ns
assert server.capture_plan(mid)["made"] == first["made"] and (mdir / "capture" / "plan.json").stat().st_mtime_ns == made, "kept"
(mdir / "holds.json").write_text(json.dumps({"holds": [{"p": [0.0, 1.0, 2.0], "role": "start"}]}))
again = server.capture_plan(mid)
assert [t["role"] for t in again["targets"] if t["kind"] == "hold"] == ["start"] and again["made"] != first["made"], "made again with its hold"
print("the server's plans OK")
print("ALL CAPTURE TESTS PASSED")
