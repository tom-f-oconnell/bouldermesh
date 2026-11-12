#!/usr/bin/env python3
"""Which way a mesh faces from its photos' skyline (skyline.py), on a scene made here with a known answer: elevation
tiles of a ring of mountains 20 km away, and photos of them from a mesh turned to heading 137° with its up tipped,
with a stand-in for the segmentation model that tells sky by its colour. Then each way it gives up early. Offline."""
from __future__ import annotations

import io
import json
import math
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
try:
    import numpy as np
    from PIL import Image
except ImportError:
    print("numpy or Pillow not installed: skipping")
    sys.exit(0)
import skyline  # noqa: E402

os.environ.pop("TERRAIN_OFFLINE", None)
LAT, LNG = 37.33, -118.57
KX, KY = 111320 * math.cos(math.radians(LAT)), 110574.0
TRUE_HEADING, TIP = 137.0, (2.0, -1.0)      # TIP: the mesh's up tipped about its east and north axes, as match() has it


def ring(lat, lng):
    """Heights (m): 1000 m flat, and a ring of mountains 20 km out whose height changes with bearing."""
    e, n = (lng - LNG) * KX, (lat - LAT) * KY
    d, az = np.hypot(e, n), np.arctan2(e, n)
    return 1000 + (900 + 500 * np.sin(3 * az) + 250 * np.cos(5 * az + 1)) * np.exp(-((d - 20000) / 4000) ** 2)


def crater(lat, lng):
    """Heights (m): a bowl around (LAT + 1, LNG) whose rim, 300 m out, hides everything beyond."""
    e, n = (lng - LNG) * KX, (lat - LAT - 1) * KY
    return 1000 + 120 * np.clip((np.hypot(e, n) - 150) / 150, 0, 1)


def tiles(height):
    """A stand-in for the Terrain Tiles service, from a height function."""
    asked = []

    def fetch(url):
        z, x, y = (int(v) for v in re.search(r"/(\d+)/(\d+)/(\d+)\.png$", url).groups())
        asked.append((z, x, y))
        n = 2 ** z
        px, py = np.meshgrid(np.arange(256) + 0.5, np.arange(256) + 0.5)
        lng = (x * 256 + px) / (n * 256) * 360 - 180
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y * 256 + py) / (n * 256)))))
        v = height(lat, lng) + 32768
        rgb = np.stack([np.floor(v / 256), np.floor(v % 256), np.floor((v % 1) * 256)], -1).astype(np.uint8)
        out = io.BytesIO()
        Image.fromarray(rgb).save(out, "PNG")
        return out.getvalue()
    return fetch, asked


class ByColour:
    """A stand-in for the segmentation model: blue is sky, anything else mountain."""

    def __init__(self):
        self.calls = 0

    def labels(self, im):
        self.calls += 1
        a = np.asarray(im.resize((512, 512)), int)
        return np.where((a[..., 2] > a[..., 0] + 40), skyline.SKY, 16).astype(np.uint8)


def quaternion(R):
    w = math.sqrt(max(0.0, 1 + R[0, 0] + R[1, 1] + R[2, 2])) / 2
    x = math.copysign(math.sqrt(max(0.0, 1 + R[0, 0] - R[1, 1] - R[2, 2])) / 2, R[2, 1] - R[1, 2])
    y = math.copysign(math.sqrt(max(0.0, 1 - R[0, 0] + R[1, 1] - R[2, 2])) / 2, R[0, 2] - R[2, 0])
    z = math.copysign(math.sqrt(max(0.0, 1 - R[0, 0] - R[1, 1] + R[2, 2])) / 2, R[1, 0] - R[0, 1])
    return [w, x, y, z]


def camera(name, centre, bearing_deg, pitch_deg=3.0):
    """A COLMAP camera at centre (mesh units) looking along a bearing in the mesh's frame (-Z north), pitched up."""
    b, p = math.radians(bearing_deg), math.radians(pitch_deg)
    z = np.array([math.sin(b) * math.cos(p), math.sin(p), -math.cos(b) * math.cos(p)])
    down = np.array([0.0, -1.0, 0.0])
    y = down - z * (down @ z)
    y /= np.linalg.norm(y)
    R = np.array([np.cross(y, z), y, z])
    return {"name": name, "q": quaternion(R), "t": (-R @ np.asarray(centre, float)).tolist()}


def photo(cam, el, size=(640, 360)):
    """What the camera sees: sky (blue) above the horizon el (world bearings), as the tipped mesh's frame has it."""
    w, h = size
    f = 1.2 * max(w, h)
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    rays = np.stack([(u - w / 2) / f, (v - h / 2) / f, np.ones_like(u)], -1) @ np.asarray(skyline.beta._rotation(cam["q"]))
    rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
    b = np.degrees(np.arctan2(rays[..., 0], -rays[..., 2])) % 360
    e = np.degrees(np.arcsin(rays[..., 1]))
    tipped = TIP[0] * np.sin(np.radians(b)) + TIP[1] * np.cos(np.radians(b))
    world = el[np.round(((b + TRUE_HEADING) % 360) / skyline.STEP).astype(int) % len(el)]
    sky = e > world + tipped
    return Image.fromarray(np.where(sky[..., None], [90, 140, 225], [125, 115, 100]).astype(np.uint8))


near = lambda a, b, tol: abs((a - b + 180) % 360 - 180) <= tol     # noqa: E731
with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    # --- the horizon, from tiles
    fetch, asked = tiles(ring)
    el, dist = skyline.Elevation(td / "cache" / "terrarium", fetch).horizon(LAT, LNG)
    assert len(el) == 1440 and 15000 < np.median(dist) < 25000 and 0.5 < el.max() < 5, (np.median(dist), el.max())
    north = math.degrees(math.atan2(1000 + 900 + 250 * math.cos(1) - 1000 - 1.6 - 20000 ** 2 / (2 * 6371000) * 0.87, 20000))
    assert abs(el[0] - north) < 0.3, (el[0], north)
    n = len(asked)
    skyline.Elevation(td / "cache" / "terrarium", lambda url: 1 / 0).horizon(LAT, LNG)
    assert n > 10 and len(asked) == n, "tiles are kept"
    print("horizon OK")

    # --- which photos: every still, and a few frames of each video
    names = [f"yt_v1_{i:03d}.jpg" for i in range(20)] + ["ddg_a.jpg", "mountainproject_b.jpg"] + [f"yt_v2_{i:03d}.jpg" for i in range(3)]
    picked = [c["name"] for c in skyline.chosen([{"name": x} for x in names])]
    assert picked[:2] == ["ddg_a.jpg", "mountainproject_b.jpg"] and len(picked) == 2 + skyline.FRAMES_PER_VIDEO + 3, picked
    assert "yt_v1_000.jpg" in picked and "yt_v1_019.jpg" in picked
    print("choosing photos OK")

    # --- a mesh in open country: its photos taken 4 m apart along a line, looking over 140° of the horizon
    mesh, photos = td / "mesh", td / "photos"
    mesh.mkdir(), photos.mkdir()
    cams = [camera(f"ddg_{i}.jpg", (-12 + 4 * i, 1.6, 0.0), -50 + 20 * i) for i in range(7)]
    cams += [camera(f"ddg_{i}b.jpg", (-12 + 4 * i, 1.6, 0.0), -40 + 20 * i) for i in range(7)]
    (mesh / "cameras.json").write_text(json.dumps(cams))
    for c in cams:
        photo(c, el).save(photos / c["name"], quality=95)
    skyline.mesh_points = lambda d: np.random.default_rng(0).normal([0, 1, 30], 0.5, (500, 3))   # a rock behind them
    seg = ByColour()
    found, note = skyline.check(mesh, photos, LAT, LNG, 1.0, td / "cache", log=lambda s: None, fetch=fetch, segmenter=seg)
    assert found and note["why"] is None and note["version"] == skyline.VERSION, note
    assert near(found["deg"], TRUE_HEADING, 1.0) and found["margin"] >= skyline.MIN_MARGIN, found
    assert abs(found["tilt"][0] - TIP[0]) <= 1 and abs(found["tilt"][1] - TIP[1]) <= 1, found
    assert found["photos"] >= skyline.MIN_PHOTOS and found["halves_apart"] is not None and found["halves_apart"] <= 3, found
    print(f"found: {found['deg']}° (truth {TRUE_HEADING}°), tipped {found['tilt']}, margin {found['margin']}")

    # --- giving up early, cheapest first
    class Never:
        def labels(self, im):
            raise AssertionError("looked at a photo")
    found, note = skyline.check(mesh, photos, LAT, LNG, None, td / "cache", log=lambda s: None, fetch=fetch, segmenter=Never())
    assert found is None and note["retry"] and "size" in note["why"], note
    cfetch, _ = tiles(crater)
    found, note = skyline.check(mesh, photos, LAT + 1, LNG, 1.0, td / "crater", log=lambda s: None, fetch=cfetch, segmenter=Never())
    assert found is None and "mostly near" in note["why"] and note["horizon_km"] < 1 and not note.get("retry"), note
    os.environ["TERRAIN_OFFLINE"] = "1"
    found, note = skyline.check(mesh, photos, LAT + 2, LNG, 1.0, td / "nothing", log=lambda s: None, segmenter=Never())
    assert found is None and note["retry"] and "no elevation data" in note["why"], note
    del os.environ["TERRAIN_OFFLINE"]
    for c in cams:      # trees, or rock, filling every photo: the first few looked at, then no more
        Image.new("RGB", (640, 360), (60, 90, 50)).save(photos / c["name"])
    seg = ByColour()
    found, note = skyline.check(mesh, photos, LAT, LNG, 1.0, td / "cache", log=lambda s: None, fetch=fetch, segmenter=seg)
    assert found is None and "looked at first" in note["why"] and seg.calls == skyline.SAMPLE == note["looked"], (note, seg.calls)
    for i, c in enumerate(cams):    # skylines too near to be the elevation data's: they shift between photos 8 m apart
        cam_x = -12 + 4 * (i % 7)
        img = np.full((360, 640, 3), (125, 115, 100), np.uint8)
        img[:120 + 6 * cam_x] = (90, 140, 225)
        Image.fromarray(img).save(photos / c["name"])
    found, note = skyline.check(mesh, photos, LAT, LNG, 1.0, td / "cache", log=lambda s: None, fetch=fetch, segmenter=ByColour())
    assert found is None and "too little distant skyline" in note["why"], note
    print("giving up early OK")
print("ALL SKYLINE TESTS PASSED")
