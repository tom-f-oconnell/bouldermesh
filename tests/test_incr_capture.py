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


def test_refit_tie_undone():
    """A model in its own frame, tied to the former by backend/frames.py, then laid back: the tie is then the identity."""
    sys.path.insert(0, str(ROOT / "backend"))
    import frames
    rng = np.random.default_rng(1)
    former = []
    for i in range(12):
        a = i * 0.5
        cam = cp.look([4 * math.sin(a), 1.5 + 0.1 * i, 4 * math.cos(a)], [0, 1.2, 0], LENS)
        q, t, _ = cp.colmap(cam)
        former.append({"name": f"p{i}.jpg", "q": q, "t": t})
    R = cp.qmatrix(cp.qunit([0.8, 0.3, -0.4, 0.2]))
    theirs = cp.cameras_moved(former, (2.5, R, np.array([1.0, -3.0, 0.5])))
    theirs[3]["t"] = list(np.asarray(theirs[3]["t"]) + 5.0)         # one placed wrongly: left out by the tie
    tie = frames.tie(former, theirs)
    assert tie["status"] == "tied" and abs(tie["scale"] - 2.5) < 1e-6 and tie["shared"] == 11
    back = cp.cameras_moved(theirs, cp.undo_tie(tie))
    again = frames.tie(former, back)
    assert abs(again["scale"] - 1) < 1e-6 and again["rot_deg"] < 1e-4 and again["rms"] < 1e-5


def test_relevel():
    """Readings all saying down is 2 degrees off -Y call for the turn that puts it back; spread or few readings don't."""
    tilt = cp.qmatrix(cp.axis_angle([1, 0, 0], math.radians(2)))
    d = tilt @ np.array([0.0, -1.0, 0.0])
    downs = [d + np.random.default_rng(i).normal(size=3) * 0.002 for i in range(20)]
    lv = cp.level([x / np.linalg.norm(x) for x in downs])
    turn = cp.relevel(lv)
    assert turn is not None and near(turn @ np.asarray(lv["down"]), [0, -1, 0], 1e-5) and abs(lv["tilt_from_model_deg"] - 2) < 0.1
    assert cp.relevel({**lv, "n": 3}) is None and cp.relevel({**lv, "spread_median_deg": 8.0}) is None
    assert cp.relevel({**lv, "tilt_from_model_deg": 0.2}) is None


def test_rotate_glb():
    """A GLB's vertices turned, its faces and textures as they were."""
    import texture as tx
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        from PIL import Image
        Image.new("RGB", (8, 8), (10, 20, 30)).save(d / "t.png")
        V = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
        tx.write_glb(d / "a.glb", [{"V": V, "UV": [[0, 0], [1, 0], [0, 1]], "I": [[0, 1, 2]], "image": d / "t.png"}])
        R = cp.qmatrix(cp.axis_angle([0, 0, 1], math.pi / 2))
        tx.rotate_glb(d / "a.glb", d / "b.glb", R)
        W, F = tx.glb_geometry(d / "b.glb")
        assert near(W, V @ R.T, 1e-6) and F.tolist() == [[0, 1, 2]]


def test_card_scale():
    """A card's corners seen from close cameras, half a pixel off, put back on its plane: the scale within 0.1%, its
    shape a card's; a square of the card's width isn't one."""
    import card
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        card.render(d, n=4, seed=3, size=(320, 240), f=300.0)
        t = json.loads((d / "truth.json").read_text())
        C = np.array(t["card"])
        ests = []
        for i, cam in enumerate(t["photos"].values()):
            K, R, tt = np.array(cam["K"]), np.array(cam["R"]), np.array(cam["t"])
            x = (K @ (R @ C.T + tt[:, None])).T
            px = x[:, :2] / x[:, 2:] + np.random.default_rng(i).normal(0, 0.5, (4, 2)) * 0.2
            o, dirs = card.rays(px, K, R, tt)
            ests.append(card.scale(card.meet_plane(o, dirs, np.zeros(3), np.array([0.0, 1.0, 0.0]))))
        c = card.combine(ests)
        assert all(e["ok"] for e in ests) and abs(c["metres_per_unit"] / t["mpu"] - 1) < 1e-3
        sq = np.array([[0, 0, 0], [4.28, 0, 0], [4.28, 0, 4.28], [0, 0, 4.28]])
        assert not card.scale(sq)["ok"]


def test_printable_solid():
    """A bumpy patch cut to a radius, closed into a solid with a flat base: watertight, in millimetres, as STL and 3MF;
    and ICP finds nothing to correct against the surface it came from."""
    import details as dt
    n = 30
    xs, zs = np.meshgrid(np.linspace(-1, 1, n), np.linspace(-1, 1, n))
    V = np.c_[xs.ravel(), 0.1 * np.sin(3 * xs.ravel()) * np.cos(2 * zs.ravel()), zs.ravel()]
    F = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j, (i + 1) * n + j + 1
            F += [[a, c, b], [b, c, d]]
    F = np.array(F)
    Vc, Fc = dt.cut(V, F, [0, 0, 0], 0.8)
    S, T = dt.solid(Vc, Fc, base_mm=3, mm=100.0)
    assert dt.is_watertight(T) and np.ptp(S[:, 0]) > 150
    with tempfile.TemporaryDirectory() as d:
        r = dt.printable(V, F, [0, 0, 0], 0.8, 100.0, Path(d) / "h")
        assert r["watertight"] and (Path(d) / "h.stl").stat().st_size > 1000 and (Path(d) / "h.3mf").exists()
    R, t, rms0, rms1, used = dt.icp_point_to_plane(Vc, V, F, reach=0.2)
    assert rms1 < 1e-9 and dt.tie_check(R, t, rms1, [0, 0, 0], 100.0)[0] == "tied"


if __name__ == "__main__":
    for name, f in sorted(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
