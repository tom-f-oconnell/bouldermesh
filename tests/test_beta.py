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
    bare = lambda holds: [{"p": h["p"], "role": h["role"]} for h in holds]      # noqa: E731
    assert beta.load_holds(d)["holds"] == saved["holds"] and bare(saved["holds"]) == [{"p": [1.0, 2.0, 3.0], "role": "start"}, {"p": [0.5, 0.0, 0.0], "role": "foot"}]
    # each hold has an id, the same whenever a hold there is read, and kept when it's given; how far round it reaches is kept within limits
    assert [h["id"] for h in saved["holds"]] == [beta.hold_id([1, 2, 3]), beta.hold_id([0.5, 0, 0])] and len(saved["holds"][0]["id"]) == 8
    again = beta._checked([{"p": [1, 2, 3.5], "role": "hand", "id": saved["holds"][0]["id"], "r_m": 0.5}, {"p": [1, 2, 3.5], "role": "hand", "id": saved["holds"][0]["id"]},
                           {"p": [4, 4, 4], "role": "foot", "id": "not an id", "r_m": 0.05}])
    assert again[0]["id"] == saved["holds"][0]["id"] and again[0]["r_m"] == beta.HOLD_R_MOST and again[1]["id"] != again[0]["id"] and "r_m" not in again[1]
    assert again[2]["id"] == beta.hold_id([4, 4, 4]) and again[2]["r_m"] == 0.05
    for bad in ([{"p": [1, 2], "role": "start"}], [{"p": [1, 2, 3], "role": "crimp"}], [{"p": "x", "role": "hand"}]):
        try:
            beta.save_holds(d, bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    # sets of beta: the holds marked before there were sets are the first; new ones blank, or copies of a set or of a video's
    (first,) = beta.load_sets(d)
    assert first["name"] == "Marked holds" and first["holds"] == saved["holds"] and (d / beta.SETS_FILE).exists()
    with tempfile.TemporaryDirectory() as old:
        old = pathlib.Path(old)
        (old / beta.HOLDS_FILE).write_text(json.dumps({"holds": [{"p": [1, 2, 3], "role": "hand"}], "updated": 5.0}))
        (legacy,) = beta.load_sets(old)
        assert legacy["id"] == "marked" and bare(legacy["holds"]) == [{"p": [1.0, 2.0, 3.0], "role": "hand"}] and not (old / beta.SETS_FILE).exists(), "read as a set, not rewritten"
        assert beta.save_holds(old, [])["holds"] == [] and beta.load_sets(old)[0]["id"] == "marked" and (old / beta.SETS_FILE).exists(), "kept as one once changed"
    blank = beta.new_set(d, author="  tom  ")
    assert blank["name"] == "Beta" and blank["holds"] == [] and blank["author"] == "tom" and blank["from"] is None and len(blank["id"]) == 8
    assert beta.new_set(d)["name"] == "Beta 2" and beta.new_set(d, name="Sit start", climb="c1")["climb"] == "c1"
    copy = beta.new_set(d, source={"set": first["id"]})
    assert copy["name"] == "Marked holds (copy)" and copy["holds"] == first["holds"] and copy["from"] == {"set": first["id"]} and copy["holds"] is not first["holds"]
    beta.save(d, {"status": "done", "videos": [{"url": "https://example.org/v", "tag": "abcd1234", "title": "A send",
                                                "holds": [{"p": [0, 1, 2], "role": "start", "limbs": ["lh"]}, {"p": [0, 2, 2], "role": "odd"}]}]})
    seen = beta.new_set(d, source={"video": "abcd1234"})
    assert seen["name"] == "From A send" and bare(seen["holds"]) == [{"p": [0.0, 1.0, 2.0], "role": "start"}] and seen["from"] == {"video": "https://example.org/v"}
    for bad in ({"set": "nope"}, {"video": "nope"}):
        try:
            beta.new_set(d, source=bad)
            raise AssertionError(f"copied {bad}")
        except ValueError:
            pass
    changed = beta.update_set(d, copy["id"], {"name": "Left exit", "holds": copy["holds"] + [{"p": [9, 9, 9], "role": "finish"}], "climb": "c2"})
    assert changed["name"] == "Left exit" and len(changed["holds"]) == 3 and changed["climb"] == "c2" and changed["updated"] >= copy["updated"]
    assert beta.load_holds(d)["holds"] == saved["holds"], "the first set is untouched by the others"
    try:
        beta.update_set(d, copy["id"], {"holds": [{"p": [1], "role": "start"}]})
        raise AssertionError("accepted a bad hold")
    except ValueError:
        pass
    assert [s["name"] for s in beta.load_sets(d)] == ["Marked holds", "Beta", "Beta 2", "Sit start", "Left exit", "From A send"]
    assert beta.delete_set(d, blank["id"]) and not beta.delete_set(d, blank["id"]) and len(beta.load_sets(d)) == 5
    beta.save(d, {"status": "running", "videos": [{"holds": [{}, {}]}]})
    assert beta.summary(beta.load(d)) == {"status": "running", "progress": None, "at": None, "note": None, "waiting": None, "videos": 1, "holds": 2}
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
        a[-1]("frame 1 of 4", (0, 4))
        a[-1]("frame 3 of 4", (2, 4))
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
    told = []
    rec = beta.find(d, d, lambda s: None, report=lambda text, at: told.append((text, at)))
    assert done == [urls[0], urls[1], urls[2]] and rec["status"] == "done" and len(rec["videos"]) == 3, (done, rec)
    assert set(json.loads((d / beta.POSES_FILE).read_text())) == set(urls), "the first video's skeletons are kept too"
    # how far it got, for the bar: videos done (one before the refusal), and frames of the one it's on
    texts, ats = [t for t, _ in told], [a for _, a in told]
    assert texts[:3] == ["video 2 of 3: downloading", "video 2 of 3: frame 1 of 4", "video 2 of 3: frame 3 of 4"], texts
    assert [a["frac"] for a in ats] == [0.3333, 0.3333, 0.5, 0.6667, 0.6667, 0.8333], [a["frac"] for a in ats]
    assert ats[2] == {"done_videos": 1, "videos": 3, "frame": 2, "frames": 4, "frac": 0.5, "started": ats[2]["started"], "frac0": 0.3333}
    assert rec["at"] is None, "none once done"
print("ALL BETA TESTS PASSED")
