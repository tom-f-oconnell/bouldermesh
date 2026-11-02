#!/usr/bin/env python3
"""iPhone gravity readings, and comparing them with a model's down, on files built here. Offline."""
from __future__ import annotations

import math
import pathlib
import struct
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import orientation as o  # noqa: E402

if not o.available():
    print("numpy or Pillow not installed: skipping (the app then keeps the estimate)")
    sys.exit(0)
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402


def maker_note(v: list[float]) -> bytes:
    """An iPhone's maker note holding one tag, as they lay them out."""
    head = b"Apple iOS\x00\x00\x01MM"
    offset = len(head) + 2 + 12 + 4
    return (head + struct.pack(">H", 1) + struct.pack(">HHII", 8, 10, 3, offset) + b"\0\0\0\0"
            + b"".join(struct.pack(">ii", int(x * 1_000_000), 1_000_000) for x in v))


def photo(path: pathlib.Path, v: list[float] | None, tag: int | None = None, size=(120, 90)) -> pathlib.Path:
    im = Image.new("RGB", size, (90, 90, 90))
    exif = Image.Exif()
    if tag is not None:
        exif[o.ORIENTATION] = tag
    if v is not None:
        exif.get_ifd(o.EXIF_IFD)[o.MAKER_NOTE] = maker_note(v)
    im.save(path, "JPEG", exif=exif)
    return path


def close(a, b, tol=1e-6) -> bool:
    return all(abs(x - y) < tol for x, y in zip(a, b))


# --- what a reading means: it points up, in the phone's axes
assert o.apple_acceleration(maker_note([-0.99, 0.04, 0.07])) == [-0.99, 0.04, 0.07]
assert o.apple_acceleration(b"Canon stuff") is None and o.apple_acceleration(b"Apple iOS\x00\x00\x01MM\x00\x05") is None
assert close(o.down_in_image([-1, 0, 0], 1), [0, 1, 0]), "phone on its side, top to the left: the sensor's own way up"
assert close(o.down_in_image([-1, 0, 0], None), [0, 1, 0]), "no flag means the same"
# the pixels are still as the sensor saw them, and the flag says how to show them: COLMAP uses the pixels
assert close(o.down_in_image([0, -1, 0], 6), [1, 0, 0]), "held upright: down is along the sensor's rows"
assert close(o.down_in_image([1, 0, 0], 3), [0, -1, 0]), "the other way round: down is up the sensor's columns"
assert close(o.down_in_image([0, 1, 0], 8), [-1, 0, 0])
# no flag on a photo not taken the sensor's way up: a website turned its pixels upright since
for v in ([0, -1, 0], [1, 0, 0], [0, 1, 0]):
    for tag in (1, None):
        assert close(o.down_in_image(v, tag), [0, 1, 0]), (v, tag)
# looking up: gravity has a part against the way the camera looks; turning keeps that, and any lean
up_30 = [-math.cos(math.radians(30)), 0, math.sin(math.radians(30))]
assert close(o.down_in_image(up_30, 1), [0, math.cos(math.radians(30)), -0.5])
lean = o.down_in_image([0.1, -0.99, 0.1], 1)
assert lean[1] > 0.9 and abs(lean[0]) > 0.05, "turned upright, with its small lean kept"
assert abs(math.sqrt(sum(x * x for x in lean)) - 1) < 1e-9
# what can't be trusted
assert o.down_in_image([-1, 0, 0], 2) is None, "mirrored"
assert o.down_in_image([-0.5, 0, 0], 1) is None and o.down_in_image([-1.5, 0, 0], 1) is None, "moving"
assert o.down_in_image([-0.1, 0, 0.99], 1) is None, "pointing up, in a file that may have been turned: which way is unclear"
assert o.down_in_image([-0.1, 0, 0.99], 6) is not None, "with a flag, the file hasn't been turned"
print("reading OK")

with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    r = o.reading(photo(d / "a.jpg", up_30, 1))
    assert close(r["down"], [0, math.cos(math.radians(30)), -0.5]) and abs(r["pitch"] - 30) < 1e-3, r
    assert o.reading(photo(d / "none.jpg", None, 1)) is None and o.reading(photo(d / "bare.jpg", None)) is None
    (d / "bad.jpg").write_bytes(b"not an image")
    assert o.reading(d / "bad.jpg") is None
print("files OK")


# --- comparing with a model. Cameras here are a model frame's; gravity is whatever each test says it is
def rotation_about_x(deg: float):
    a = math.radians(deg)
    return np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])


def images_bin(path: pathlib.Path, cameras: dict) -> None:
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(cameras)))
        for i, (name, q) in enumerate(cameras.items()):
            f.write(struct.pack("<I", i + 1) + struct.pack("<4d", *q) + struct.pack("<3d", 1.0, 2.0, 3.0) + struct.pack("<I", 1))
            f.write(name.encode() + b"\0" + struct.pack("<Q", 2) + b"\0" * 48)


def reading_for(R, true_down) -> list[float]:
    """What an iPhone with this camera rotation records, given gravity in the model's axes."""
    g = R @ np.asarray(true_down, float)     # down in the camera's axes: image x, y, z
    return [-float(g[1]), -float(g[0]), -float(g[2])]   # the phone's axes, pointing up


with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    model, images = d / "aligned", d / "photos"
    model.mkdir()
    images.mkdir()
    # true down is 12 degrees from the model's, whose -Y is the estimate: the cameras all look up
    true_down = rotation_about_x(12) @ np.array([0.0, -1.0, 0.0])
    # upright cameras, whose bottom edges point along the model's -Y: half a turn about X
    q_cam = [0.0, 1.0, 0.0, 0.0]
    cam = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])
    cams = {"ddg_a.jpg": q_cam, "ddg_b.jpg": q_cam, "ddg_none.jpg": q_cam, "yt_x_001.jpg": q_cam}
    images_bin(model / "images.bin", cams)
    cameras = o.read_cameras(model)
    assert [c["name"] for c in cameras] == list(cams) and close(cameras[0]["t"], [1.0, 2.0, 3.0]), cameras[0]
    assert close(np.ravel(o._matrix(cameras[0]["q"])), np.ravel(cam)), "quaternion to matrix"

    # no readings at all
    photo(images / "ddg_none.jpg", None, 1)
    r = o.check(model, images)
    assert r["method"] == "photos" and r["iphone"]["status"] == "no_readings" and r["placed"] == 4 and r["estimate"]["photos_agree_deg"] == 0.0, r
    # readings on photos that weren't placed don't count, but are mentioned
    photo(images / "ddg_notplaced.jpg", [-1.0, 0, 0], 1)
    r = o.check(model, images)
    assert r["iphone"]["status"] == "no_readings" and r["iphone"]["kept_with_readings"] == 1 and r["iphone"]["placed_with_readings"] == 0, r

    # placed readings: found to be 12 degrees off, and the correction takes them to straight down
    photo(images / "ddg_a.jpg", reading_for(cam, true_down), 1)
    photo(images / "ddg_b.jpg", reading_for(cam, true_down), 1)
    r = o.check(model, images)
    p = r["iphone"]
    assert p["status"] == "used" and abs(p["off_estimate_deg"] - 12) < 0.2 and p["readings_agree_deg"] < 0.2, p
    assert np.allclose(p["down"], true_down, atol=1e-2), (p["down"], true_down)
    assert [x["photo"] for x in p["readings"]] == ["ddg_a.jpg", "ddg_b.jpg"], p["readings"]
    q = o.level_quaternion(p["down"])
    assert np.allclose(o._rotate(q, p["down"]), [0, -1, 0], atol=1e-9)
    assert abs(math.sqrt(sum(x * x for x in q)) - 1) < 1e-9
    r2 = o.after_levelling(r, q)
    assert r2["method"] == "iphone" and np.allclose(r2["iphone"]["down"], [0, -1, 0], atol=1e-9)
    assert abs(o._angle(r2["estimate"]["down"], r2["iphone"]["down"]) - p["off_estimate_deg"]) < 0.1, "the same angle apart after turning"
    assert o.level_quaternion([0, -1, 0]) == (1.0, 0.0, 0.0, 0.0), "already level: no turn"
    assert np.allclose(o._rotate(o.level_quaternion([0, 1, 0]), [0, 1, 0]), [0, -1, 0], atol=1e-9), "upside down: still gets there"

    # a reading too far from the estimate isn't believed
    far = rotation_about_x(70) @ np.array([0.0, -1.0, 0.0])
    photo(images / "ddg_a.jpg", reading_for(cam, far), 6)
    photo(images / "ddg_b.jpg", reading_for(cam, far), 6)
    r = o.check(model, images)
    assert r["iphone"]["status"] == "rejected" and abs(r["iphone"]["off_estimate_deg"] - 70) < 0.5, r["iphone"]

    # one bad reading among three is outvoted by the median
    photo(images / "ddg_a.jpg", reading_for(cam, true_down), 1)
    photo(images / "ddg_b.jpg", reading_for(cam, true_down), 1)
    photo(images / "ddg_none.jpg", reading_for(cam, far), 6)
    r = o.check(model, images)
    assert r["iphone"]["status"] == "used" and abs(r["iphone"]["off_estimate_deg"] - 12) < 1 and r["iphone"]["readings_agree_deg"] > 40, r["iphone"]

    # old meshes: cameras gone, so all that can be said is what their photos hold
    r = o.no_cameras(images)
    assert r["iphone"] == {"status": "no_cameras", "kept_with_readings": 4} and not r["cameras_kept"], r
    assert o.no_cameras(d / "missing")["iphone"]["kept_with_readings"] == 0

    # kept with the mesh, for checking again later
    out = d / "mesh"
    o.save(out, r2, model)
    assert o.load(out)["method"] == "iphone" and (out / o.CAMERAS_FILE).exists()
    assert o.load(d / "nothing") is None
    o.save(d / "mesh2", r)
    assert not (d / "mesh2" / o.CAMERAS_FILE).exists()
print("comparing OK")
print("ALL ORIENTATION TESTS PASSED")
