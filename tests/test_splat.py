#!/usr/bin/env python3
"""Splats of the scene around a mesh (backend/splat.py), the parts that run without a GPU: COLMAP's cameras and lenses,
the .splat file the viewer reads, where the rock is, what the photos give the fitter (with OpenCV), and that fitting
says why it can't run here. The fitting itself needs CUDA torch and gsplat, and is tried by hand (splat.py's docstring).

    python3 tests/test_splat.py
"""
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import splat  # noqa: E402


def write_glb(path: Path, P, I) -> None:
    """A plain GLB of one primitive."""
    blob = bytearray()
    views = []
    for data in (P.astype("<f4"), I.astype("<u4")):
        blob += b"\0" * (-len(blob) % 4)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": data.nbytes})
        blob += data.tobytes()
    j = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
         "buffers": [{"byteLength": len(blob)}], "bufferViews": views,
         "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(P), "type": "VEC3", "min": P.min(0).tolist(), "max": P.max(0).tolist()},
                       {"bufferView": 1, "componentType": 5125, "count": I.size, "type": "SCALAR"}],
         "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}]}
    text = json.dumps(j).encode()
    text += b" " * (-len(text) % 4)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(text) + 8 + len(blob)))
        f.write(struct.pack("<II", len(text), 0x4E4F534A) + text + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))


# a camera's quaternion is world to camera: the identity looks down +z from the origin
R = np.array(splat.rotation([1, 0, 0, 0]))
assert np.allclose(R, np.eye(3))
half = np.sqrt(0.5)
R = np.array(splat.rotation([half, 0, half, 0]))     # 90 degrees about y
assert np.allclose(R @ R.T, np.eye(3)) and np.allclose(R @ [0, 0, 1], [1, 0, 0]), R

# lenses: SIMPLE_RADIAL's one focal length and k1; OPENCV's two and four coefficients; scaled for fitting smaller
K, dist, Ks = splat.intrinsics({"model": 2, "width": 1280, "height": 720, "params": [1000, 640, 360, -0.1]}, 0.5)
assert np.allclose(K, [[1000, 0, 640], [0, 1000, 360], [0, 0, 1]]) and np.allclose(dist, [-0.1, 0, 0, 0])
assert np.allclose(Ks, [[500, 0, 320], [0, 500, 180], [0, 0, 1]]), Ks
K, dist, _ = splat.intrinsics({"model": 4, "width": 10, "height": 10, "params": [9, 8, 5, 4, .1, .2, .3, .4]}, 1)
assert K[0, 0] == 9 and K[1, 1] == 8 and np.allclose(dist, [.1, .2, .3, .4])
try:
    splat.intrinsics({"model": 8, "params": []}, 1)
    raise AssertionError("an unknown lens")
except ValueError:
    pass
print("cameras OK")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    # the .splat rows: 32 bytes, as GaussianSplats3D reads them (rotation bytes w, x, y, z)
    means = np.array([[1, 2, 3], [-1, 0, .5]], np.float32)
    scales = np.array([[.1, .2, .3], [1, 1, 1]], np.float32)
    quats = np.array([[1, 0, 0, 0], [half, 0, 0, half]], np.float32)
    splat.write(td / "s.splat", means, scales, quats, np.array([[1, .5, 0], [0, 0, 1]], np.float32), np.array([1, .25], np.float32))
    raw = (td / "s.splat").read_bytes()
    assert len(raw) == 64 and not list(td.glob("*.partial"))
    row = struct.unpack("<3f3f4B4B", raw[32:])
    assert np.allclose(row[:3], [-1, 0, .5]) and np.allclose(row[3:6], [1, 1, 1]), row
    assert row[6:10] == (0, 0, 255, 64), row[6:10]
    w, x, y, z = ((b - 128) / 128 for b in row[10:14])
    assert abs(w - half) < .01 and abs(z - half) < .01 and abs(x) < .01 and abs(y) < .01, row[10:14]
    assert struct.unpack("<3f3f4B4B", raw[:32])[10:14] == (255, 128, 128, 128)

    # where the rock is: its vertices' 5th to 95th percentiles and a margin, so that a stray far off doesn't stretch it
    mesh = td / "mesh"
    mesh.mkdir()
    assert splat.rock_box(mesh) is None
    g = np.linspace(0, 1, 21)
    P = np.array([[x, y, 0] for x in g for y in g] + [[50, 50, 50]], float)
    write_glb(mesh / "mesh.glb", P, np.array([0, 1, 21]))
    lo, hi = splat.rock_box(mesh)
    assert np.all(lo < [0.1, 0.1, 0.01]) and np.all(hi < [1.2, 1.2, 1]) and np.all(hi > [0.9, 0.9, -0.01]), (lo, hi)     # the stray at (50, 50, 50) left out

    # starting points: the mesh's vertices, and a shell well outside the cameras and the rock
    cams = [{"name": "a.jpg", "q": [1, 0, 0, 0], "t": [0, 0, 2]}, {"name": "b.jpg", "q": [1, 0, 0, 0], "t": [-1, 0, 2]}]
    pts, centre, radius = splat.starting_points(mesh, cams, n_mesh=100, n_shell=200)
    assert len(pts) == 100 + 200 and radius > 0
    far = np.linalg.norm(pts[100:] - centre, axis=1)
    assert far.min() > splat.SHELL * radius * 0.79, (far.min(), radius)

    # what the photos give the fitter: undistorted, shrunk, and where a person is, left out
    try:
        import cv2
    except ImportError:
        cv2 = None
    if cv2 is not None:
        photos, masks = td / "photos", td / "masks"
        photos.mkdir()
        masks.mkdir()
        img = np.zeros((400, 600, 3), np.uint8)
        img[:, :300] = (0, 0, 255)      # red on the left, in OpenCV's BGR
        cv2.imwrite(str(photos / "a.jpg"), img)
        cv2.imwrite(str(photos / "b.jpg"), img)
        m = np.full((400, 600), 255, np.uint8)
        m[:, 500:] = 0                  # a person on the right
        cv2.imwrite(str(masks / "a.jpg.png"), m)
        try:        # a photo whose EXIF says to show it turned: COLMAP placed it, and its lens is, in the stored pixels
            from PIL import Image
            exif = Image.Exif()
            exif[0x0112] = 6
            Image.fromarray(img[:, :, ::-1]).save(photos / "turned.jpg", exif=exif.tobytes())
        except ImportError:
            pass
        lens = {"model": 1, "width": 600, "height": 400, "params": [500, 500, 300, 200]}
        (mesh / "cameras.json").write_text(json.dumps([{**c, "lens": lens} for c in cams] + [{"name": "gone.jpg", "q": [1, 0, 0, 0], "t": [0, 0, 0], "lens": lens}]))
        v = splat.views(mesh, photos, masks, max_side=300)
        assert [x["name"] for x in v] == ["a.jpg", "b.jpg"], "a photo that's gone is left out"
        a = v[0]
        assert a["image"].shape == (200, 300, 3) and a["image"].flags["C_CONTIGUOUS"]
        assert a["image"][100, 10, 0] > .9 and a["image"][100, 10, 2] < .1, "RGB, not OpenCV's BGR"
        assert not a["valid"][100, 290] and a["valid"][100, 10] and v[1]["valid"].all(), "the masked person"
        assert np.allclose(a["K"], [[250, 0, 150], [0, 250, 100], [0, 0, 1]]) and np.allclose(a["viewmat"][:3, 3], [0, 0, 2])
        if (photos / "turned.jpg").exists():
            (mesh / "cameras.json").write_text(json.dumps([{"name": "turned.jpg", "q": [1, 0, 0, 0], "t": [0, 0, 2], "lens": lens}]))
            t = splat.views(mesh, photos, None, max_side=300)[0]["image"]
            assert t.shape == (200, 300, 3), t.shape
            assert t[150, 10, 0] > .9 and t[50, 290, 0] < .1, "read as its EXIF Orientation turns it, not as stored (red on the left)"
        print("photos OK")
    else:
        print("OpenCV not installed: skipping the photos")

    # without CUDA torch and gsplat, fitting says why and does nothing
    why = splat.unavailable()
    if why:
        try:
            splat.fit(mesh, td, None)
            raise AssertionError("fitted without gsplat")
        except RuntimeError as e:
            assert why in str(e) and not (mesh / splat.FILE).exists(), e
    assert splat.enabled() is (os.environ.get("SPLATS") == "1")
print("ALL SPLAT TESTS PASSED")
