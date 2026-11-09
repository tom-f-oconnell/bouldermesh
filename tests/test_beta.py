#!/usr/bin/env python3
"""Finding holds from climbers' hands and feet: cameras, rays, contacts and roles, on data built here.
Offline: the pose model and videos aren't used."""
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
import beta  # noqa: E402

close = lambda a, b, tol=1e-6: np.allclose(np.asarray(a, float), np.asarray(b, float), atol=tol)  # noqa: E731

# a camera 5 units in front of the origin, looking along +Z at it, with some radial distortion
cam = {"q": [1.0, 0.0, 0.0, 0.0], "t": [0.0, 0.0, 5.0], "lens": {"model": 2, "width": 640, "height": 480, "params": [500.0, 320.0, 240.0, 0.05]}}
X = [0.4, -0.3, 0.0]
uv = beta.project(cam, (640, 480), X)
origin, d = beta.camera_ray(cam, (640, 480), uv)
assert close(origin, [0, 0, -5]) and abs(np.linalg.norm(d) - 1) < 1e-9
s = (X[2] - origin[2]) / d[2]
assert close(origin + s * d, X, 1e-5), (origin + s * d, X)
uv2 = beta.project(cam, (1280, 960), X)
assert close(uv2, [2 * uv[0], 2 * uv[1]], 1e-6), "a photo at twice the size scales the lens with it"
assert beta.project(cam, (640, 480), [0, 0, -6]) is None, "behind the camera"
assert beta.lens({"q": [1, 0, 0, 0], "t": [0, 0, 0]}, (640, 480))[4], "without a lens kept, an approximate one"

# a ray meets the nearest of two squares, and misses beside them
V = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, -1], [1, 0, -1], [1, 1, -1], [0, 1, -1]], float)
F = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]])
mesh = beta.Mesh(V, F)
assert close(mesh.hit(np.array([0.25, 0.6, 2.0]), np.array([0, 0, -1.0])), [0.25, 0.6, 0]), "the nearer square"
assert mesh.hit(np.array([1.5, 0.5, 2.0]), np.array([0, 0, -1.0])) is None
assert mesh.hit(np.array([0.5, 0.5, 2.0]), np.array([0, 0, 1.0])) is None, "not behind the ray's start"

# contacts: runs of frames that hardly move, long enough
track = [(0.5, [0, 0, 0]), (1.0, [0.001, 0, 0]), (1.5, [0.002, 0, 0]), (2.0, [0.5, 0, 0]), (2.5, None),
         (3.0, [1, 1, 0]), (3.5, [1, 1.001, 0]), (4.0, [2, 2, 0])]
found = beta.contacts(track, still=0.01, min_frames=2)
assert [(c["t0"], c["t1"]) for c in found] == [(0.5, 1.5), (3.0, 3.5)], found
assert close(found[0]["p"], [0.001, 0, 0])
assert beta.contacts(track, still=0.01, min_frames=4) == [], "too short to count"

# roles: first hands start, last hands finish, other hands hand holds, feet alone foot holds, the ground not a hold
A, B, C, D, E, G = [0, 1, 0], [0.5, 1.5, 0], [0, 3, 0], [1, 2, 0], [0.5, 0.6, 0], [0.3, 0.0, 0]
c = lambda t0, t1, p: {"t0": t0, "t1": t1, "p": p}  # noqa: E731
got = beta.holds({"lh": [c(1, 3, A), c(5, 6, B), c(10, 12, C)], "rh": [c(1.2, 4, A), c(6, 7, D), c(11, 12, C)],
                  "lf": [c(0, 9, G), c(2, 4, E)], "rf": [c(7, 8, B)]}, radius=0.1, ground=0.0, ground_margin=0.2)
roles = {tuple(h["p"]): h["role"] for h in got}
assert roles == {tuple(map(float, A)): "start", tuple(map(float, B)): "hand", tuple(map(float, D)): "hand",
                 tuple(map(float, C)): "finish", tuple(map(float, E)): "foot"}, roles
b = next(h for h in got if h["role"] == "hand" and close(h["p"], B))
assert {x["limb"] for x in b["contacts"]} == {"lh", "rf"}, "a hand hold used as a foot stays a hand hold"
assert all(not close(h["p"], G) for h in got), "standing on the ground isn't a hold"
assert beta.holds({}, radius=0.1) == []
assert [h["role"] for h in beta.holds({"rf": [c(1, 2, E)]}, radius=0.1)] == ["foot"]

# hands reach past the wrist along the forearm; feet are their toes, or ankles without them
kp, sc = np.zeros((26, 2)), np.ones(26)
kp[7], kp[9] = [100, 100], [100, 200]      # left elbow above the wrist
kp[20], kp[15] = [300, 400], [310, 380]    # left big toe, ankle
pts = beta.limb_points(kp, sc)
assert close(pts["lh"], [100, 200 + 100 * beta.HAND_REACH]) and close(pts["lf"], [300, 400]), pts
sc[20] = 0.0
assert close(beta.limb_points(kp, sc)["lf"], [310, 380])
sc[7] = 0.0
assert beta.limb_points(kp, sc)["lh"] is None, "no elbow, no forearm to reach along"

# the climber: the largest well-found person, or the one nearest where they were
big, small = np.zeros((26, 2)), np.zeros((26, 2))
big[:, 0], big[:, 1] = np.linspace(0, 200, 26), np.linspace(0, 400, 26)
small[:, 0], small[:, 1] = np.linspace(500, 560, 26), np.linspace(500, 620, 26)
assert beta.climber(np.array([small, big]), np.ones((2, 26))) == 1
assert beta.climber(np.array([small, big]), np.vstack([np.ones(26), np.zeros(26)])) == 0, "only one found well"
assert beta.climber(np.zeros((0, 26, 2)), np.zeros((0, 26))) is None

# holds marked by hand are checked
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    assert beta.load_holds(d) == {"holds": []}
    saved = beta.save_holds(d, [{"p": [1, 2, 3], "role": "start"}, {"p": [0.5, 0, 0], "role": "foot"}])
    assert beta.load_holds(d)["holds"] == saved["holds"] == [{"p": [1.0, 2.0, 3.0], "role": "start"}, {"p": [0.5, 0.0, 0.0], "role": "foot"}]
    for bad in ([{"p": [1, 2], "role": "start"}], [{"p": [1, 2, 3], "role": "crimp"}], [{"p": "x", "role": "hand"}]):
        try:
            beta.save_holds(d, bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    beta.save(d, {"status": "running", "videos": [{"holds": [{}, {}]}]})
    assert beta.summary(beta.load(d)) == {"status": "running", "progress": None, "note": None, "waiting": None, "videos": 1, "holds": 2}
    beta.carry_on(d)
    assert beta.load(d)["status"] == "queued" and beta.load(d)["resume"], "a restart queues it again, keeping what's done"
    assert beta.videos(d) == {}, "no record of its photos"

# --- untextured meshes: the PLY a reconstruction keeps when texturing fails, binary or ASCII ---------------------
import pipeline  # noqa: E402
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    try:
        beta.Mesh.load(d)
        raise AssertionError("nothing to read")
    except ValueError:
        pass
    pipeline.write_ply(d / "mesh.ply", [(0, 0, 0), (2, 0, 0), (0, 2, 0), (2, 2, 0)], [(9, 9, 9)] * 4, [(0, 1, 2), (1, 3, 2)])
    m = beta.Mesh.load(d)
    assert m.F.tolist() == [[0, 1, 2], [1, 3, 2]] and close(m.V[3], [2, 2, 0]), (m.V, m.F)
    assert close(m.hit([0.5, 0.5, 5], [0, 0, -1]), [0.5, 0.5, 0]), "rays meet it like a GLB's"
    (d / "q.ply").write_text("ply\nformat ascii 1.0\nelement vertex 4\nproperty float x\nproperty float y\nproperty float z\n"
                             "element face 1\nproperty list uchar int vertex_indices\nend_header\n0 0 0\n1 0 0\n1 1 0\n0 1 0\n4 0 1 2 3\n")
    V, F = beta.read_ply(d / "q.ply")
    assert F.tolist() == [[0, 1, 2], [0, 2, 3]] and V.shape == (4, 3), "a quad becomes two triangles"

# --- YouTube refusing partway: it waits, and then does only the videos it hadn't ---------------------------------
from climbs import youtube  # noqa: E402
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    urls = [f"https://www.youtube.com/watch?v=v{i}" for i in range(3)]
    found = {u: {"tag": f"t{i}", "title": None, "uploader": None, "frames": {f"yt_t{i}_000.jpg": {}}} for i, u in enumerate(urls)}
    done, refuse = [], {urls[1]}

    def analyse(url, *a, **k):
        if url in refuse:
            raise youtube.Refused({"since": 0, "until": 1e10, "level": 0, "error": "not a bot"})
        done.append(url)
        return {"holds": [{"p": [0, 0, 0]}], "reference": None, "poses": {"f": url}, "with_climber": 1, "frames": 1, "placed_frames": 1}
    beta.videos, beta.Mesh.load, beta._pose_model, beta.analyse_video = (lambda _: found), (lambda _: None), (lambda _: None), analyse
    try:
        beta.find(d, d, lambda s: None)
        raise AssertionError("refused")
    except youtube.Refused:
        pass
    rec = beta.load(d)
    assert rec["status"] == "queued" and rec["waiting"] == 1e10 and [v["url"] for v in rec["videos"]] == urls[:1], rec
    refuse.clear()
    rec = beta.find(d, d, lambda s: None)
    assert done == [urls[0], urls[1], urls[2]] and rec["status"] == "done" and len(rec["videos"]) == 3, (done, rec)
    assert set(json.loads((d / beta.POSES_FILE).read_text())) == set(urls), "the first video's skeletons are kept too"
print("ALL BETA TESTS PASSED")
