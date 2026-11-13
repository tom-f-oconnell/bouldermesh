"""Which way is down in a model, and how that's known.

COLMAP's model_orientation_aligner assumes most photos were taken upright, so its estimate leans if
they all look up at one face. iPhones record the real direction of gravity in each photo's maker note,
so where a placed photo has one, it can level the model, and check that estimate.

  orientation.json    how a mesh was levelled, both directions in its own coordinates, and the check
  cameras.json        where its photos were placed, so that it can be checked again later

design.md has the measurements.
"""
from __future__ import annotations

import importlib.util
import json
import math
import struct
import time
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
FILE = "orientation.json"
CAMERAS_FILE = "cameras.json"
PHOTO_EXT = {".jpg", ".jpeg"}
MAX_DISAGREEMENT = 45.0  # degrees from the estimate past which a reading is more likely wrong than the estimate
EXIF_IFD, MAKER_NOTE, ORIENTATION = 0x8769, 0x927C, 0x0112


def available() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("numpy", "PIL"))


def apple_acceleration(note: bytes) -> list[float] | None:
    """The AccelerationVector in an iPhone's maker note: tag 8, three rationals, in units of g."""
    if not note.startswith(b"Apple iOS"):
        return None
    try:
        for i in range(struct.unpack(">H", note[14:16])[0]):
            tag, kind, count, offset = struct.unpack(">HHII", note[16 + i * 12:28 + i * 12])
            if tag == 8 and kind == 10 and count == 3:
                pairs = [struct.unpack(">ii", note[offset + k * 8:offset + k * 8 + 8]) for k in range(3)]
                return [a / b if b else 0.0 for a, b in pairs]
    except struct.error:
        pass
    return None


def down_in_image(v: list[float], tag: int | None) -> list[float] | None:
    """Gravity in a stored photo's axes: x along its rows, y down its columns, z the way the camera looks.

    The reading points up, and is in the phone's axes: +x to its left side, +y to its bottom, +z into
    its screen. A photo's pixels start as the sensor saw them, which is upright with the phone on its
    side, top to the left, where the phone's y and x are the image's x and y.

    Args:
        v: the reading.
        tag: the file's EXIF orientation. 3, 6 or 8 mean the pixels are still as the sensor saw them,
            which is what COLMAP uses. 1 or none on a photo taken any other way mean a website has
            turned its pixels upright since, so they're turned to match.

    Returns:
        A unit vector, or None if it can't be trusted: a mirrored file, a phone that was moving, or one
        pointing nearly straight up or down in a file that may have been turned.
    """
    if tag in (2, 4, 5, 7):
        return None
    size = math.sqrt(sum(x * x for x in v))
    if not 0.8 < size < 1.2:
        return None
    x, y, z = -v[1] / size, -v[0] / size, -v[2] / size
    if tag in (3, 6, 8):
        return [x, y, z]
    if math.hypot(x, y) < 0.35:
        return None
    for _ in range(4):      # a quarter turn at a time, until most of it is down the image
        if y >= abs(x):
            return [x, y, z]
        x, y = -y, x
    return None


def reading(path: Path) -> dict | None:
    """A photo's gravity reading, as BoulderMesh's apps record it with theirs (phonedata.py) or else an iPhone's own:
    "down" in image axes, and how far it looked up, in degrees."""
    from PIL import Image
    import phonedata
    down = phonedata.down(phonedata.read(path))
    if down:
        return {"down": down, "pitch": math.degrees(math.asin(max(-1.0, min(1.0, -down[2])))), "from": "app"}
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not len(exif):
                return None
            note = exif.get_ifd(EXIF_IFD).get(MAKER_NOTE)
            tag = exif.get(ORIENTATION)
    except Exception:
        return None
    v = apple_acceleration(bytes(note)) if note else None
    down = down_in_image(v, tag) if v else None
    if not down:
        return None
    return {"down": down, "pitch": math.degrees(math.asin(max(-1.0, min(1.0, -down[2]))))}


CAMERA_PARAMS = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12}   # COLMAP camera model id: parameters


def read_lenses(model: Path) -> dict[int, dict]:
    """A COLMAP model's cameras.bin, by camera id: its model id (2 is SIMPLE_RADIAL: f, cx, cy, k),
    image size and parameters, in pixels of the photo it was found for."""
    out = {}
    path = model / "cameras.bin"
    if not path.exists():
        return out
    with open(path, "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            cam_id, model_id = struct.unpack("<ii", f.read(8))
            width, height = struct.unpack("<QQ", f.read(16))
            params = struct.unpack(f"<{CAMERA_PARAMS[model_id]}d", f.read(8 * CAMERA_PARAMS[model_id]))
            out[cam_id] = {"model": model_id, "width": width, "height": height, "params": list(params)}
    return out


def read_cameras(model: Path) -> list[dict]:
    """The placed photos of a COLMAP model, from its images.bin: name, rotation and translation
    (world to camera), and the lens it was taken with, where cameras.bin has it."""
    out, lenses = [], read_lenses(model)
    with open(model / "images.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(4)
            q = struct.unpack("<4d", f.read(32))
            t = struct.unpack("<3d", f.read(24))
            cam_id = struct.unpack("<i", f.read(4))[0]
            name = b""
            while (c := f.read(1)) != b"\0":
                name += c
            f.seek(24 * struct.unpack("<Q", f.read(8))[0], 1)
            out.append({"name": name.decode(), "q": list(q), "t": list(t), **({"lens": lenses[cam_id]} if cam_id in lenses else {})})
    return out


def _matrix(q):
    """World to camera, from a quaternion w, x, y, z."""
    import numpy as np
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def _angle(a, b) -> float:
    import numpy as np
    return float(np.degrees(np.arccos(np.clip(np.dot(a, b), -1.0, 1.0))))


def level_quaternion(down) -> tuple[float, float, float, float]:
    """The shortest rotation (w, x, y, z) that takes this direction to -Y, which is down in a levelled model."""
    import numpy as np
    d = np.asarray(down, float)
    d = d / np.linalg.norm(d)
    target = np.array([0.0, -1.0, 0.0])
    axis = np.cross(d, target)
    w = 1.0 + float(d @ target)
    if w < 1e-9:    # exactly upside down: any axis across it will do
        return (0.0, 1.0, 0.0, 0.0)
    q = np.array([w, *axis])
    return tuple(float(x) for x in q / np.linalg.norm(q))


def _rotate(q, v):
    import numpy as np
    return _matrix(q) @ np.asarray(v, float)


def check(model: Path, images: Path) -> dict:
    """Compare a levelled model's down, which is -Y, with the readings of its placed photos.

    Returns:
        The report for orientation.json, before any change to the model. If "iphone" has status
        "used", level_quaternion(report["iphone"]["down"]) is the correction to apply.
    """
    import numpy as np
    cameras = read_cameras(model)
    down = np.array([0.0, -1.0, 0.0])
    edges = np.array([_matrix(c["q"]).T @ [0, 1, 0] for c in cameras]) if cameras else np.zeros((0, 3))
    agree = float(np.median(np.degrees(np.arccos(np.clip(edges @ down, -1, 1))))) if len(edges) else None
    placed = {c["name"] for c in cameras}
    readings, kept = [], 0
    for p in sorted(images.iterdir()) if images.is_dir() else []:
        if p.suffix.lower() not in PHOTO_EXT or p.name.startswith("yt_"):
            continue
        r = reading(p)
        if not r:
            continue
        kept += 1
        if p.name in placed:
            c = next(c for c in cameras if c["name"] == p.name)
            d = _matrix(c["q"]).T @ np.asarray(r["down"])
            readings.append({"photo": p.name, "pitch_deg": round(r["pitch"], 1), "down": d})
    report = {"created": time.time(), "method": "photos", "placed": len(cameras), "cameras_kept": True,
              "estimate": {"down": [0.0, -1.0, 0.0], "photos_agree_deg": None if agree is None else round(agree, 1)},
              "iphone": {"status": "no_readings", "kept_with_readings": kept, "placed_with_readings": len(readings)}}
    if not readings:
        return report
    ds = np.array([r["down"] for r in readings])
    mean = np.median(ds, axis=0) if len(ds) >= 3 else ds.mean(axis=0)
    mean = mean / np.linalg.norm(mean)
    off = _angle(mean, down)
    report["iphone"].update({
        "down": [float(x) for x in mean], "off_estimate_deg": round(off, 1),
        "readings_agree_deg": round(max(_angle(d, mean) for d in ds), 1),
        "readings": [{"photo": r["photo"], "pitch_deg": r["pitch_deg"], "off_estimate_deg": round(_angle(r["down"], down), 1)} for r in readings]})
    report["iphone"]["status"] = "used" if off <= MAX_DISAGREEMENT else "rejected"
    return report


def after_levelling(report: dict, q) -> dict:
    """The report once the model has been turned by q, so that both directions are in the mesh's coordinates."""
    report["method"] = "iphone"
    report["estimate"]["down"] = [float(x) for x in _rotate(q, report["estimate"]["down"])]
    report["iphone"]["down"] = [float(x) for x in _rotate(q, report["iphone"]["down"])]
    return report


def no_cameras(images: Path) -> dict:
    """The report for a mesh whose cameras weren't kept: its readings can't be related to it."""
    kept = 0
    if images.is_dir():
        kept = sum(1 for p in images.iterdir() if p.suffix.lower() in PHOTO_EXT and not p.name.startswith("yt_") and reading(p))
    return {"created": time.time(), "method": "photos", "cameras_kept": False, "estimate": {"down": [0.0, -1.0, 0.0], "photos_agree_deg": None},
            "iphone": {"status": "no_cameras", "kept_with_readings": kept}}


def save(mesh_dir: Path, report: dict, model: Path | None = None) -> None:
    """Keep a mesh's report, and its cameras if there's a model to take them from."""
    mesh_dir.mkdir(parents=True, exist_ok=True)
    if model is not None:
        (mesh_dir / CAMERAS_FILE).write_text(json.dumps(read_cameras(model)))
    tmp = mesh_dir / (FILE + ".partial")
    tmp.write_text(json.dumps(report))
    tmp.replace(mesh_dir / FILE)


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((mesh_dir / FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
