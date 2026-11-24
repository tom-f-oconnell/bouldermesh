#!/usr/bin/env python3
"""A model's check of itself (backend/modelcheck.py), on a made-up model and database in COLMAP's layout, where the
answer is known; and that the pipeline's use of it can't cost a mesh.

    python3 tests/test_modelcheck.py
"""
from __future__ import annotations

import json
import math
import pathlib
import sqlite3
import struct
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
try:
    import numpy as np
except ImportError:
    print("SKIPPED: numpy isn't installed")
    sys.exit(0)
import modelcheck  # noqa: E402
import pipeline  # noqa: E402
import reconstruction  # noqa: E402

N, W, H = 12, 1280, 720


def rot(axis, deg):
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    t = math.radians(deg)
    return np.eye(3) + math.sin(t) * K + (1 - math.cos(t)) * K @ K


def quat(R):
    w = math.sqrt(max(0.0, 1 + R[0, 0] + R[1, 1] + R[2, 2])) / 2
    x = math.copysign(math.sqrt(max(0.0, 1 + R[0, 0] - R[1, 1] - R[2, 2])) / 2, R[2, 1] - R[1, 2])
    y = math.copysign(math.sqrt(max(0.0, 1 - R[0, 0] + R[1, 1] - R[2, 2])) / 2, R[0, 2] - R[2, 0])
    z = math.copysign(math.sqrt(max(0.0, 1 - R[0, 0] - R[1, 1] + R[2, 2])) / 2, R[1, 0] - R[0, 1])
    return w, x, y, z


def cameras(seed=0):
    """Twelve cameras: each its turn, where it stands (as COLMAP's t) and focal length."""
    rng = np.random.default_rng(seed)
    return [(rot(rng.normal(size=3), rng.uniform(5, 40)), rng.normal(size=3) * 2, 900.0 + 40 * i) for i in range(N)]


def write_model(d: pathlib.Path, cams, err=0.8, colmap4=False) -> None:
    """A binary model of those cameras (SIMPLE_RADIAL, one each), each seeing 40 points, and 480 points `err` px off."""
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "cameras.bin", "wb") as f:
        f.write(struct.pack("<Q", len(cams)))
        for i, (_, _, fl) in enumerate(cams):
            f.write(struct.pack("<iiQQ4d", i + 1, 2, W, H, fl, W / 2, H / 2, 0.01))
    with open(d / "images.bin", "wb") as f:
        f.write(struct.pack("<Q", len(cams)))
        for i, (R, t, _) in enumerate(cams):
            f.write(struct.pack("<I7dI", i + 1, *quat(R), *t, i + 1) + f"p{i:02d}.jpg".encode() + b"\0")
            f.write(struct.pack("<Q", 50))
            for k in range(50):
                f.write(struct.pack("<ddq", float(k), float(k), k if k < 40 else -1))
    with open(d / "points3D.bin", "wb") as f:
        f.write(struct.pack("<Q", 480))
        for k in range(480):
            f.write(struct.pack("<Q3d3Bd", k, 0.0, 0.0, 0.0, 1, 2, 3, err * (0.5 + k % 3 * 0.5)) + struct.pack("<Q", 3) + struct.pack("<6I", 1, 0, 2, 0, 3, 0))
    if colmap4:     # the files COLMAP 4 adds, which the check has no need of
        (d / "rigs.bin").write_bytes(b"\0" * 8)
        (d / "frames.bin").write_bytes(b"\0" * 8)


def write_db(path: pathlib.Path, cams, colmap4=False) -> None:
    """A database whose verified pairs are those cameras' own: F for each pair, from their turns and places."""
    c = sqlite3.connect(path)
    extra = ", qvec BLOB, tvec BLOB" if colmap4 else ""
    c.executescript(f"""
        CREATE TABLE cameras (camera_id INTEGER PRIMARY KEY, model INTEGER, width INTEGER, height INTEGER, params BLOB, prior_focal_length INTEGER);
        CREATE TABLE images (image_id INTEGER PRIMARY KEY, name TEXT UNIQUE, camera_id INTEGER);
        CREATE TABLE keypoints (image_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE descriptors (image_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE matches (pair_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE two_view_geometries (pair_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB, config INTEGER, F BLOB, E BLOB, H BLOB{extra});
    """)
    Ks = [np.array([[fl, 0, W / 2], [0, fl, H / 2], [0, 0, 1.0]]) for _, _, fl in cams]
    for i in range(len(cams)):
        c.execute("INSERT INTO images VALUES (?, ?, ?)", (i + 1, f"p{i:02d}.jpg", i + 1))
    for i in range(len(cams)):
        for j in range(i + 1, len(cams)):
            R = cams[j][0] @ cams[i][0].T
            t = cams[j][1] - R @ cams[i][1]
            E = np.array([[0, -t[2], t[1]], [t[2], 0, -t[0]], [-t[1], t[0], 0]]) @ R
            F = np.linalg.inv(Ks[j]).T @ E @ np.linalg.inv(Ks[i])
            row = [(i + 1) * modelcheck.MAX_IMAGE_ID + j + 1, 60, 2, b"", 3, F.astype("<f8").tobytes(), b"", b""] + ([b"", b""] if colmap4 else [])
            c.execute(f"INSERT INTO two_view_geometries VALUES ({','.join('?' * len(row))})", row)
    # pairs that mustn't count: too few inliers, and one COLMAP called planar
    c.execute("UPDATE two_view_geometries SET rows = 8 WHERE pair_id = ?", (1 * modelcheck.MAX_IMAGE_ID + 2,))
    c.execute("UPDATE two_view_geometries SET config = 6, F = ? WHERE pair_id = ?", (np.eye(3).tobytes(), 1 * modelcheck.MAX_IMAGE_ID + 3))
    c.commit()
    c.close()


with tempfile.TemporaryDirectory() as tmp:
    tmp = pathlib.Path(tmp)
    cams = cameras()
    for four in (False, True):      # COLMAP 3.9.1's layout, and 4.2.1's
        d = tmp / f"right{int(four)}"
        write_model(d / "model", cams, colmap4=four)
        write_db(d / "db.db", cams, colmap4=four)
        c = modelcheck.check(d / "model", d / "db.db")
        assert c["ok"] and c["failed"] == [] and c["placed"] == N and c["points"] == 480 and c["track"] == 3.0, c
        assert c["pairs"] == N * (N - 1) // 2 - 2 and c["pair_med_deg"] < 0.01 and c["untied"] == 0 and c["odd_lenses"] == 0, c
        assert abs(c["err_mean"] - 0.8) < 1e-6 and "passed" in modelcheck.line(c)
    print("consistent cameras pass, in both COLMAPs' layouts OK")

    # one camera turned 30 degrees: its 11 pairs say so, and the median of 64 pairs doesn't move
    turned = list(cams)
    turned[5] = (rot([0, 1, 0], 30) @ cams[5][0], cams[5][1], cams[5][2])
    write_model(tmp / "one" / "model", turned)
    got = modelcheck.read_model(tmp / "one" / "model")
    ang = sorted(modelcheck.pair_angles(got[0], tmp / "right0" / "db.db"))
    assert sum(1 for a in ang if abs(a - 30) < 0.5) == 11 and ang[len(ang) - 12] < 0.01, ang[-13:]
    one = modelcheck.check(tmp / "one" / "model", tmp / "right0" / "db.db")
    assert one["ok"] and one["pair_med_deg"] < 0.01 and 29 < one["pair_p90_deg"] < 31, one
    # most of them turned: the model is at odds with its photos, and fails
    many = [(rot(np.random.default_rng(i).normal(size=3), 25) @ R, t, f) if i % 3 else (R, t, f) for i, (R, t, f) in enumerate(cams)]
    write_model(tmp / "many" / "model", many)
    bad = modelcheck.check(tmp / "many" / "model", tmp / "right0" / "db.db")
    assert not bad["ok"] and bad["failed"] == ["pairs", "split"] and bad["pair_med_deg"] > 10 and "FAILED (pairs, split)" in modelcheck.line(bad), bad
    # one camera wrong: a group of its own, the rest one group; groups of cameras that agree among themselves only
    groups, judged = modelcheck.agreeing_groups(got[0], tmp / "right0" / "db.db")
    assert [len(g) for g in groups] == [11, 1] and "p05.jpg" in groups[1] and len(judged) == 12, groups
    assert one["ok"] and one["agree_share"] == round(11 / 12, 3), one
    print("cameras turned from where their pairs have them show OK")

    # points 5 px off: fails on error, whatever the cameras say
    write_model(tmp / "far" / "model", cams, err=5.0)
    far = modelcheck.check(tmp / "far" / "model", tmp / "right0" / "db.db")
    assert far["failed"] == ["error"] and far["why"].startswith("its points are 5.00 px off"), far
    # a few points whose error COLMAP never set (4.2.1 has written 1e152): left out, and counted, not failed on
    got = modelcheck.read_model(tmp / "right0" / "model")
    junk = modelcheck.numbers_of(got[0], list(got[1]) + [1.2e152, 5.6e150, float("nan"), float("inf"), -1.0], got[2], tmp / "right0" / "db.db")
    assert junk["err_unread"] == 4 and junk["points"] == 480 and abs(junk["err_mean"] - 0.8) < 1e-6, junk
    assert modelcheck.verdict(junk)["ok"], junk
    # lenses that can't be real, and cameras that see next to nothing
    odd = [(R, t, 50.0 if i < 4 else f) for i, (R, t, f) in enumerate(cams)]
    write_model(tmp / "odd" / "model", odd)
    assert "lenses" in modelcheck.check(tmp / "odd" / "model", tmp / "right0" / "db.db")["failed"]
    # the verdict alone, on numbers the benchmark's models gave
    good = {"placed": 143, "points": 52842, "track": 10.2, "err_mean": 0.95, "err_p90": 1.75, "pairs": 4303, "pair_med_deg": 3.03, "odd_lenses": 8, "untied": 0}
    assert modelcheck.verdict(good)["ok"]
    assert modelcheck.verdict({**good, "pair_med_deg": 26.85})["failed"] == ["pairs"]
    assert modelcheck.verdict({**good, "placed": 114, "pair_med_deg": 16.2, "odd_lenses": 24})["failed"] == ["pairs", "lenses"]
    assert modelcheck.verdict({**good, "pairs": 6, "pair_med_deg": 50.0})["ok"], "too few pairs to judge by"
    assert not modelcheck.verdict({**good, "err_mean": None, "err_p90": None})["ok"]
    print("error, lenses and the verdict's thresholds OK")

    # as the pipeline uses it: check.json beside the mesh and a line in the log; nothing it meets can raise
    said: list[str] = []
    out = tmp / "out"
    c = pipeline.check_model(tmp / "right0" / "model", tmp / "right0" / "db.db", out, said.append)
    assert c["ok"] and modelcheck.load(out)["pair_med_deg"] == c["pair_med_deg"] and said[-1].startswith("model check: passed"), said
    for model, db in ((tmp / "nowhere", tmp / "right0" / "db.db"), (tmp / "right0" / "model", tmp / "nowhere.db")):
        said.clear()
        assert pipeline.check_model(model, db, tmp / "out2", said.append) is None and not (tmp / "out2" / modelcheck.FILE).exists()
        assert said and said[0].startswith("model check: not made ("), said
    (tmp / "junk.db").write_text("not a database")
    assert pipeline.check_model(tmp / "right0" / "model", tmp / "junk.db", tmp / "out2", said.append) is None
    assert modelcheck.load(tmp / "out2") is None
    print("the pipeline's use of it never raises OK")

    # later, from what a mesh kept of its reconstruction (which has the verified pairs, not the features)
    mesh = tmp / "mesh"
    assert reconstruction.keep(tmp / "many" / "model", tmp / "right0" / "db.db", mesh, lambda m: None, "COLMAP x", {"args": [], "device": "cpu"})
    later = modelcheck.of_mesh(mesh, write=True)
    assert later["failed"] == ["pairs", "split"] and later["pair_med_deg"] == bad["pair_med_deg"] and modelcheck.load(mesh)["ok"] is False
    assert modelcheck.of_mesh(tmp / "out2") is None, "a mesh that kept nothing has nothing to check"
    print("a kept reconstruction checked later OK")

    # cameras placed collapsed: their points at under 2% of the model's usual depth (every point is at the origin here,
    # so each camera's depth is its t's z), however well every turn and error holds up
    flat = [(np.eye(3), np.array([0.0, 0.0, 5.0 + i % 3]), 1000.0) for i in range(10)] + \
        [(np.eye(3), np.array([0.0, 0.0, 0.05]), 1000.0), (np.eye(3), np.array([0.0, 0.0, 0.001]), 1000.0)]
    write_model(tmp / "collapsed" / "model", flat)
    m = tmp / "collapsed" / "model"
    assert modelcheck.collapsed((m / "images.bin").read_bytes(), (m / "points3D.bin").read_bytes()) == ["p10.jpg", "p11.jpg"]
    assert reconstruction.keep(m, tmp / "right0" / "db.db", tmp / "cmesh", lambda m: None, "COLMAP x", {"args": [], "device": "cpu"})
    assert modelcheck.collapsed_kept(tmp / "cmesh") == ["p10.jpg", "p11.jpg"] and modelcheck.collapsed_kept(tmp / "out2") is None
    write_model(tmp / "apart" / "model", flat[:10])
    m = tmp / "apart" / "model"
    assert modelcheck.collapsed((m / "images.bin").read_bytes(), (m / "points3D.bin").read_bytes()) == [], "none, all at their depths"
    print("collapsed cameras OK")
print("ALL MODEL CHECK TESTS PASSED")
