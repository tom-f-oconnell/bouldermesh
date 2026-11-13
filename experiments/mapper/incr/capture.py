"""A capture from the phone apps, as app/FORMAT.md (v2) has it, read for the incremental pipeline (numpy, PIL only).

The geometry mirrors app/shared's Geometry.kt exactly (quaternions w, x, y, z; Similarity.apply = rot(p * scale) +
shift; CameraPose.into), so that a photo's camera here is the one the app drew: tests/test_incr_capture.py checks
them against the numbers GeometryTest.kt uses.

    readings(photo)                 the photo's BMSH1 readings (EXIF UserComment), or None
    record(path)                    capture.json
    points(path)                    capture-points.bin: (xyz in the tracking world, spell)
    pose(blob)                      a photo's camera in the tracking world: centre, rotation (image axes to world), lens
    into(cam, fit)                  that camera in the mesh's frame, through the record's fit (mesh to world)
    colmap(cam)                     COLMAP's (q w,x,y,z and t, world to camera) and a PINHOLE lens, for a pose
    spells(shots)                   the photos by spell of tracking (ar.s), in the order taken
"""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import numpy as np

UP = np.array([0.0, 1.0, 0.0])


# ---------------------------------------------------------------- Geometry.kt's quaternions and similarity

def qmul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


def qconj(q):
    return np.array([q[0], -q[1], -q[2], -q[3]])


def qunit(q):
    q = np.asarray(q, float)
    n = float(np.linalg.norm(q))
    return q / n if n > 1e-12 else np.array([1.0, 0.0, 0.0, 0.0])


def qrot(q, v):
    """v turned by q (Geometry.kt's Quat.rotate)."""
    w, qv = q[0], np.asarray(q[1:], float)
    v = np.asarray(v, float)
    t = np.cross(qv, v) * 2.0
    return v + t * w + np.cross(qv, t)


def qmatrix(q):
    """The rotation matrix of q: its columns are where it takes the x, y and z axes."""
    return np.stack([qrot(q, e) for e in np.eye(3)], axis=1)


def qfrom_matrix(m):
    """Geometry.kt's Quat.fromAxes, from a matrix's columns."""
    m = np.asarray(m, float)
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = [s / 4, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s]
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = [(m[2, 1] - m[1, 2]) / s, s / 4, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s]
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = [(m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, s / 4, (m[1, 2] + m[2, 1]) / s]
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = [(m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, s / 4]
    return qunit(q)


def axis_angle(axis, radians):
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    return np.array([math.cos(radians / 2), *(a * math.sin(radians / 2))])


class Similarity:
    """x' = rot(x * scale) + shift (Geometry.kt's Similarity); the record's fit takes the mesh's coordinates to the
    tracking world's."""

    def __init__(self, scale: float, rot, shift):
        self.scale, self.rot, self.shift = float(scale), qunit(rot), np.asarray(shift, float)

    @classmethod
    def of_fit(cls, fit: dict) -> "Similarity":
        return cls(fit["scale"], fit["rot"], fit["shift"])

    def apply(self, p):
        return qrot(self.rot, np.asarray(p, float) * self.scale) + self.shift

    def inverse(self) -> "Similarity":
        r = qconj(self.rot)
        return Similarity(1.0 / self.scale, r, qrot(r, -self.shift) / self.scale)


class Pose:
    """A camera as a photo was taken (Geometry.kt's CameraPose): its centre, its turn from image axes (x along the
    stored rows, y down the columns, z the way it looks) to the world's, and its lens {fx, fy, cx, cy, w, h}."""

    def __init__(self, centre, rot, lens: dict):
        self.centre, self.rot, self.lens = np.asarray(centre, float), qunit(rot), lens

    def project(self, p):
        c = qrot(qconj(self.rot), np.asarray(p, float) - self.centre)
        if c[2] <= 1e-6:
            return None
        return self.lens["fx"] * c[0] / c[2] + self.lens["cx"], self.lens["fy"] * c[1] / c[2] + self.lens["cy"], c[2]

    def into(self, s: Similarity) -> "Pose":
        """The same camera in the coordinates s comes from (a mesh's, for s its mesh-to-world fit)."""
        inv = s.inverse()
        return Pose(inv.apply(self.centre), qunit(qmul(inv.rot, self.rot)), self.lens)


def look(frm, at, lens: dict) -> Pose:
    """A camera at frm looking at `at`, held level (the app's tests' TestWorld.look)."""
    f = np.asarray(at, float) - np.asarray(frm, float)
    f /= np.linalg.norm(f)
    right = np.cross(f, UP)
    right /= np.linalg.norm(right)
    return Pose(frm, qfrom_matrix(np.stack([right, np.cross(f, right), f], axis=1)), lens)


# ---------------------------------------------------------------- the files

def readings(photo: Path) -> dict | None:
    """A photo's BMSH1 readings, from its EXIF UserComment."""
    from PIL import Image
    with Image.open(photo) as im:
        exif = im.getexif()
        raw = exif.get_ifd(0x8769).get(0x9286) if exif else None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw[8:] if raw[:8] in (b"ASCII\0\0\0", b"UNICODE\0", b"\0" * 8) else raw
        raw = raw.decode("utf-8", "replace")
    raw = raw.strip("\0 ")
    if not raw.startswith("BMSH1:"):
        return None
    return json.loads(raw[len("BMSH1:"):])


def record(path: Path) -> dict:
    r = json.loads(Path(path).read_text())
    if r.get("v") not in (1, 2):
        raise ValueError(f"a capture record of version {r.get('v')}: this reads 1 and 2")
    return r


def points(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """capture-points.bin: "BMP1", uint32 n, then n of (float32 x, y, z; uint16 spell)."""
    data = Path(path).read_bytes()
    if data[:4] != b"BMP1":
        raise ValueError(f"{path} isn't a BMP1 points file")
    n = struct.unpack_from("<I", data, 4)[0]
    rows = np.frombuffer(data, np.dtype([("p", "<f4", 3), ("s", "<u2")]), n, 8)
    return rows["p"].astype(np.float64), rows["s"].astype(np.int64)


def pose(blob: dict) -> Pose | None:
    """A photo's camera in its spell of tracking's world, from its readings (ar.p, ar.q, k), or None without them."""
    ar, k = blob.get("ar"), blob.get("k")
    if not ar or not k or ar.get("p") is None or ar.get("q") is None:
        return None
    return Pose(ar["p"], ar["q"], k)


def into(cam: Pose, fit: dict) -> Pose:
    """A camera in the tracking world, in the mesh's frame, through the record's fit (mesh to world)."""
    return cam.into(Similarity.of_fit(fit))


def colmap(cam: Pose) -> tuple[list[float], list[float], dict]:
    """COLMAP's world-to-camera q (w, x, y, z) and t for a pose, and its lens as a PINHOLE camera (model 1)."""
    q = qconj(cam.rot)          # world to camera: the inverse turn
    t = -qrot(q, cam.centre)
    k = cam.lens
    return list(map(float, q)), list(map(float, t)), {"model": 1, "width": int(k["w"]), "height": int(k["h"]),
                                                      "params": [float(k["fx"]), float(k["fy"]), float(k["cx"]), float(k["cy"])]}


def spell_of(blob: dict) -> str:
    """Which spell of tracking a photo is from: its ar.s, whose ".2", ".3"... mark where tracking was lost a while."""
    return str((blob.get("ar") or {}).get("s") or "")


def spells(shots: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for sh in sorted(shots, key=lambda s: (s.get("blob") or {}).get("t") or 0):
        if sh.get("kept", True) and sh.get("blob"):
            out.setdefault(spell_of(sh["blob"]), []).append(sh)
    return out


def down_in_model(blob: dict, R_world_to_cam) -> np.ndarray | None:
    """A photo's `down` (in its picture's axes) in the model's frame: R^T down, R the model's world-to-camera turn."""
    d = blob.get("down")
    if d is None:
        return None
    v = np.asarray(R_world_to_cam, float).T @ np.asarray(d, float)
    return v / np.linalg.norm(v)


def level(downs: list[np.ndarray]) -> dict | None:
    """Which way is down in a model by its photos' readings: their mean, and how far they spread about it (median and
    90th percentile, degrees), and how far it is from the model's own -Y."""
    if not downs:
        return None
    D = np.array(downs)
    m = D.mean(axis=0)
    m /= np.linalg.norm(m)
    off = np.degrees(np.arccos(np.clip(D @ m, -1, 1)))
    tilt = float(np.degrees(np.arccos(np.clip(m @ -UP, -1, 1))))
    return {"down": [round(float(x), 6) for x in m], "n": len(D), "spread_median_deg": round(float(np.median(off)), 3),
            "spread_p90_deg": round(float(np.percentile(off, 90)), 3), "tilt_from_model_deg": round(tilt, 3),
            # how far the mean of n readings may be off, if each is off by about their spread
            "mean_err_deg": round(float(np.median(off) / math.sqrt(len(D))), 3)}
