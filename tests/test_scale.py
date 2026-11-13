#!/usr/bin/env python3
"""A mesh's scale: Mountain Project's height, and people and objects measured against a mesh, on scenes
built here, where the answer is known. Offline: the pose model isn't used."""
from __future__ import annotations

import json
import math
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
import beta  # noqa: E402
import scale  # noqa: E402

near = lambda a, b, rel=0.01: abs(a - b) <= rel * abs(b)  # noqa: E731

# ------------------------------------------------------------------ Mountain Project's height
mp = lambda kind="", **kw: scale.stated_height({"raw": {"type": kind}, **kw})  # noqa: E731
assert near(mp("Boulder, 15 ft (5 m)")["m"], 15 * 0.3048) and mp("Boulder, 15 ft (5 m)")["where"] == "type"
assert mp("Boulder, 100 ft (30 m)") is None, "not a boulder's height"
assert mp("Boulder") is None and mp() is None
got = mp("Boulder", description="Pull through slopers. About 16 ft high, with a flat landing.")
assert near(got["m"], 16 * 0.3048) and got["where"] == "description" and "16 ft high" in got["text"], got
assert near(mp(description="Slightly tall (tops out at 20-25 feet tall)")["m"], 22.5 * 0.3048)
assert mp(description="a jug about 8 feet high off the ground") is None, "a hold's height, not the problem's"


# ------------------------------------------------------------------ a scene: a wall, the ground, a camera
def grid(corner, a, b, n):
    """A square of n x n cells from corner, along vectors a and b."""
    corner, a, b = (np.asarray(v, float) for v in (corner, a, b))
    V = np.array([corner + a * i / n + b * j / n for j in range(n + 1) for i in range(n + 1)])
    F = []
    for j in range(n):
        for i in range(n):
            k = j * (n + 1) + i
            F += [[k, k + 1, k + n + 2], [k, k + n + 2, k + n + 1]]
    return V, np.array(F)


def scene():
    """A wall at z = 0 from y = -2 to 4, and the ground y = -2 in front of it, toward the camera at z = -5."""
    V1, F1 = grid([-5, -2, 0], [10, 0, 0], [0, 6, 0], 30)
    V2, F2 = grid([-5, -2, -6], [10, 0, 0], [0, 0, 6], 30)
    return beta.Mesh(np.vstack([V1, V2]), np.vstack([F1, F2 + len(V1)]))


def camera(pitch_deg=0.0, origin=(0.0, 0.0, -5.0)):
    """Looking along +Z with +Y up in its photos, tilted about its X by pitch_deg, from origin."""
    a = math.radians(pitch_deg)
    q = [0.0, 0.0, -math.sin(a / 2), math.cos(a / 2)]     # the tilt, after a half turn about Z
    R = beta._rotation(q)
    return {"q": q, "t": list(-R @ np.asarray(origin, float)),
            "lens": {"model": 2, "width": 1000, "height": 800, "params": [900.0, 500.0, 400.0, 0.02]}}


SIZE = (1000, 800)
mesh = scene()
cam = camera()


def body(height=1.0, at=(0.0, 0.0, 0.0), spread=True):
    """Halpe26 keypoints in 3D of someone `height` units tall at the proportions scale.SEGMENTS uses (wingspan
    the same as height): spread flat against the wall at z = at[2], arms out, or standing upright with feet
    at `at`, +Y up."""
    ua, fa, th, sh, to = (scale.SEGMENTS[k][1] * height for k in ("upper_arm", "forearm", "thigh", "shank", "torso"))
    x0, y0, z = at
    P = np.zeros((26, 3))
    P[:, 2] = z
    foot = y0 if not spread else y0 + 0.04 * height
    ankle = foot + 0.04 * height
    knee, hip = ankle + sh, ankle + sh + th
    neck = hip + to
    for i, x in ((15, -0.08), (16, 0.08), (13, -0.08), (14, 0.08), (11, -0.08), (12, 0.08)):
        P[i, 0] = x0 + x * height
    P[[15, 16], 1], P[[13, 14], 1], P[[11, 12], 1] = ankle, knee, hip
    P[19] = [x0, hip, z]
    P[18] = [x0, neck, z]
    P[17] = [x0, y0 + height, z]      # the top of the head
    P[[20, 22], :] = [x0 - 0.08 * height, foot, z]
    P[[21, 23], :] = [x0 + 0.08 * height, foot, z]
    P[[24, 25], 0], P[[24, 25], 1] = [x0 - 0.08 * height, x0 + 0.08 * height], foot
    if spread:     # arms straight out to the sides
        P[5], P[6] = [x0 - 0.1 * height, neck, z], [x0 + 0.1 * height, neck, z]
        P[7], P[8] = P[5] - [ua, 0, 0], P[6] + [ua, 0, 0]
        P[9], P[10] = P[7] - [fa, 0, 0], P[8] + [fa, 0, 0]
    else:          # hanging by the sides
        P[5], P[6] = [x0 - 0.1 * height, neck, z], [x0 + 0.1 * height, neck, z]
        P[7], P[8] = P[5] - [0, ua, 0], P[6] - [0, ua, 0]
        P[9], P[10] = P[7] - [0, fa, 0], P[8] - [0, fa, 0]
    for i in (0, 1, 2, 3, 4):
        P[i] = [x0, neck + 0.1 * height, z]
    return P


def keypoints(P, c=cam):
    return np.array([beta.project(c, SIZE, X) for X in P]), np.ones(len(P))


# ------------------------------------------------------------------ ray casting from one camera's view
view = mesh.view(cam, SIZE)
for uv in ((500, 400), (120, 700), (900, 50), (999, 799)):
    X, z, n = view.hit(uv, normal=True)
    Y = mesh.hit(*beta.camera_ray(cam, SIZE, uv))
    assert np.allclose(X, Y), (uv, X, Y)
    assert near(z, beta.depth(cam, X), 1e-9) and abs(np.linalg.norm(n) - 1) < 1e-9
X, z, n = view.hit((500, 50), normal=True)       # the top of the photo: the wall, 5 units away
assert abs(X[2]) < 1e-9 and near(z, 5.0) and abs(abs(n[2]) - 1) < 1e-9
X, z, n = view.hit((500, 790), normal=True)      # the bottom: the ground in front of it, which faces up
assert abs(X[1] + 2) < 1e-9 and abs(abs(n[1]) - 1) < 1e-9 and z < 5

# ------------------------------------------------------------------ a climber on the wall
P = body(1.0, at=(0.0, 0.5, -0.001))
kp, sc = keypoints(P)
m = scale.measure_person(view, kp, sc)
assert set(m["anchors"]) == {"lh", "rh", "lf", "rf"} and all(near(z, 5.001, 1e-3) for z in m["anchors"].values()), m["anchors"]
for name, (_, share, _) in scale.SEGMENTS.items():
    for length in m["segments"][name]:
        assert near(length * min(m["anchors"].values()), share * 1.0, 0.01), (name, length)
assert m["elbows"]["l"] > 175 and m["elbows"]["r"] > 175, "arms straight out"
assert set(m["behind"]) == {"5", "6", "18"} and m["standing"] is None, "spread on the wall isn't standing"
assert np.allclose(m["points"]["lh"], P[9] + beta.HAND_REACH * (P[9] - P[7]), atol=0.01), "the hand's point, on the wall"

# a video's climber: metres per unit is their height in metres over their height in units
rows = [{"photo": "f.jpg", "group": "https://v", "rank": 0, **m} for _ in range(5)]
est = scale.people_estimates(rows, {"https://v": {"height_cm": 174.0, "ape_index_cm": 0.0}})
video = next(e for e in est if e["kind"] == "video")
assert near(video["metres_per_unit"], 1.74, 0.02) and video["stated"] and video["n"] == 5, video
taller = next(e for e in scale.people_estimates(rows, {"https://v": {"height_cm": 190.0, "ape_index_cm": 0.0}}) if e["kind"] == "video")
assert near(taller["metres_per_unit"] / video["metres_per_unit"], 190 / 174, 0.01), "a taller climber: a bigger rock"
unknown = next(e for e in scale.people_estimates(rows, {}) if e["kind"] == "video")
assert not unknown["stated"] and unknown["sd"] > video["sd"], "an unknown height counts for less"
assert scale.people_estimates(rows[:2], {}) == [], "too few measurements"
one = {**rows[0], "anchors": {"lh": 5.0}}
assert scale.people_estimates([one] * 5, {}) == [], "one hand on the mesh alone doesn't say how far away they are"
far = [{**rows[0], "anchors": {"lh": 15.0, "rh": 15.1}}] + rows[:4]
assert near(next(e for e in scale.people_estimates(far, {"https://v": {"height_cm": 174.0, "ape_index_cm": 0.0}}))["metres_per_unit"], 1.74, 0.02), \
    "a frame whose hands met rock far behind is left out"
photos = scale.people_estimates([{**r, "group": f"photo:{i}.jpg"} for i, r in enumerate(rows)], {})
assert [e["kind"] for e in photos] == ["photos"], "climbers in single photos are pooled"
assert scale.people_estimates([{**r, "rank": 1} for r in rows], {}) == [], "only the climber's limbs: others may be on the ground"

# ------------------------------------------------------------------ someone standing, however the camera's tilted
for pitch in (0.0, 15.0):
    c = camera(pitch, origin=(0.0, -1.0, -5.0))
    v = mesh.view(c, SIZE)
    kp, sc = keypoints(body(1.6, at=(0.5, -2.0, -2.0), spread=False), c)
    got = scale.standing(v, kp, sc)
    assert got is not None and near(got, 1.6, 0.01), (pitch, got)
kp, sc = keypoints(body(1.6, at=(0.5, -2.0, -2.0), spread=False))
sc[17] = 0.0
assert scale.standing(view, kp, sc) is None, "no head"
lying = body(1.6, at=(0.0, -2.0, -2.0), spread=False)
lying[:, [0, 1]] = lying[:, [1, 0]] - [2.0, -2.0]      # turned on its side
assert scale.standing(view, *keypoints(lying)) is None, "not upright"
kp, sc = keypoints(body(1.0, at=(0.0, 0.5, -0.001), spread=False))
assert scale.standing(view, kp, sc) is None, "feet on the wall, not the ground"
est = scale.people_estimates([{"photo": "p.jpg", "group": "photo:p.jpg", "rank": 1, "segments": {}, "anchors": {},
                               "standing": 1.0} for _ in range(3)], {})
assert len(est) == 1 and est[0]["kind"] == "standing" and near(est[0]["metres_per_unit"], scale.HEIGHT_CM / 100)

# ------------------------------------------------------------------ something of known size standing on the ground
h, w, _ = scale.KNOWN["nalgene_narrow_32"]
X0, low = np.array([0.3, -2.0, -2.5]), camera(origin=(0.0, -1.2, -5.0))     # from nearer the ground, to see it
corners = [beta.project(low, SIZE, X0 + d) for d in ([-w / 2, 0, 0], [w / 2, h, 0])]
box = (min(corners[0][0], corners[1][0]), min(corners[0][1], corners[1][1]), max(corners[0][0], corners[1][0]), max(corners[0][1], corners[1][1]))
got = scale.measure_upright(mesh.view(low, SIZE), box)
assert near(got["height"], h, 0.02) and near(got["width"], w, 0.02), got
assert scale.measure_upright(view, (0, -900, 10, -800)) is None, "its base over the top of the wall, meeting nothing"

# a project's photos said to show it: found in two photos, plus something else upright, and a box that misses the ground
assert scale.known_object({"kind": "nalgene_narrow_32"}) == {"what": "a 32 oz narrow-mouth Nalgene", "height": h, "width": w}
assert scale.known_object({"height_cm": 30}) == {"what": "a bottle", "height": 0.3, "width": None}
assert scale.known_object({"height_cm": 500}) is None and scale.known_object({"kind": "anvil"}) is None and scale.known_object(None) is None
X1 = np.array([-0.4, -2.0, -2.0])
corners1 = [beta.project(low, SIZE, X1 + d) for d in ([-w / 2, 0, 0], [w / 2, h, 0])]
box1 = (min(c[0] for c in corners1), min(c[1] for c in corners1), max(c[0] for c in corners1), max(c[1] for c in corners1))
wide = (box[0] - 40, box[1], box[2] + 40, box[3])                       # something squat where it stands: not it
with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    for n in ("a.jpg", "b.jpg", "c.jpg"):
        (td / n).write_bytes(b"x")
    boxes = {"a.jpg": [box, wide], "b.jpg": [box1], "c.jpg": [(0, -900, 10, -800)]}
    seen = []
    found = scale.find_objects(td, td, ["a.jpg", "b.jpg", "c.jpg", "missing.jpg"], print,
                               detect=lambda f: (seen.append(f.name), [{"box": list(b), "score": 0.9} for b in boxes[f.name]])[1])
    assert sorted(found) == ["a.jpg", "b.jpg", "c.jpg"] and len(seen) == 3
    assert scale.find_objects(td, td, ["a.jpg"], print, detect=lambda f: 1 / 0) == found, "kept, not looked for again"
    est = scale.object_estimates(mesh, {n: low for n in found}, {n: SIZE for n in found}, found, scale.known_object({"kind": "nalgene_narrow_32"}))
    assert len(est) == 1 and est[0]["kind"] == "object" and est[0]["n"] == 2 and near(est[0]["metres_per_unit"], 1.0, 0.03), est
    combined = scale.combine(est + [{"kind": "stated", "metres_per_unit": 1.6, "sd": scale.METHOD_SD["stated"]}])
    assert combined["method"] == "object" and near(combined["metres_per_unit"], 1.0, 0.03), combined
    assert scale.object_estimates(mesh, {}, {}, found, scale.known_object({"height_cm": 23.5})) == [], "no cameras, nothing measured"

# ------------------------------------------------------------------ the rock's height, near where the photos look
cams = [camera(0, origin=(x, 0.0, -5.0)) for x in (-1.0, 0.0, 1.0)]
got = scale.heights(mesh, cams)
assert near(got["whole"], 6.0, 0.05) and near(got["around"], 6.0, 0.05), got
assert np.allclose(got["focus"], [0, 0, 0], atol=0.05)
keep = np.ones(len(mesh.v0), bool)
keep[mesh.c[:, 1] > 1.0] = False                     # cleanup removed the top of the wall
assert near(scale.heights(mesh, cams, keep)["whole"], 3.0, 0.05)
est = scale.stated_estimate([{"raw": {"type": "Boulder, 20 ft (6 m)"}}], got)
assert near(est["metres_per_unit"], 20 * 0.3048 / got["around"]) and est["kind"] == "stated"
assert scale.stated_estimate([{"raw": {"type": "Boulder"}}], got) is None

# ------------------------------------------------------------------ combining estimates
e = lambda k, v, sd: {"kind": k, "metres_per_unit": v, "sd": sd}  # noqa: E731
got = scale.combine([e("video", 2.0, 0.1), e("video", 2.0, 0.1)])
assert near(got["metres_per_unit"], 2.0) and got["low"] < 2.0 < got["high"] and got["method"] == "video"
assert got["sd"] >= scale.FLOOR_SD
got = scale.combine([e("video", 2.0, 0.1), e("stated", 4.0, 1.0)])
assert near(got["metres_per_unit"], 2.0) and got["method"] == "video", "a vague estimate counts for little"
got = scale.combine([e("video", 2.0, 0.1), e("video", 2.1, 0.1), e("video", 9.0, 0.1)])
assert 2.0 <= got["metres_per_unit"] <= 2.1 and got["high"] > 3, "one far off hardly moves it, but widens the range"
assert near(scale.combine([e("video", 2.0, 0.1), e("video", 3.0, 0.1)])["metres_per_unit"], math.sqrt(6)), "between two equal"
agree, disagree = scale.combine([e("video", 2.0, 0.1), e("video", 2.1, 0.1)]), scale.combine([e("video", 2.0, 0.1), e("video", 4.0, 0.1)])
assert disagree["sd"] > 2 * agree["sd"], "disagreeing estimates widen the range"
assert scale.combine([]) is None

# ------------------------------------------------------------------ measuring every photo, and beta's frames
P = body(1.0, at=(0.0, 0.5, -0.001))
kp, sc = keypoints(P)
poses = {"a.jpg": {"size": list(SIZE), "people": [{"kp": kp.tolist(), "sc": sc.tolist()}]}}
sources = {"a.jpg": {"file": "a.jpg", "source": "youtube", "url": "https://v"}}
rows = scale.measure(mesh, {"a.jpg": cam}, poses, sources)
assert len(rows) == 1 and rows[0]["group"] == "https://v" and rows[0]["rank"] == 0
shift = np.array([[1, 0, -40], [0, 1, 25], [0, 0, 1]], float)    # a frame 40 px right, 25 up of the placed one
frame = np.c_[kp, np.ones(len(kp))] @ np.linalg.inv(shift).T
bp = {"https://v": [{"t": 1.0, "kp": (frame[:, :2] / frame[:, 2:]).tolist(), "sc": sc.tolist(), "ref": "a.jpg", "H": shift.ravel().tolist()},
                    {"t": 1.5, "kp": kp.tolist(), "sc": sc.tolist(), "ref": None, "H": None}]}
rows = scale.measure(mesh, {"a.jpg": cam}, poses, sources, bp)
assert len(rows) == 2 and rows[1].get("beta"), "a frame matched no placed frame isn't measured"
for name in scale.SEGMENTS:
    assert np.allclose(rows[0]["segments"][name], rows[1]["segments"][name], rtol=1e-4), name

# ------------------------------------------------------------------ reach: hand, shoulder, shoulder, hand, while both hands hold on
P = body(1.0, at=(0.0, 0.5, -0.001))
kp, sc = keypoints(P)
hands = [P[w] + beta.HAND_REACH * (P[w] - P[e]) for w, e in ((9, 7), (10, 8))]
record = {"videos": [{"url": "https://v", "holds": [{"p": list(hands[0]), "contacts": [{"limb": "lh", "t0": 0.0, "t1": 9.0}]},
                                                    {"p": list(hands[1]), "contacts": [{"limb": "rh", "t0": 0.0, "t1": 5.0}]}]}]}
bent = kp.copy()
bent[9] = bent[7] + (bent[7] - bent[5])[::-1] * [1, -1]       # the left forearm at a right angle to the upper arm
frame = lambda t, k: {"t": t, "kp": k.tolist(), "sc": sc.tolist(), "ref": "a.jpg", "H": np.eye(3).ravel().tolist()}  # noqa: E731
frames = {"https://v": [frame(1.0, kp), frame(2.0, kp), frame(3.0, bent), frame(6.0, kp)]}
paths = scale.span_paths(mesh, {"a.jpg": cam}, {"a.jpg": SIZE}, record, frames, metres_per_unit=1000.0)
ua, fa = scale.SEGMENTS["upper_arm"][1], scale.SEGMENTS["forearm"][1]
assert len(paths["https://v"]) == 2, "not with an elbow bent, nor once a hand has let go"
assert all(near(x, 2 * (ua + fa + beta.HAND_REACH * fa) + 0.2, 0.01) for x in paths["https://v"]), paths
near_wall = scale.span_paths(mesh, {"a.jpg": cam}, {"a.jpg": SIZE}, record, frames, metres_per_unit=1.0)["https://v"][0]
assert near_wall > paths["https://v"][0], "shoulders held off the rock make the way between the hands longer"
est = scale.span_estimates({"https://v": [0.8] * 6}, {"https://v": {"height_cm": 170.0, "ape_index_cm": 10.0}})
assert near(est[0]["metres_per_unit"], scale.PALM_SPAN * 1.8 / 0.8) and est[0]["kind"] == "span", est
assert scale.span_estimates({"https://v": [0.8] * 2}, {}) == [], "too few frames"

# ------------------------------------------------------------------ a mesh, end to end
with tempfile.TemporaryDirectory() as tmp:
    d = pathlib.Path(tmp)
    (d / "cameras.json").write_text(json.dumps([{"name": f"yt_{i}.jpg", **c} for i, c in enumerate(cams)]))
    (d / "photo_sources.json").write_text(json.dumps({"photos": [{"file": f"yt_{i}.jpg", "source": "youtube", "url": "https://v"}
                                                                 for i in range(3)]}))
    found = {}
    for i, c in enumerate(cams):
        kp, sc = keypoints(body(1.0, at=(0.2 * i, 0.5, -0.001)), c)
        found[f"yt_{i}.jpg"] = {"size": list(SIZE), "people": [{"kp": kp.tolist(), "sc": sc.tolist()}]}
    (d / scale.POSES_FILE).write_text(json.dumps({"photos": found}))
    load, avail = beta.Mesh.load, scale.available
    beta.Mesh.load, scale.available = classmethod(lambda cls, path: mesh), lambda: False    # people already found are still measured
    try:
        record = scale.estimate(d, d / "photos", None, [{"raw": {"type": "Boulder, 20 ft (6 m)"}}],
                                {"https://v": {"height_cm": 174.0, "ape_index_cm": 0.0}}, lambda s: None)
    finally:
        beta.Mesh.load, scale.available = load, avail
    assert record["status"] == "done" and record["people"] == 3, record
    kinds = {x["kind"] for x in record["estimates"]}
    assert kinds == {"video", "stated"}, kinds
    assert record["method"] == "video" and record["low"] < record["metres_per_unit"] < record["high"]
    assert near(record["height_m"], record["heights"]["around"] * record["metres_per_unit"], 0.01)
    assert json.loads((d / scale.MEASURED_FILE).read_text())[0]["group"] == "https://v"
    assert scale.load(d)["status"] == "done"
    s = scale.summary(scale.load(d))
    assert s["videos"] == 1 and s["stated"]["m"] == round(20 * 0.3048, 2) and s["method"] == "video", s
    scale.save(d, {"status": "running", "progress": "x"})
    assert scale.carry_on(d) and scale.load(d)["status"] == "queued" and scale.load(d)["progress"] is None
    assert not scale.carry_on(d / "nothing"), "nothing to carry on"

# ------------------------------------------------------------------ people found in a photo its EXIF Orientation turned
try:
    from PIL import Image
except ImportError:
    Image = None
if Image is not None:
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        (d / "photos").mkdir()
        for n in ("turned.jpg", "upright.jpg"):
            Image.new("RGB", (60, 40)).save(d / "photos" / n)       # stored 60 wide, 40 high
        (d / "cameras.json").write_text(json.dumps([{"name": n, "q": [1, 0, 0, 0], "t": [0, 0, 0]} for n in ("turned.jpg", "upright.jpg", "gone.jpg")]))
        person = [{"kp": [[1.0, 2.0]] * 6, "sc": [0.9] * 6}]
        (d / scale.POSES_FILE).write_text(json.dumps({"photos": {"turned.jpg": {"size": [40, 60], "people": person},
                                                                 "upright.jpg": {"size": [60, 40], "people": person},
                                                                 "gone.jpg": {"size": [40, 60], "people": person}}}))
        found = scale.find_poses(d, d / "photos", None, lambda s: None, look=False)
        assert set(found) == {"upright.jpg", "gone.jpg"}, f"people found turned are dropped, to be found again: {sorted(found)}"
    print("turned photos OK")

print("ALL SCALE TESTS PASSED")
