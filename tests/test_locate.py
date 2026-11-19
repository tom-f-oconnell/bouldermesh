#!/usr/bin/env python3
"""Placing a picture against a mesh's locate pack (backend/locate.py): a made-up textured wall seen from three
cameras, two of them a COLMAP model kept as a mesh keeps one, the third placed against the pack made from those.

    python3 tests/test_locate.py
"""
from __future__ import annotations

import io
import json
import pathlib
import struct
import sys
import tarfile
import tempfile

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
import locate  # noqa: E402

W, H, F = 960, 720, 800.0
TEX = 1024              # the wall's texture, in pixels, over 2 x 2 units from -1 to 1


def texture() -> np.ndarray:
    rng = np.random.default_rng(3)
    t = rng.integers(0, 255, (TEX // 8, TEX // 8), dtype=np.uint8)
    t = cv2.resize(t, (TEX, TEX), interpolation=cv2.INTER_CUBIC)
    return cv2.GaussianBlur(t, (5, 5), 1.0)


def camera(yaw_deg: float, x: float):
    """World-to-camera R, t of a camera 3 units in front of the wall (z = 0), at x, turned by yaw to look at it."""
    a = np.radians(yaw_deg)
    R = np.array([[np.cos(a), 0, -np.sin(a)], [0, 1, 0], [np.sin(a), 0, np.cos(a)]])
    c = np.array([x, 0.0, -3.0])
    return R, -R @ c


def K():
    return np.array([[F, 0, W / 2 - 0.5], [0, F, H / 2 - 0.5], [0, 0, 1]])


def render(tex, R, t):
    """The wall as the camera sees it: the texture's pixels taken to the plane's x, y, then into the picture."""
    A = np.array([[2 / TEX, 0, -1], [0, 2 / TEX, -1], [0, 0, 1]])
    Hm = K() @ np.column_stack([R[:, 0], R[:, 1], t]) @ A
    return cv2.warpPerspective(tex, Hm, (W, H))


def on_wall(xy, R, t):
    """Where pictured pixels (OpenCV's) land on the wall."""
    rays = (np.linalg.inv(K()) @ np.column_stack([xy, np.ones(len(xy))]).T).T @ R      # in world, from the centre
    c = -R.T @ t
    s = -c[2] / rays[:, 2]
    return c + rays * s[:, None]


def quat(R):
    w = np.sqrt(max(1e-12, 1 + R[0, 0] + R[1, 1] + R[2, 2])) / 2
    return (w, (R[2, 1] - R[1, 2]) / (4 * w), (R[0, 2] - R[2, 0]) / (4 * w), (R[1, 0] - R[0, 1]) / (4 * w))


def model(views, mesh_dir: pathlib.Path) -> None:
    """A COLMAP model of the photos (name, picture, R, t): each SIFT keypoint a point of its own, where it is on the
    wall, seen where it was found; kept as reconstruction.py keeps one."""
    pts, imgs = [], []
    for i, (name, pic, R, t) in enumerate(views):
        kps = cv2.SIFT_create(nfeatures=1500).detect(pic, None)
        xy = np.array([k.pt for k in kps])
        obs = []
        for p, xyz in zip(xy, on_wall(xy, R, t)):
            pts.append((len(pts) + 1, xyz, i + 1, len(obs)))
            obs.append((p[0] + 0.5, p[1] + 0.5, len(pts)))
        imgs.append((i + 1, quat(R), t, name, obs))
    cams = struct.pack("<Q", 1) + struct.pack("<iiQQ", 1, 1, W, H) + struct.pack("<4d", F, F, W / 2, H / 2)
    ib = struct.pack("<Q", len(imgs))
    for iid, q, t, name, obs in imgs:
        ib += struct.pack("<i4d3di", iid, *q, *t, 1) + name.encode() + b"\0" + struct.pack("<Q", len(obs))
        ib += b"".join(struct.pack("<ddq", *o) for o in obs)
    pb = struct.pack("<Q", len(pts))
    for pid, xyz, iid, k in pts:
        pb += struct.pack("<Q3d3Bd", pid, *xyz, 128, 128, 128, 0.5) + struct.pack("<Q", 1) + struct.pack("<ii", iid, k)
    with tarfile.open(mesh_dir / "reconstruction.tar.gz", "w:gz") as tar:
        for n, b in (("model/cameras.bin", cams), ("model/images.bin", ib), ("model/points3D.bin", pb)):
            info = tarfile.TarInfo(n); info.size = len(b)
            tar.addfile(info, io.BytesIO(b))


def test_places_a_new_view() -> None:
    tex = texture()
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        photos = d / "photos"; photos.mkdir()
        views = []
        for name, yaw, x in (("a.jpg", 8, -0.4), ("b.jpg", -8, 0.4)):
            R, t = camera(yaw, x)
            pic = render(tex, R, t)
            cv2.imwrite(str(photos / name), pic, [cv2.IMWRITE_JPEG_QUALITY, 95])
            views.append((name, pic, R, t))
        model(views, d)
        info = locate.build_pack(d, [d / "nowhere", photos])
        assert info["photos_found"] == 2 and info["points"] > 300, info
        assert json.loads((d / "locate.json").read_text())["entries"] == info["entries"]
        pack = locate.load_pack(d / "locate.bin")
        assert pack["desc"].shape == (info["entries"], 128) and pack["xyz"].shape == (info["points"], 3)

        R, t = camera(3, 0.1)
        got = locate.place(pack, render(tex, R, t), {"fx": F, "fy": F, "cx": W / 2 - 0.5, "cy": H / 2 - 0.5})
        assert got is not None and got["inliers"] >= locate.MIN_INLIERS, got
        Rg, tg = np.array(got["R"]), np.array(got["t"])
        off = np.linalg.norm(-Rg.T @ tg - (-R.T @ t))
        turn = np.degrees(np.arccos(np.clip((np.trace(Rg @ R.T) - 1) / 2, -1, 1)))
        assert off < 0.02 and turn < 0.5, (off, turn)

        # a picture of something else places nothing
        noise = np.random.default_rng(9).integers(0, 255, (H, W), dtype=np.uint8)
        assert locate.place(pack, noise, {"fx": F, "fy": F, "cx": W / 2, "cy": H / 2}) is None


def test_no_reconstruction() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            locate.build_pack(pathlib.Path(tmp), [])
        except locate.LocateError:
            return
        raise AssertionError("built a pack with no reconstruction")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
