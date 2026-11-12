#!/usr/bin/env python3
"""Which way meshes face (heading.py), on a small scene built here with a known answer: photos placed north of a rock
looking south, one with its compass reading, and Mountain Project's text naming the face. Offline."""
from __future__ import annotations

import json
import math
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import heading  # noqa: E402

try:
    import numpy as np
    from PIL import Image
except ImportError:
    print("numpy or Pillow not installed: skipping")
    sys.exit(0)

# --- reading a face from text
for text, want in (("The majority of climbs here are on the east face. This one is in its center.", 90),
                   ("Descend off the north side. Climb the west face.", 270), ("Faces northwest, so it's shady.", 315),
                   ("On the N face of the boulder.", 0), ("A tall north-east arete.", 45), ("In Manx Boulders - Southeast Face.", 135),
                   ("Start on the boulder's south-west side.", 225), ("Facing the south, sit start.", 180)):
    got = heading.face_named(text)
    assert got and got[0] == want, (text, got)
for text in ("Hike up the trail to the east face.", "A N start.", "Nothing about it.", "", "Park at the east lot."):
    assert heading.face_named(text) is None, text
print("text OK")


def quaternion(R):
    """w, x, y, z of a rotation matrix."""
    w = math.sqrt(max(0.0, 1 + R[0, 0] + R[1, 1] + R[2, 2])) / 2
    x = math.copysign(math.sqrt(max(0.0, 1 + R[0, 0] - R[1, 1] - R[2, 2])) / 2, R[2, 1] - R[1, 2])
    y = math.copysign(math.sqrt(max(0.0, 1 - R[0, 0] + R[1, 1] - R[2, 2])) / 2, R[0, 2] - R[2, 0])
    z = math.copysign(math.sqrt(max(0.0, 1 - R[0, 0] - R[1, 1] + R[2, 2])) / 2, R[1, 0] - R[0, 1])
    return [w, x, y, z]


def camera_at(name, centre, look):
    """A COLMAP camera (world to camera: q, t) at centre, looking along look, upright (+Y up in the mesh)."""
    z = np.asarray(look, float) / np.linalg.norm(look)
    down = np.array([0.0, -1.0, 0.0])
    y = down - z * (down @ z)     # its down, as near the mesh's down as its view allows
    y /= np.linalg.norm(y)
    x = np.cross(y, z)
    R = np.array([x, y, z])
    return {"name": name, "q": quaternion(R), "t": (-R @ np.asarray(centre, float)).tolist()}


# The truth: the mesh's own -Z axis points east (heading 90), so its own -X points north. The climb's face faces north:
# its photos stand north of the rock (own -X), looking south (own +X).
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    mesh, photos = d / "mesh", d / "photos"
    mesh.mkdir(), photos.mkdir()
    cams = [camera_at(f"p{i}.jpg", (-6.0, 1.0, z), (6.0, -0.5, -z * 0.8)) for i, z in enumerate((-2.0, -0.5, 0.5, 2.0))]
    (mesh / "cameras.json").write_text(json.dumps(cams))
    for i in range(4):
        Image.new("RGB", (32, 24), (90, 90, 90)).save(photos / f"p{i}.jpg")
    exif = Image.Exif()
    exif[heading.GPS_IFD] = {heading.IMG_DIRECTION_REF: "T", heading.IMG_DIRECTION: 180.0}    # pointing south
    Image.new("RGB", (32, 24), (90, 90, 90)).save(photos / "p1.jpg", exif=exif.tobytes())

    near = lambda a, b, tol: abs((a - b + 180) % 360 - 180) <= tol    # noqa: E731
    compass = heading.from_compass(mesh, photos)
    assert compass and compass["photos"] == 1 and near(compass["deg"], 90, 6) and not compass["magnetic"], compass
    climbs = [{"id": "c1", "name": "Slab", "text": "Climb the north face, left of the crack.", "own": True}]
    text = heading.from_text(mesh, climbs)
    assert text and near(text["deg"], 90, 3) and text["climbs"][0]["words"] == "north face", text
    assert heading.from_text(mesh, [{**climbs[0], "own": False}]) is None, "a pooled mesh needs where that climb's seen from"
    print("compass and text OK")

    # a boulder's mesh: each climb by where it's seen from (views.json); two climbs on two faces agree
    (mesh / "views.json").write_text(json.dumps({"climbs": {"a": {"eye": [-5, 1, 0], "target": [0, 1, 0]},
                                                            "b": {"eye": [0, 1, -5], "target": [0, 1, 0]}}}))
    both = heading.from_text(mesh, [{"id": "a", "name": "A", "text": "North face.", "own": False},
                                    {"id": "b", "name": "B", "text": "On the east side.", "own": False}])
    assert both and near(both["deg"], 90, 1) and both["spread_deg"] < 1 and "A (North face), B (east side)" in both["detail"], both

    # the best there is: an edit, then the compass, then the text
    rec = heading.compute(mesh, photos, climbs)
    assert heading.current(mesh) and heading.best(rec)["source"] == "compass", rec
    assert heading.best({"text": text})["source"] == "text" and heading.best(None) is None
    rec = heading.set_edited(mesh, 450)
    assert heading.best(rec) == {"deg": 90.0, "source": "edited", "detail": "turned by hand"}, heading.best(rec)
    rec = heading.compute(mesh, photos, climbs)
    assert heading.best(rec)["source"] == "edited", "working it out again keeps an edit"
    assert heading.best(heading.set_edited(mesh, None))["source"] == "compass"
    for bad in ("east", float("nan"), True):
        try:
            heading.set_edited(mesh, bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    # the skyline's (skyline.py): before one photo's compass, kept when the rest is worked out again, after an edit,
    # and after the compass when several photos' agree
    sky = {"deg": 92.5, "photos": 7, "detail": "the skyline in 7 of its photos"}
    rec = heading.set_skyline(mesh, sky, {"version": 1, "why": None})
    assert heading.best(rec) == {"deg": 92.5, "source": "skyline", "detail": "the skyline in 7 of its photos"}, heading.best(rec)
    rec = heading.compute(mesh, photos, climbs)
    assert rec["skyline"] == sky and rec["skyline_note"]["why"] is None and heading.best(rec)["source"] == "skyline", rec
    assert heading.best(heading.set_edited(mesh, 10))["source"] == "edited"
    heading.set_edited(mesh, None)
    strong = {"deg": 95.0, "photos": 3, "spread_deg": 8.0}
    assert heading.best({"skyline": sky, "compass": strong})["source"] == "compass"
    assert heading.best({"skyline": sky, "compass": {**strong, "spread_deg": 30.0}})["source"] == "skyline", "three that disagree"
    heading.set_skyline(mesh, None, {"version": 1, "why": "its horizon is mostly near"})
    (photos / "p1.jpg").unlink()
    assert heading.best(heading.compute(mesh, photos, climbs))["source"] == "text", "without its compass reading"
    print("best OK")
print("ALL HEADING TESTS PASSED")
