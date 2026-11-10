#!/usr/bin/env python3
"""Placing photos with VGGT where COLMAP's mapper can't (backend/vggt_place.py), the parts that run without a GPU or
VGGT itself: which photos it's given, its focal lengths in the photos' own pixels, the COLMAP model it starts from, and
the rounds of triangulating and adjusting after it. VGGT itself, and those COLMAP steps for real, are tried in
experiments/depth/test3.py.

    python3 tests/test_vggt_place.py
"""
from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import splat  # noqa: E402
import vggt_place  # noqa: E402


def database(path: Path, links: dict[tuple[int, int], int], n: int = 8, single_camera: bool = False) -> Path:
    """A COLMAP database with the columns vggt_place reads: n photos and the verified matches between them."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cameras (camera_id INTEGER PRIMARY KEY, model INTEGER, width INTEGER, height INTEGER)")
    con.execute("CREATE TABLE images (image_id INTEGER PRIMARY KEY, name TEXT, camera_id INTEGER)")
    con.execute("CREATE TABLE two_view_geometries (pair_id INTEGER PRIMARY KEY, rows INTEGER)")
    for i in range(1, n + 1):
        cam = 1 if single_camera else i
        con.execute("INSERT OR IGNORE INTO cameras VALUES (?, 2, ?, ?)", (cam, 1920 if i % 2 else 1080, 1080 if i % 2 else 1920))
        con.execute("INSERT INTO images VALUES (?, ?, ?)", (i, f"p{i}.jpg", cam))
    for (a, b), rows in links.items():
        con.execute("INSERT INTO two_view_geometries VALUES (?, ?)", (min(a, b) * vggt_place.MAX_IMAGE_ID + max(a, b), rows))
    con.commit()
    con.close()
    return path


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    # which photos: the largest group linked by verified matches (pairs with too few matches don't link)
    links = {(1, 2): 100, (2, 3): 50, (3, 4): 40, (1, 4): 20, (5, 6): 200, (6, 7): 16, (7, 8): 10}
    db = database(tmp / "a.db", links)
    chosen = vggt_place.choose(db)
    assert [c["name"] for c in chosen] == ["p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"], chosen
    assert chosen[0] == {"id": 1, "name": "p1.jpg", "camera": 1, "width": 1920, "height": 1080}, chosen[0]
    assert chosen[1]["width"] == 1080 and chosen[1]["height"] == 1920
    # more than fit: grown from the best linked photo, each time by the one most linked to those already taken
    assert [c["id"] for c in vggt_place.choose(db, 2)] == [1, 2]
    assert [c["id"] for c in vggt_place.choose(db, 3)] == [1, 2, 3]
    # groups as large as each other: the one with more matches
    db = database(tmp / "b.db", {(1, 2): 30, (3, 4): 300})
    assert [c["id"] for c in vggt_place.choose(db)] == [3, 4]
    assert vggt_place.choose(database(tmp / "c.db", {(1, 2): 5})) == [], "no pair with enough matches"
print("choosing photos OK")

# focal lengths: VGGT sees each photo 518 wide, its height in proportion to the nearest 14, then cropped to 518 or padded
for w, h in ((1920, 1080), (1080, 1920), (1280, 720), (608, 1080), (480, 854)):
    scaled = round(h * 518 / w / 14) * 14
    K = [[1000 * 518 / w, 0, 259], [0, 1000 * scaled / h, min(scaled, 518) / 2], [0, 0, 1]]
    assert abs(vggt_place.focal(K, w, h) - 1000) < 1e-6, (w, h, vggt_place.focal(K, w, h))
print("focal lengths OK")

# rotations: COLMAP's quaternion, as splat.py reads cameras.json's back
random.seed(0)
mm = lambda A, B: [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]  # noqa: E731
for _ in range(500):
    a, b, c = (random.uniform(-math.pi, math.pi) for _ in range(3))
    Rz = [[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]]
    Ry = [[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]]
    Rx = [[1, 0, 0], [0, math.cos(c), -math.sin(c)], [0, math.sin(c), math.cos(c)]]
    R = mm(mm(Rz, Ry), Rx)
    back = splat.rotation(vggt_place.quaternion(R))
    assert max(abs(back[i][j] - R[i][j]) for i in range(3) for j in range(3)) < 1e-9, R
assert vggt_place.quaternion([[1, 0, 0], [0, -1, 0], [0, 0, -1]]) == (0.0, 1.0, 0.0, 0.0)   # 180° about x
print("rotations OK")

# the model VGGT's placements start COLMAP from: the database's ids, a lens per camera, no points
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    db = database(tmp / "a.db", {(1, 2): 100, (2, 3): 50}, n=3, single_camera=True)
    chosen = vggt_place.choose(db)
    I = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    placed = {"p1.jpg": (I, [0, 0, 0], 900.0), "p2.jpg": (I, [1, 0, 0], 1000.0), "p3.jpg": (I, [2, 0, 0], 1300.0)}
    vggt_place.write_model(tmp / "m", chosen, placed)
    cams = (tmp / "m" / "cameras.txt").read_text().split()
    assert cams == ["1", "SIMPLE_RADIAL", "1920", "1080", "1000.0000", "960.0", "540.0", "0"], cams
    lines = (tmp / "m" / "images.txt").read_text().split("\n")
    assert lines[0].split()[0] == "1" and lines[0].split()[-2:] == ["1", "p1.jpg"] and lines[1] == "", lines
    assert [float(x) for x in lines[2].split()[1:8]] == [1, 0, 0, 0, 1, 0, 0], lines[2]
    assert (tmp / "m" / "points3D.txt").read_text() == ""
print("starting model OK")


# the rounds after VGGT: triangulating from its placements, then adjusting, each tighter, the last into out
def placing(why=None, links=None, fit=("cuda", 80)):
    calls = []
    saved = vggt_place.unavailable, vggt_place.capacity, vggt_place.predict
    vggt_place.unavailable, vggt_place.capacity = (lambda: why), (lambda: fit)
    vggt_place.predict = lambda paths, device, log: {p.name: ([[1, 0, 0], [0, 1, 0], [0, 0, 1]], [i, 0, 0], 1000.0)
                                                     for i, p in enumerate(paths)}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = database(tmp / "database.db", links if links is not None else {(1, 2): 100, (2, 3): 50, (3, 4): 40})

        def run(cmd):
            calls.append(cmd)
            out = Path(cmd[cmd.index("--output_path") + 1])
            (out / "images.bin").write_bytes(struct.pack("<Q", 4))
            (out / "points3D.bin").write_bytes(struct.pack("<Q", 1000 * len(calls)))
        try:
            n = vggt_place.place("colmap", db, tmp / "photos", tmp / "sparse" / "vggt", tmp / "vggt", lambda s: None, run)
            return n, calls, json.loads((tmp / "vggt" / "placed.json").read_text()), tmp
        finally:
            vggt_place.unavailable, vggt_place.capacity, vggt_place.predict = saved


n, calls, record, tmp = placing()
assert n == 4 and [c[1] for c in calls] == ["point_triangulator", "bundle_adjuster"] * len(vggt_place.ROUNDS), calls
arg = lambda c, k: c[c.index(k) + 1]  # noqa: E731
assert arg(calls[0], "--input_path").endswith("vggt/start") and arg(calls[-1], "--output_path").endswith("sparse/vggt")
for i, (angle, err, focal) in enumerate(vggt_place.ROUNDS):
    tri, ba = calls[2 * i], calls[2 * i + 1]
    assert arg(tri, "--Mapper.tri_create_max_angle_error") == str(angle) and arg(tri, "--Mapper.filter_max_reproj_error") == str(err)
    assert arg(tri, "--Mapper.tri_ignore_two_view_tracks") == "0", "with few photos, points two photos see count"
    assert arg(ba, "--input_path") == arg(tri, "--output_path") and arg(ba, "--BundleAdjustment.refine_focal_length") == str(focal)
    if i:
        assert arg(tri, "--input_path") == arg(calls[2 * i - 1], "--output_path"), "each round starts from the last"
assert record == {"by": "vggt", "photos": 4, "rounds": [list(r) for r in vggt_place.ROUNDS]}, record
try:
    placing(why="no CUDA GPU is visible")
    raise AssertionError("should say it can't run")
except vggt_place.Unavailable as e:
    assert "no CUDA GPU" in str(e)
try:
    placing(links={(1, 2): 100})
    raise AssertionError("two photos are too few")
except RuntimeError as e:
    assert "only 2 photo(s)" in str(e), e
print("rounds OK")

# whether it can run here
os.environ["VGGT_FALLBACK"] = "0"
assert vggt_place.unavailable() == "VGGT_FALLBACK=0"
del os.environ["VGGT_FALLBACK"]
why = vggt_place.unavailable()
assert why is None or "isn't installed" in why or "GPU" in why, why
print(f"here: {why or 'VGGT can run'}")
print("ALL VGGT PLACING TESTS PASSED")
