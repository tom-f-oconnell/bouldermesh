#!/usr/bin/env python3
"""Climbs' lines from their videos' climbers (betalines.py): runs, the line videos agree on with the ones that are off
left out, laying it on a mesh, where its name goes, and when lines are made again. On tracks and a wall built here.
Offline.

    python3 tests/test_betalines.py
"""
from __future__ import annotations

import json
import os
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
import betalines as bl  # noqa: E402

rng = np.random.default_rng(7)
SIZE = 4.0


def track(points, t0=0.0, wobble=0.03, each=6):
    """A centre track along the straight legs between points: each leg `each` frames, half a second apart, wobbling."""
    out, t = [], t0
    for i, (a, b) in enumerate(zip(points, points[1:])):
        for k in range(each + (i == len(points) - 2)):     # the last leg to its end
            p = np.asarray(a, float) + (np.asarray(b, float) - a) * k / each + rng.normal(0, wobble, 3) * [1, 1, 0]
            out.append([round(t, 2), *p])
            t += 0.5
    return out


# ---- runs: pieces joined over a moment's gap, apart over a jump; standing, and there and back, aren't climbs
up = track([[1, 0.5, 0], [1, 2, 0]])
on = track([[1, 2.1, 0], [1, 3.5, 0]], t0=up[-1][0] + 2)                    # lost for 2 s, found again just above
far = track([[3, 0.5, 0], [3, 3, 0]], t0=on[-1][0] + 1)                     # found again 2 away: another run
got = bl.runs([up, on, far], SIZE)
assert len(got) == 2, [len(r) for r in got]
assert abs(got[0][0][1] - 0.5) < 0.15 and abs(got[0][-1][1] - 3.5) < 0.15, "the first two pieces are one run, bottom to top"
steps = np.linalg.norm(np.diff(got[0], axis=0), axis=1)
assert np.allclose(steps, steps[0], rtol=0.2) and abs(steps[0] - bl.STEP * SIZE) < 0.4 * bl.STEP * SIZE, "points evenly along it"
assert bl.runs([track([[2, 1, 0], [2.05, 1.02, 0], [2, 1, 0], [2.03, 1, 0]], each=10)], SIZE) == [], "standing"
assert bl.runs([track([[2, 1, 0], [2, 3, 0], [2, 1.1, 0]])], SIZE) == [], "up and back down ends up nowhere"
assert bl.runs([], SIZE) == [] and bl.runs([[[0, 0, 0, 0]]], SIZE) == []

# ---- a climb's line: where its videos agree. Three go up x = 1; one of them walks in from the right first; a fourth
# is off on its own (another climb, or matched to the wrong place); one of both this climb and another agrees too
walked = [track([[3.2, 0.5, 0], [1, 0.5, 0], [1, 3.5, 0]], each=8)]
videos = [{"url": "a1", "centre": walked},
          {"url": "a2", "centre": [track([[1.05, 0.6, 0], [0.95, 3.4, 0]], each=14)]},
          {"url": "a3", "centre": [track([[0.95, 0.5, 0], [1.05, 2.0, 0]], each=8), track([[1.05, 2.1, 0], [1, 3.5, 0]], t0=6, each=8)]},
          {"url": "off", "centre": [track([[3.5, 0.5, 0], [3.6, 3.5, 0]], each=14)]},
          {"url": "both", "centre": [track([[1, 0.6, 0], [1, 3.3, 0]], each=14)]},
          {"url": "b1", "centre": [track([[2, 0.5, 0], [2.6, 3.0, 0]], each=14), track([[2.2, 0.4, 0], [2.25, 0.45, 0]], t0=30)]},
          {"url": "noise", "centre": []}]
of = {"A": ["a1", "a2", "a3", "off", "both"], "B": ["b1", "both"], "C": ["both"], "D": []}
found = bl.lines(videos, of, SIZE)
assert set(found) == {"A", "B"}, f"C's only video is of two climbs, D has none: {set(found)}"
A = found["A"]["points"]
assert np.abs(A[:, 0] - 1).max() < 0.2, f"up x = 1, without the walk in or the video that's off: {A[:, 0].min():.2f}..{A[:, 0].max():.2f}"
assert min(A[0][1], A[-1][1]) < 0.8 and max(A[0][1], A[-1][1]) > 3.2, "from its bottom to its top"
assert (found["A"]["videos"], found["A"]["agree"]) == (5, 4), found["A"]
wobble = np.abs(np.diff(A[:, 0], 2)).max()
assert wobble < 0.02, f"smooth: no one video's wobble ({wobble:.3f})"
B = found["B"]["points"]
assert (found["B"]["videos"], found["B"]["agree"]) == (2, 1), "the video of both climbs goes up A, not B"
assert abs(B[0][0] - 2) < 0.2 and abs(B[-1][0] - 2.6) < 0.2 and len(B) > 20, "one video of its own: its run that goes somewhere"

# two videos that never agree: the one that ends up furthest, rather than none
apart = bl.climb_line([bl.runs([track([[0.5, 0.5, 0], [0.5, 1.5, 0]], each=10)], SIZE)], [], SIZE)
assert apart is not None and apart["agree"] == 1
two = bl.climb_line([bl.runs([track([[0.5, 0.5, 0], [0.5, 1.5, 0]], each=10)], SIZE), bl.runs([track([[3, 0.5, 0], [3, 3.5, 0]], each=14)], SIZE)], [], SIZE)
assert abs(two["points"][:, 0].mean() - 3) < 0.1 and (two["videos"], two["agree"]) == (2, 1), two
assert bl.climb_line([], [bl.runs(walked, SIZE)], SIZE) is None and bl.climb_line([[]], [], SIZE) is None

# past where videos agree, one's run is kept while it carries on the same way (seen for longer: on up the climb)
longer = bl.climb_line([bl.runs([track([[1, 0.5, 0], [1, 3.6, 0]], each=16)], SIZE), bl.runs([track([[1, 0.5, 0], [1, 2.0, 0]], each=10)], SIZE)], [], SIZE)
assert longer["points"][:, 1].max() > 3.3, "the top only one video shows is kept"

# a short end doubling back is cut off; a line that's a real there-and-back isn't
hook = np.array([[0, y, 0] for y in np.linspace(0, 3, 31)] + [[0.05, y, 0] for y in np.linspace(2.9, 2.7, 3)])
assert len(bl._unhooked(hook)) == 31 and np.allclose(bl._unhooked(hook[::-1])[0], [0, 3, 0])
vee = np.array([[x, abs(x), 0] for x in np.linspace(-1, 1, 41)])
assert len(bl._unhooked(vee)) == 41

# ---- on the mesh: a wall at z = 0 with a bulge, points off it put back on it, with its normal
n = 81
gx, gy = np.meshgrid(np.linspace(0, 4, n), np.linspace(0, 4, n), indexing="ij")
V = np.stack([gx.ravel(), gy.ravel(), 0.3 * np.exp(-((gx.ravel() - 3) ** 2 + (gy.ravel() - 3) ** 2) / 0.5)], axis=1)
idx = np.arange(n * n).reshape(n, n)
F = np.concatenate([np.stack([idx[:-1, :-1].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()], axis=1),
                    np.stack([idx[:-1, :-1].ravel(), idx[1:, 1:].ravel(), idx[:-1, 1:].ravel()], axis=1)])
size = beta.rock_size(V)
assert abs(size - 3.6) < 0.05, size
surface = bl.Surface(V, F, bl.PATCH * size)
for _ in range(40):     # the grid finds what going through every vertex does
    p = np.array([rng.uniform(-1, 5), rng.uniform(-1, 5), rng.uniform(-2, 2)])
    brute = np.linalg.norm(V - p, axis=1)
    assert np.isclose(np.linalg.norm(V[surface.nearest(p, 0.05)] - p), brute.min(), rtol=1e-5)
    h = rng.uniform(0.05, 0.6)
    inside = surface.within(p, h)
    assert inside is None or set(inside.tolist()) == set(np.flatnonzero((np.abs(V - p) <= h).all(axis=1)).tolist())
P, N = surface.drape(A + [0, 0, 0.15], size)
assert np.abs(P[:, 2]).max() < 0.01 and np.allclose(P[:, :2], A[:, :2], atol=0.01), "straight back onto the wall"
assert np.allclose(np.abs(N[:, 2]), 1, atol=0.01) and np.allclose(np.linalg.norm(N, axis=1), 1)
bulge, bn = surface.drape(np.array([[3.0, 3.0, 0.5], [2.7, 3.0, 0.5]]), size)
assert 0.25 < bulge[0][2] < 0.31 and abs(bn[1][0]) > 0.15, "onto the bulge, whose side leans"

# ---- where a line's name goes: along its middle, away from the other lines
line = np.array([[1, y, 0] for y in np.linspace(0, 4, 41)])
assert bl.label_at(line, []) == 22
near_top = np.array([[1.1, y, 0] for y in np.linspace(2.5, 4, 16)])
at = bl.label_at(line, [near_top])
assert 12 <= at <= 16, f"by its lower part, the other line being by its upper: {at}"

# ---- holds by the lines: those further than a reach from their climb's line are off, and the rest's roles given again
held = lambda limb, t0, t1: {"limb": limb, "t0": t0, "t1": t1}  # noqa: E731
found_holds = [
    {"id": 0, "role": "start", "p": [3.4, 0.4, 0], "contacts": [held("lh", 0, 1), held("rh", 0, 1)]},     # a touch of the rock, walking in
    {"id": 1, "role": "hand", "p": [0.8, 0.7, 0], "contacts": [held("lh", 5, 8)]},
    {"id": 2, "role": "hand", "p": [1.3, 0.9, 0], "contacts": [held("rh", 5, 9)]},
    {"id": 3, "role": "hand", "p": [0.9, 2.0, 0], "contacts": [held("lh", 9, 14), held("rf", 16, 18)]},
    {"id": 4, "role": "hand", "p": [1.2, 3.3, 0], "contacts": [held("rh", 14, 18)]},
    {"id": 5, "role": "hand", "p": [1.0, 3.5, 0], "contacts": [held("lh", 15, 18)]},
    {"id": 6, "role": "foot", "p": [1.1, 0.3, 0], "contacts": [held("lf", 5, 9)]},
    {"id": 7, "role": "finish", "p": [3.5, 3.0, 0], "contacts": [held("lh", 30, 31), held("rh", 30, 31)]},  # walking off the top
    {"id": 8, "role": "foot", "p": [3.0, 0.2, 0], "contacts": [held("rf", 0, 2)]},
    {"id": 9, "role": "foot", "p": [0.9, 1.2, 0], "contacts": [held("rf", 9, 13)]},
    {"id": 10, "role": "foot", "p": [1.1, 2.4, 0], "contacts": [held("lf", 14, 18)]}]
up_one = np.array([[1.0, y, 0.0] for y in np.linspace(0.5, 3.5, 31)])
with_holds = [{"url": "a1", "holds": found_holds}, {"url": "a2", "holds": [dict(found_holds[0], id=0), dict(found_holds[3], id=1, role="finish")]},
              {"url": "none", "holds": []}, {"url": "both", "holds": [dict(found_holds[0], id=0), dict(found_holds[8], id=1)]}]
by = {"A": ["a1", "a2", "none", "both"], "B": ["both"], "C": []}
agreed = {"A": {"p": up_one, "near": ["a1", "none"], "agree": 2}}
got = bl.holds_by_lines(with_holds, by, agreed, 1.0)
assert got["a1"] == {"off": [0, 7, 8], "roles": {"1": "start", "2": "start", "4": "finish", "5": "finish"}}, got["a1"]
assert got["a2"] == {"off": [0], "roles": {"1": "start"}}, "a line two videos agree on speaks for the climb's others too"
assert got["both"] == {"off": [0, 1], "roles": {}} and "none" not in got
alone = bl.holds_by_lines(with_holds, by, {"A": {"p": up_one, "near": ["a1"], "agree": 1}}, 1.0)
assert set(alone) == {"a1"}, "a line from one video says nothing of the others' holds"
also_b = bl.holds_by_lines(with_holds, by, {**agreed, "B": {"p": up_one + [2.4, 0, 0], "near": ["both"], "agree": 1}}, 1.0)
assert "both" not in also_b, "a video of two climbs keeps a hold near either's line (and its roles stay)"
assert bl.holds_by_lines(with_holds, by, {}, 1.0) == {} and bl.holds_by_lines(with_holds, by, agreed, 9.0) == {}
# a line checks itself: a third of its own videos' holds beyond a reach of it, and it says nothing of holds
assert abs(bl.astray(with_holds, agreed, 1.0)["A"] - 3 / 11) < 1e-9 and bl.astray(with_holds, {"A": {**agreed["A"], "near": ["none"]}}, 1.0) == {"A": 0.0}
short = {"A": {"p": up_one[:8], "near": ["a1"], "agree": 2}}        # a line that stops at 1.2 of the 3.5 its climb goes up
assert bl.astray(with_holds, short, 1.0)["A"] > bl.ASTRAY and bl.holds_by_lines(with_holds, by, short, 1.0) == {}, "its top's holds aren't off"
assert abs(bl._from_line([1.5, 2.0, 0], up_one) - 0.5) < 1e-9 and abs(bl._from_line([1.0, 4.5, 0], up_one) - 1.0) < 1e-9
# a reach: in metres where the scale is known, within bounds of the rock's size; else a share of it
assert bl.reach(4.0) == bl.REACH * 4.0 and abs(bl.reach(4.0, 1.0) - bl.REACH_M) < 1e-9 and abs(bl.reach(4.0, 2.5) - bl.REACH_M / 2.5) < 1e-9
assert bl.reach(4.0, 0.1) == bl.REACH_BOUNDS[1] * 4.0 and bl.reach(4.0, 50.0) == bl.REACH_BOUNDS[0] * 4.0 and bl.reach(4.0, 0) == bl.reach(4.0)
# a record as shown: off marked, roles given, what was found kept; nothing changed without lines for it
rec = {"videos": [{"url": "a1", "holds": json.loads(json.dumps(found_holds))}, {"url": "zz", "holds": [dict(found_holds[1])]}]}
seen = bl.shown(rec, {"holds": got})
h = {x["id"]: x for x in seen["videos"][0]["holds"]}
assert seen is rec and [i for i, x in h.items() if x.get("off")] == [0, 7, 8] and h[0]["role"] == "start", "an off hold keeps the role found"
assert (h[1]["role"], h[1]["found_role"], h[5]["role"], h[3]["role"]) == ("start", "hand", "finish", "hand") and "found_role" not in h[3]
assert seen["videos"][1]["holds"] == [found_holds[1]] and bl.shown(None, None) is None and bl.shown({"videos": [{"url": "a1"}]}, None) == {"videos": [{"url": "a1"}]}

# ---- a mesh's: made from its beta and which videos are of which climb, kept, and known to be of them still
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    with open(d / "mesh.ply", "w") as f:
        f.write(f"ply\nformat ascii 1.0\nelement vertex {len(V)}\nproperty float x\nproperty float y\nproperty float z\n"
                f"element face {len(F)}\nproperty list uchar int vertex_indices\nend_header\n")
        f.write("\n".join(" ".join(f"{x:.5f}" for x in v) for v in V) + "\n")
        f.write("\n".join(f"3 {a} {b} {c}" for a, b, c in F) + "\n")
    record = {"status": "done", "videos": videos}
    beta.save(d, record)
    kept = bl.make(d, of)
    assert bl.load(d) == kept and set(kept["lines"]) == {"A", "B"}
    one = kept["lines"]["A"]
    assert len(one["p"]) == len(one["n"]) > 20 and 0 < one["at"] < len(one["p"]) - 1 and (one["videos"], one["agree"]) == (5, 4)
    assert max(abs(p[2]) for p in one["p"]) < 0.01 and len(json.dumps(kept)) < 20000
    k = bl.key(bl.tracks(record), of)
    assert kept["key"] == k
    assert bl.key(bl.tracks({**record, "status": "queued", "progress": "video 2 of 3"}), of) == k, "the same tracks, written again"
    assert bl.key(bl.tracks(record), {**of, "D": ["off"]}) != k, "a video now of another climb"
    assert bl.key(bl.tracks({"videos": videos[:-2]}), of) != k, "a video's track gone"
    assert bl.key(bl.tracks({"videos": [dict(videos[0], holds=found_holds), *videos[1:]]}), of) != k, "its holds found again"
    assert bl.key(bl.tracks(record), of, "", 1.5) != k and bl.key(bl.tracks(record), of, "", 1.5) == bl.key(bl.tracks(record), of, "", 1.50001), "its scale"
    # its holds by its lines, by its scale: the wall's size is 3.6, so a reach is 1.08 of its units unsized, 0.7 at 2.5 m a unit
    some = [{"id": 0, "role": "finish", "p": [0.8, 0.7, 0], "contacts": [held("lh", 5, 8)]},
            {"id": 1, "role": "start", "p": [2.0, 0.6, 0], "contacts": [held("lh", 0, 1), held("rh", 0, 1)]},
            {"id": 2, "role": "foot", "p": [1.1, 0.6, 0], "contacts": [held("lf", 5, 8)]}, {"id": 3, "role": "foot", "p": [0.9, 1.5, 0], "contacts": [held("rf", 5, 8)]}]
    beta.save(d, {"status": "done", "videos": [dict(videos[0], holds=some), *videos[1:]]})
    assert bl.make(d, of)["holds"] == {}, "1.0 from the line: within a reach of 1.08, and its roles as found"
    assert bl.make(d, of, metres_per_unit=2.5)["holds"] == {"a1": {"off": [1], "roles": {"0": "start"}}}, "and beyond one of 0.7"
    beta.save(d, record)
    assert bl.key(bl.tracks(record), {c: list(reversed(u)) for c, u in reversed(list(of.items()))}) == k, "in whatever order"
    os.remove(d / bl.FILE)
    none = bl.make(d, {"A": []})
    assert none["lines"] == {} and bl.load(d) == none, "no line is kept as none, not as not yet made"
    beta.save(d, {"status": "done", "videos": [{"url": "x"}]})
    os.remove(d / "mesh.ply")
    assert bl.make(d, {"A": ["x"]})["lines"] == {}, "with no tracks, the mesh isn't read"
    beta.save(d, record)
    assert bl.make(d, of) == {"key": k, "lines": {}, "holds": {}}, "no mesh to read: none, kept as made"

# ---- a piece its cleanup hides: a fragment in front of the wall's foot, which the start of a track lands on. Its
# points there are dropped, and the line laid on the wall alone
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    m = 11
    sx, sy = np.meshgrid(np.linspace(1.5, 2.5, m), np.linspace(0, 1.2, m), indexing="ij")
    SV = np.stack([sx.ravel(), sy.ravel(), np.full(m * m, 0.3)], axis=1)
    si = np.arange(m * m).reshape(m, m) + len(V)
    SF = np.concatenate([np.stack([si[:-1, :-1].ravel(), si[1:, :-1].ravel(), si[1:, 1:].ravel()], axis=1),
                         np.stack([si[:-1, :-1].ravel(), si[1:, 1:].ravel(), si[:-1, 1:].ravel()], axis=1)])
    V2, F2 = np.vstack([V, SV]), np.vstack([F, SF])
    with open(d / "mesh.ply", "w") as f:
        f.write(f"ply\nformat ascii 1.0\nelement vertex {len(V2)}\nproperty float x\nproperty float y\nproperty float z\n"
                f"element face {len(F2)}\nproperty list uchar int vertex_indices\nend_header\n")
        f.write("\n".join(" ".join(f"{x:.5f}" for x in v) for v in V2) + "\n")
        f.write("\n".join(f"3 {a} {b} {c}" for a, b, c in F2) + "\n")
    climb = [[0.5 * k, 2.0, 0.2 + 0.1 * k, 0.3 if 0.2 + 0.1 * k <= 1.2 else 0.0] for k in range(33)]     # its foot on the fragment
    beta.save(d, {"status": "done", "videos": [{"url": "s1", "centre": [climb]}]})
    whole = bl.make(d, {"S": ["s1"]})["lines"]["S"]
    assert max(p[2] for p in whole["p"]) > 0.2 and min(p[1] for p in whole["p"]) < 0.4, "nothing hidden: the fragment is rock like any"
    assert bl.shown_faces(d, len(F2)) is None and bl.shown_stamp(d) == ""
    (d / "cleanup_faces.bin").write_bytes(np.r_[np.zeros(len(F), "<u2"), np.ones(len(SF), "<u2")].tobytes())
    (d / "cleanup.json").write_text(json.dumps({"label_bytes": 2, "faces": len(F2), "pieces": [{"id": 0}, {"id": 1}], "removed": [1]}))
    shown = bl.shown_faces(d, len(F2))
    assert shown.sum() == len(F) and not shown[len(F):].any() and bl.shown_faces(d, len(F2) - 1) is None
    assert bl.key("t", {}, bl.shown_stamp(d)) != bl.key("t", {}, ""), "its cleanup changed: its lines are made again"
    hidden = bl.make(d, {"S": ["s1"]})["lines"]["S"]
    wall_z = lambda p: 0.3 * np.exp(-((p[0] - 3) ** 2 + (p[1] - 3) ** 2) / 0.5)  # noqa: E731
    assert max(abs(p[2] - wall_z(p)) for p in hidden["p"]) < 0.01, "on the wall alone"
    assert 1.1 < min(p[1] for p in hidden["p"]) < 1.5 and max(p[1] for p in hidden["p"]) > 3.2, "from where its track leaves the fragment"
    surface = bl.Surface(V2, F2[shown], bl.PATCH * size)
    assert len(surface.V) == len(V) and surface.near([[2, 0.5, 0.05], [2, 0.5, 0.3], [9, 9, 9]]).tolist() == [True, False, False]
    assert [len(p) for p in bl.on_rock([climb], surface)] == [22] and bl.on_rock([[], climb[:1]], surface) == []
    (d / "cleanup.json").write_text(json.dumps({"deleted": True}))
    assert bl.shown_faces(d, len(F2)) is None, "a cleanup deleted hides nothing"
print("ALL BETALINES TESTS PASSED")
