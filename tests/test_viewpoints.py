#!/usr/bin/env python3
"""Where each climb on a boulder's mesh is seen from (viewpoints.py), on a cube with cameras placed around it.
Offline."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
try:
    import numpy as np
except ImportError:
    print("numpy not installed: skipping")
    sys.exit(0)
import viewpoints  # noqa: E402

LENS = {"model": 2, "width": 800, "height": 600, "params": [700.0, 400.0, 300.0, 0.0]}


def quaternion(R) -> list[float]:
    """w, x, y, z of a rotation matrix (beta._rotation's inverse), from its largest component, so half turns work too."""
    t = np.trace(R)
    if t > 0:
        s = 2 * np.sqrt(1 + t)
        return [s / 4, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s]
    i = int(np.argmax(np.diag(R)))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = 2 * np.sqrt(1 + R[i, i] - R[j, j] - R[k, k])
    q = [0.0, 0.0, 0.0, 0.0]
    q[0], q[1 + i], q[1 + j], q[1 + k] = (R[k, j] - R[j, k]) / s, s / 4, (R[j, i] + R[i, j]) / s, (R[k, i] + R[i, k]) / s
    return q


def camera(name: str, at, looking_at=(0, 0, 0)) -> dict:
    """A placed photo taken from `at`, looking at `looking_at`, upright (COLMAP's: x right, y down, z ahead)."""
    at = np.asarray(at, float)
    z = np.asarray(looking_at, float) - at
    z /= np.linalg.norm(z)
    x = np.cross([0, -1.0, 0], z)
    x /= np.linalg.norm(x)
    R = np.array([x, np.cross(z, x), z])
    return {"name": name, "q": quaternion(R), "t": list(-R @ at), "lens": LENS}


def cube_ply(path: pathlib.Path) -> None:
    V = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = [t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))]
    path.write_text("ply\nformat ascii 1.0\nelement vertex 8\nproperty float x\nproperty float y\nproperty float z\n"
                    f"element face {len(tris)}\nproperty list uchar int vertex_indices\nend_header\n"
                    + "".join(f"{x} {y} {z}\n" for x, y, z in V) + "".join(f"3 {a} {b} {c}\n" for a, b, c in tris))


# its sanity: the camera helper and beta's rotation agree
from beta import _rotation  # noqa: E402
cam = camera("c", [5, 0, 0])
R = _rotation(cam["q"])
assert np.allclose(-R.T @ np.asarray(cam["t"]), [5, 0, 0]) and np.allclose(R.T @ [0, 0, 1], [-1, 0, 0])

with tempfile.TemporaryDirectory() as td:
    mesh = pathlib.Path(td)
    cube_ply(mesh / "mesh.ply")
    cams = [camera("mountainproject_a1.jpg", [5, 0.5, 0.4]), camera("yt_aaaa_001.jpg", [5.5, 0.2, -0.3]),
            camera("yt_aaaa_002.jpg", [4.8, 0.3, 0.0], (0.9, 0.2, 0.1)),
            camera("mountainproject_b1.jpg", [0.2, 0.4, -6]), camera("ddg_web.jpg", [-5, 0, 0]),
            camera("yt_close_001.jpg", [0, 0, 1.05], (0, 0, 0))]
    (mesh / "cameras.json").write_text(json.dumps(cams))
    logged = []
    out = viewpoints.compute(mesh, {"a": {"mountainproject_a1.jpg", "yt_aaaa_001.jpg", "yt_aaaa_002.jpg", "mountainproject_a9.jpg"},
                                    "b": {"mountainproject_b1.jpg"}, "c": {"mountainproject_c1.jpg"}, "d": set(),
                                    "e": {"yt_close_001.jpg"}}, logged.append)
    a, b, e = out["climbs"]["a"], out["climbs"]["b"], out["climbs"]["e"]
    assert a["photos"] == 3 and a["of"] == 4 and a["names"] == ["mountainproject_a1.jpg", "yt_aaaa_001.jpg", "yt_aaaa_002.jpg"], a
    assert a["eye"][0] > 2 and abs(a["target"][0] - 1) < 0.05, ("from its side, looking at the face its photos see", a)
    assert np.allclose(a["eye"], [5, 0.5, 0.4], atol=1e-4) or np.allclose(a["eye"], [4.8, 0.3, 0.0], atol=1e-4), "the middle one's place"
    assert b["eye"][2] < -2 and abs(b["target"][2] + 1) < 0.05, b
    size = 2.0     # the cube's, as Mesh.size has it: what its 5th to 95th percentile of vertices span
    assert np.linalg.norm(np.subtract(e["eye"], e["target"])) >= viewpoints.NEAREST * size - 1e-6, ("a photo from right up close is backed off", e)
    assert out["unplaced"] == {"c": 1} and "d" not in out["climbs"], out
    assert viewpoints.load(mesh) == json.loads(json.dumps(out))
    assert "3 climb(s) placed by their own photos, 1 with none of theirs placed" in logged[-1], logged

    # without a mesh to read, its cameras still say roughly where things are
    (mesh / "mesh.ply").unlink()
    out = viewpoints.compute(mesh, {"a": {"mountainproject_a1.jpg", "yt_aaaa_001.jpg"}}, logged.append)
    a = out["climbs"]["a"]
    assert a["eye"][0] > 2 and a["target"][0] < a["eye"][0], a
    assert "couldn't read the mesh" in logged[-2], logged

# where the reconstruction recorded how what it left out is tied to what it placed (models.json), a climb's video frames
# are its own only where placed, or tied to a placed photo by 16 verified matches or more; its page's photos always are
with tempfile.TemporaryDirectory() as td:
    mesh = pathlib.Path(td)
    cube_ply(mesh / "mesh.ply")
    cams = [camera("mountainproject_a1.jpg", [5, 0.5, 0.4]), camera("yt_aaaa_001.jpg", [5.5, 0.2, -0.3]), camera("ddg_web.jpg", [-5, 0, 0])]
    (mesh / "cameras.json").write_text(json.dumps(cams))
    left = {"yt_aaaa_002.jpg": {"with": "yt_aaaa_001.jpg", "inliers": 16}, "yt_aaaa_003.jpg": {"with": "ddg_web.jpg", "inliers": 15},
            "yt_aaaa_004.jpg": {"with": None, "inliers": 0}, "mountainproject_a9.jpg": {"with": None, "inliers": 0},
            "vid_up_001.jpg": {"with": "ddg_web.jpg", "inliers": 200}}
    (mesh / "models.json").write_text(json.dumps({"models": [{"name": "0", "photos": [c["name"] for c in cams]}], "main": "0", "left": left}))
    own = {"a": {"mountainproject_a1.jpg", "yt_aaaa_001.jpg", "yt_aaaa_002.jpg", "yt_aaaa_003.jpg", "yt_aaaa_004.jpg", "yt_aaaa_099.jpg",
                 "mountainproject_a9.jpg"},
           "f": {"yt_ffff_001.jpg", "yt_ffff_002.jpg"}, "g": {"mountainproject_g1.jpg", "yt_gggg_001.jpg"}, "h": {"vid_up_001.jpg"}}
    logged = []
    out = viewpoints.compute(mesh, own, logged.append)
    a = out["climbs"]["a"]
    assert (a["photos"], a["of"]) == (2, 4), ("placed: its page photo and a frame; of: those, a tied frame and its other page photo", a)
    assert out["unplaced"] == {"g": 1, "h": 1} and "f" not in out["climbs"], ("frames with no evidence are nobody's", out)
    assert out["evidence"] is True and any("6 of the climbs' video frames count as nobody's own" in s for s in logged), logged
    assert viewpoints.poorly_placed(out) == {"a": (2, 4), "g": (0, 1), "h": (0, 1)}, viewpoints.poorly_placed(out)
    assert viewpoints.own_by_evidence({"yt_x_001.jpg"}, set(), None) == {"yt_x_001.jpg"}, "no ties recorded: all of them"
    # a models.json from before the ties were recorded: as before
    (mesh / "models.json").write_text(json.dumps({"models": [{"name": "0", "photos": [c["name"] for c in cams]}]}))
    out = viewpoints.compute(mesh, own, logged.append)
    assert (out["climbs"]["a"]["of"], out["unplaced"], out["evidence"]) == (7, {"f": 2, "g": 2, "h": 1}, False), out

# a climb's own photos: its page's and videos', not web searches', nor those an older harvest pooled from another climb
with tempfile.TemporaryDirectory() as td:
    proj = pathlib.Path(td)
    (proj / "photos").mkdir()
    for f in ("mountainproject_1.jpg", "yt_ab_001.jpg", "ddg_2.jpg", "mountainproject_9.jpg"):
        (proj / "photos" / f).write_bytes(b"x")
    (proj / "sources.json").write_text(json.dumps({"files": [{"file": "mountainproject_9.jpg", "pooled_from": "other"}]}))
    assert viewpoints.own_photos(proj) == {"mountainproject_1.jpg", "yt_ab_001.jpg"}
    assert viewpoints.own_photos(proj / "nothing") == set()

# --- which climbs a pooled mesh hardly shows: none of theirs placed, or fewer than 3 and a quarter of their own
views = {"climbs": {"a": {"photos": 30, "of": 40}, "b": {"photos": 4, "of": 40}, "c": {"photos": 2, "of": 4}, "d": {"photos": 3, "of": 3}},
         "unplaced": {"e": 12, "f": 0}}
assert viewpoints.poorly_placed(views) == {"b": (4, 40), "c": (2, 4), "e": (0, 12)}, viewpoints.poorly_placed(views)
assert viewpoints.poorly_placed({}) == {}
print("poorly placed OK")
print("ALL VIEWPOINT TESTS PASSED")
