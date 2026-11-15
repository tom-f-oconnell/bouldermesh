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


# --- a capture's scenery: its frames placed in the mesh's coordinates by its own photos the mesh placed ----------------
def quat_of(R) -> list[float]:
    """w, x, y, z of a rotation matrix (Shepperd's way: from its largest diagonal term, so turns of half a circle work)."""
    t = R[0, 0] + R[1, 1] + R[2, 2]
    if t > 0:
        r = math.sqrt(1 + t) * 2
        q = [r / 4, (R[2, 1] - R[1, 2]) / r, (R[0, 2] - R[2, 0]) / r, (R[1, 0] - R[0, 1]) / r]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        r = math.sqrt(1 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        q = [(R[2, 1] - R[1, 2]) / r, r / 4, (R[0, 1] + R[1, 0]) / r, (R[0, 2] + R[2, 0]) / r]
    elif R[1, 1] > R[2, 2]:
        r = math.sqrt(1 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        q = [(R[0, 2] - R[2, 0]) / r, (R[0, 1] + R[1, 0]) / r, r / 4, (R[1, 2] + R[2, 1]) / r]
    else:
        r = math.sqrt(1 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        q = [(R[1, 0] - R[0, 1]) / r, (R[0, 2] + R[2, 0]) / r, (R[1, 2] + R[2, 1]) / r, r / 4]
    return q


def yaw(deg: float):
    a = math.radians(deg)
    return np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]])


def looking(centre, at):
    """World to camera (x right, y down, z forward), held level, at `centre` looking at `at`."""
    f = np.asarray(at, float) - centre
    f /= np.linalg.norm(f)
    r = np.cross([0, -1.0, 0], f)
    r /= np.linalg.norm(r)
    d = np.cross(f, r)
    return np.array([r, d, f])


import math  # noqa: E402
import cv2  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    mesh, proj = td / "mesh", td / "proj"
    for d in (mesh, proj / "scenery", proj / "photos"):
        d.mkdir(parents=True)
    write_glb(mesh / "mesh.glb", np.array([[-1, 0, -1], [1, 0, -1], [0, 2, 1], [1, 2, 1]], float), np.array([0, 1, 2, 1, 3, 2]))
    # the tracking's world is the mesh's turned 30 degrees about up, at 0.8 of its size, and shifted
    S, Rm, T = 1.25, yaw(30), np.array([2.0, -0.5, 1.0])      # mesh = S Rm tracking + T
    lens = {"model": 1, "params": [500.0, 500.0, 320.0, 240.0], "width": 640, "height": 480}
    cams, shots = [], []
    for k in range(8):      # the capture's photos, all round the rock, as the mesh placed them and as the tracking had them
        cm = np.array([4 * math.sin(k * math.pi / 4), 1.0 + 0.1 * k, 4 * math.cos(k * math.pi / 4)])
        Rwc = looking(cm, [0, 1, 0])
        cams.append({"name": f"cap_x_{k:03d}.jpg", "q": quat_of(Rwc), "t": list(-Rwc @ cm), "lens": lens})
        ct = Rm.T @ (cm - T) / S
        Rcw_t = Rm.T @ Rwc.T            # image axes to the tracking's world
        spell = "a" if k < 6 else "a.2"     # a second spell with only two placed: not enough to tie
        shots.append({"name": f"cap_x_{k:03d}.jpg", "kept": True, "blob": {"ar": {"s": spell, "p": list(ct), "q": quat_of(Rcw_t)}}})
    (mesh / "cameras.json").write_text(json.dumps(cams))
    scenery = []
    for k, (spell, where) in enumerate([("a", [0, 1.5, 9.0]), ("a", [6, 1.5, 6]), ("a.2", [-9, 1.5, 0])]):
        cm = np.array(where, float)
        Rwc = looking(cm, cm + (cm - [0, 1.5, 0]))      # looking out, away from the rock
        ct, Rcw_t = Rm.T @ (cm - T) / S, Rm.T @ Rwc.T
        name = f"scn_x_{k + 1:03d}.jpg"
        cv2.imwrite(str(proj / "scenery" / name), np.full((480, 640, 3), 100, np.uint8))
        scenery.append({"name": name, "kind": "walk", "t": k, "ar": {"s": spell, "p": list(ct), "q": quat_of(Rcw_t)},
                        "k": {"fx": 500.0, "fy": 500.0, "cx": 320.0, "cy": 240.0, "w": 640, "h": 480}, "turn": 90})
    (proj / "capture.json").write_text(json.dumps({"v": 4, "shots": shots, "scenery": scenery}))
    pts = [(1.0, 0.5, 2.0, 1), (3.0, 0.0, -1.0, 1), (0.0, 0.0, 0.0, 2)]
    (proj / "capture-points.bin").write_bytes(b"BMP1" + struct.pack("<I", len(pts)) + b"".join(struct.pack("<fffH", *p) for p in pts))
    task = splat.capture_task(mesh, proj)
    assert task["ties"]["a"]["status"] == "tied" and task["ties"]["a"]["shared"] == 6 and task["ties"]["a.2"]["status"] == "untied", task["ties"]
    walk = [v for v in task["views"] if v["kind"] == "walk"]
    assert [v["name"] for v in walk] == ["scn_x_001.jpg", "scn_x_002.jpg"] and task["placed"] == 2 and task["frames"] == 3, "the untied spell's frame left out"
    for v, where in zip(walk, ([0, 1.5, 9.0], [6, 1.5, 6])):
        vm = np.asarray(v["viewmat"])
        centre = -vm[:3, :3].T @ vm[:3, 3]
        assert np.allclose(centre, where, atol=1e-4) and np.allclose(vm[:3, :3] @ (np.array(where) - [0, 1.5, 0]) / np.linalg.norm(np.array(where) - [0, 1.5, 0]),
                                                                    [0, 0, 1], atol=1e-4), (centre, where)
        assert v["refine"] and v["folder"] == "scenery" and v["size"] == [640, 480]
    photos = [v for v in task["views"] if v["kind"] == "photo"]
    assert len(photos) == 8 and not any(v["refine"] for v in photos), "the capture's own photos, as the mesh placed them, fixed"
    assert len(task["points"]) == 2 and np.allclose(task["points"][0], S * Rm @ [1.0, 0.5, 2.0] + T, atol=1e-4), "the tied spell's points, in the mesh's"
    assert task["box"] is not None
    # task_views reads them as the fitter wants them, shrunk
    (proj / "task.json").write_text(json.dumps(task))
    for c in cams:
        cv2.imwrite(str(proj / "photos" / c["name"]), np.full((480, 640, 3), 50, np.uint8))
    read = splat.task_views(proj, task, max_side=320)
    assert len(read) == 10 and read[0]["image"].shape == (240, 320, 3) and np.isclose(read[0]["K"][0, 0], 250.0)
    try:
        (proj / "capture.json").write_text(json.dumps({"v": 4, "shots": shots, "scenery": []}))
        splat.capture_task(mesh, proj)
        raise AssertionError("no scenery")
    except ValueError as e:
        assert "no frames" in str(e)
print("a capture's scenery placed OK")

# --- the turn's frames, stitched into a panorama of what's far off -----------------------------------------------------
def far(d):
    """What's far off, by which way: a colour from the direction's longitude, darker towards the sky."""
    lon = np.arctan2(d[..., 0], d[..., 2])
    return np.stack([127 + 120 * np.sin(lon), 127 + 120 * np.cos(lon), 200 - 100 * np.clip(d[..., 1], 0, 1)], axis=-1)


with tempfile.TemporaryDirectory() as td:
    job = Path(td)
    (job / "scenery").mkdir()
    K = np.array([[300.0, 0, 120], [0, 300.0, 160], [0, 0, 1]])
    views = []
    for k in range(24):
        R = looking(np.zeros(3), yaw(k * 15) @ [0, 0, 1.0])
        u, v = np.meshgrid(np.arange(240) + 0.5, np.arange(320) + 0.5)
        rays = np.stack([(u - K[0, 2]) / K[0, 0], (v - K[1, 2]) / K[1, 1], np.ones_like(u)], axis=-1) @ R      # camera to world
        rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
        cv2.imwrite(str(job / "scenery" / f"pano_x_{k:02d}.jpg"), far(rays)[..., ::-1].astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 98])
        vm = np.eye(4)
        vm[:3, :3] = R
        views.append({"name": f"pano_x_{k:02d}.jpg", "folder": "scenery", "kind": "pano", "K": K.tolist(), "size": [240, 320], "viewmat": vm.tolist()})
    # the rock: a box straight ahead along +Z, 3 m off, which is left out (it's near, and the mesh shows it)
    got = splat.panorama(job, {"views": views, "box": [[-0.5, -0.5, 2.5], [0.5, 1.0, 3.5]]}, job / splat.PANO_FILE, width=512)
    assert got["frames"] == 24 and 0.15 < got["covered"] < 0.6 and -9 < got["lat_deg"][0] < -6 and got["lat_deg"][1] > 20, got
    from PIL import Image
    pic = np.asarray(Image.open(job / splat.PANO_FILE)).astype(float)
    assert pic.shape == (256, 512, 4)
    lon = (np.arange(512) + 0.5) / 512 * 2 * np.pi - np.pi
    lat = np.pi / 2 - (np.arange(256) + 0.5) / 256 * np.pi
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    want = far(np.stack([np.cos(la) * np.sin(lo), np.sin(la), np.cos(la) * np.cos(lo)], axis=-1))
    solid = pic[..., 3] > 250
    err = np.abs(pic[..., :3] - want)[solid].mean()
    assert solid.mean() > 0.1 and err < 6, (solid.mean(), err)
    assert (pic[:20, :, 3] == 0).all() and (pic[-20:, :, 3] == 0).all(), "the sky and ground nobody looked at, left clear"
    assert (pic[lat < math.radians(-9)][..., 3] == 0).all(), "and the ground near by"
    ahead = (np.abs(lo) < 0.1) & (la > math.radians(-5)) & (la < math.radians(5))
    assert (pic[ahead][:, 3] == 0).all() and (pic[(np.abs(np.abs(lo) - math.pi) < 0.1) & (np.abs(la) < 0.08)][:, 3] > 200).all(), "the rock left out, what's behind kept"
print("the panorama OK")
print("ALL SPLAT TESTS PASSED")
