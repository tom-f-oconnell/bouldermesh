#!/usr/bin/env python3
"""The incremental mapper's reading of the phone apps' captures (experiments/mapper/incr/capture.py), against the
numbers the apps' own tests use (app/shared GeometryTest.kt) and the fixtures they wrote; no COLMAP, no GPU."""
import json
import math
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "mapper" / "incr"))
import capture as cp  # noqa: E402

LENS = {"fx": 3000.0, "fy": 3000.0, "cx": 2000.0, "cy": 1500.0, "w": 4000, "h": 3000}      # TestWorld.lens
FIXTURES = ROOT / "app" / "shared" / "src" / "jvmTest" / "resources"


def near(a, b, eps):
    return float(np.linalg.norm(np.asarray(a, float) - np.asarray(b, float))) <= eps


def test_cameras_as_geometry_test():
    """GeometryTest.cameras(), number for number."""
    cam = cp.look([0.0, 1.5, 5.0], [0.0, 1.5, 0.0], LENS)
    u, v, depth = cam.project([0.0, 1.5, 0.0])
    assert near([u, v, depth], [2000.0, 1500.0, 5.0], 1e-6)
    assert cam.project([-1.0, 1.5, 0.0])[0] < 2000.0
    assert cam.project([0.0, 2.5, 0.0])[1] < 1500.0, "up in the world is up the picture"
    assert cam.project([0.0, 1.5, 9.0]) is None, "behind it"
    s = cp.Similarity(1.25, cp.axis_angle([0, 1, 0], 0.6), [3.0, 0.2, -4.0])
    p = [0.3, 1.0, 0.0]
    world = cp.look(s.apply([0.0, 1.5, 4.0]), s.apply(p), LENS)
    in_mesh = world.into(s)
    assert near(in_mesh.centre, [0.0, 1.5, 4.0], 1e-9)
    assert abs(in_mesh.project(p)[0] - world.project(s.apply(p))[0]) < 1e-6, "it lands on the same pixel"


def test_colmap_pose_projects_alike():
    """A camera given to COLMAP (world to camera q, t) puts a point on the same pixel as the app's pose does."""
    s = cp.Similarity(0.8, cp.qunit([0.9, 0.1, 0.3, -0.2]), [1.0, -2.0, 0.5])
    world = cp.look([2.0, 1.6, 3.0], [0.0, 1.0, 0.0], LENS)
    cam = world.into(s)
    q, t, lens = cp.colmap(cam)
    R = cp.qmatrix(q)
    for p in ([0.1, 0.9, 0.2], [-0.5, 1.3, -0.4]):
        x = R @ np.asarray(p) + np.asarray(t)
        uv = [lens["params"][0] * x[0] / x[2] + lens["params"][2], lens["params"][1] * x[1] / x[2] + lens["params"][3]]
        assert near(uv, cam.project(p)[:2], 1e-6)
    assert near(-R.T @ np.asarray(t), cam.centre, 1e-9)
    assert lens["model"] == 1 and lens["width"] == 4000


def test_down_and_level():
    """A level camera's down (0, 1, 0) in its picture comes out as the model's -Y; spread and tilt are as made."""
    cams = [cp.look([4 * math.sin(a), 1.5, 4 * math.cos(a)], [0, 1.5, 0], LENS) for a in np.linspace(0, 2, 9)]
    downs = []
    for c in cams:
        q, _, _ = cp.colmap(c)
        downs.append(cp.down_in_model({"down": [0.0, 1.0, 0.0]}, cp.qmatrix(q)))
    lv = cp.level(downs)
    assert near(lv["down"], [0, -1, 0], 1e-9) and lv["tilt_from_model_deg"] < 1e-6 and lv["spread_median_deg"] < 1e-6
    # a model tilted 2 degrees about x: its cameras' downs say so
    tilt = cp.qmatrix(cp.axis_angle([1, 0, 0], math.radians(2)))
    downs = [cp.down_in_model({"down": [0.0, 1.0, 0.0]}, cp.qmatrix(cp.colmap(c)[0]) @ tilt.T) for c in cams]
    assert abs(cp.level(downs)["tilt_from_model_deg"] - 2.0) < 1e-6


def test_points_file():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "capture-points.bin"
        f.write_bytes(b"BMP1" + struct.pack("<I", 2) + struct.pack("<3fH", 1.0, 2.0, 3.0, 1) + struct.pack("<3fH", -1.0, 0.5, 0.25, 2))
        P, S = cp.points(f)
        assert near(P[1], [-1.0, 0.5, 0.25], 1e-6) and list(S) == [1, 2]


def test_fixtures():
    """The record and readings the app's tests write."""
    r = cp.record(FIXTURES / "app" / "capture.json")
    assert r["v"] == 2 and r["shots"][0]["name"] == "cap_k1_001.jpg"
    blob = json.loads((FIXTURES / "app" / "shot.txt").read_text().split("BMSH1:", 1)[1])
    cam = cp.pose(blob)
    assert near(cam.centre, [1.0, 2.0, 3.0], 1e-12) and cam.lens["w"] == 4000
    assert list(cp.spells(r["shots"])) == ["a"]


if __name__ == "__main__":
    for name, f in sorted(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
