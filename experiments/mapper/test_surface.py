"""surface.py's laying of one mesh over another, on made-up rock: the scale and shift found without the cameras'
positions, from a start a quarter off, both ways matched so a mesh isn't shrunk into the other.

    python3 test_surface.py        (numpy and scipy)
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import surface  # noqa: E402


def rock(n: int, extent: float, seed: int, wobble: float = 0.0):
    """Bumpy ground over [-extent, extent]^2 with a boulder on it, as (vertices, triangles): the same whatever the grid."""
    xs = np.linspace(-extent, extent, n)
    X, Y = np.meshgrid(xs, xs)
    Z = 0.15 * np.sin(1.3 * X) * np.cos(0.9 * Y) + 0.1 * np.sin(3.1 * X + 1.0) + 0.08 * np.cos(2.7 * Y - 0.5) + 0.05 * np.sin(5.0 * X * Y / 3)
    Z += 1.6 * np.exp(-((X - 0.2) ** 2 + (Y + 0.1) ** 2) / 1.2) + 0.7 * np.exp(-((X + 1.1) ** 2 + (Y - 0.9) ** 2) / 0.3)      # a boulder, and a smaller one by it
    Z += np.random.default_rng(seed).normal(size=Z.shape) * wobble
    V = np.stack([X, Z, Y], axis=-1).reshape(-1, 3)
    i = np.arange(n * n).reshape(n, n)
    F = np.concatenate([np.stack([i[:-1, :-1], i[1:, :-1], i[:-1, 1:]], -1).reshape(-1, 3), np.stack([i[1:, :-1], i[1:, 1:], i[:-1, 1:]], -1).reshape(-1, 3)])
    return V, F


def turn(axis, deg):
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    t = np.radians(deg)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + np.sin(t) * K + (1 - np.cos(t)) * K @ K


rng = np.random.default_rng(0)
Vb, Fb = rock(140, 3.0, 1, wobble=0.004)                # b: the rock, and 3 units round it
Va_in_b, Fa = rock(170, 4.0, 2, wobble=0.004)           # a: the same rock on another grid, with more round it
# a's own frame: b's turned, 0.6 of its size, shifted (so a point of a goes to b by s, R, t)
s_true, R_true, t_true = 1 / 0.6, turn([0.3, 1, 0.2], 50), np.array([4.0, -1.0, 2.5])
Va = (Va_in_b - t_true) @ R_true / s_true
size, middle = 3.0, np.zeros(3)
A, B = surface.spread(Va, Fa, 20_000, rng), surface.spread(Vb, Fb, 20_000, rng)
mid_a, spread_a = np.median(A, axis=0), float(np.median(np.linalg.norm(A - np.median(A, axis=0), axis=1)))
mid_b, spread_b = np.median(B, axis=0), float(np.median(np.linalg.norm(B - np.median(B, axis=0), axis=1)))
floor = float(np.median(surface.cKDTree(B).query(surface.spread(Vb, Fb, 20_000, rng))[0]))

# the cameras' fit a quarter too large and off to one side, as when positions trade against lenses
cameras = (s_true * 1.25, t_true + np.array([0.4, 0.1, -0.3]))
ways = surface.starts(R_true, cameras, mid_a, spread_a, mid_b, spread_b)
assert [w[0] for w in ways][:6] == ["points", "points x0.8", "points x0.9", "points x1.1", "points x1.25", "cameras"] and len(ways) == 10
laid = surface.lay(A, B, ways, size, middle)
assert abs(laid["s"] / s_true - 1) < 0.01, (laid["name"], laid["s"] / s_true, laid["tried"])
assert laid["score"] < 1.5 * floor and laid["score"] <= laid["first"], (laid["score"], floor)
assert not laid["flags"], laid["flags"]
print(f"from a start a quarter off: scale {laid['s'] / s_true:.4f} of the truth, {laid['score'] / floor:.2f} of the floor, by '{laid['name']}'")

# from the spoiled camera fit alone, matching settles somewhere near but not there, which is why several starts are
# tried; and matched one way only, free to scale (as it was), it ends further off still
one = surface.fit(A, B, (cameras[0], R_true, cameras[1]), size, middle)
assert one["score"] <= one["first"] and laid["score"] <= one["score"]
s, At = cameras[0], A @ (cameras[0] * R_true).T + cameras[1]
tree = surface.cKDTree(B)
for _ in range(20):
    d, i = tree.query(At)
    near = d <= np.percentile(d, 70)
    s2, R2, t2 = surface.similarity(At[near], B[i[near]])
    At = At @ (s2 * R2).T + t2
    s *= s2
old = float(np.median(tree.query(At)[0]))
print(f"the cameras' start alone: both ways, scale {one['s'] / s_true:.3f} of the truth at {one['score'] / floor:.1f} of the floor; "
      f"one way and free to scale, {s / s_true:.3f} at {old / floor:.1f}")
assert abs(laid["s"] / s_true - 1) < abs(s / s_true - 1)

# a fit never ends worse than it began: from the truth it stays there
exact = surface.fit(A, B, (s_true, R_true, t_true), size, middle)
assert exact["score"] <= exact["first"] and abs(exact["s"] / s_true - 1) < 0.005
# and its scale stays within SCALE of its start's, however the matches pull
wild = surface.fit(A, B, (s_true * 5, R_true, t_true), size, middle)
assert wild["s"] >= s_true * 5 / surface.SCALE - 1e-9
# a run's folder: sift391 unless it names its feature set
assert surface.where("p", "run/post").parts[-3:] == ("sift391", "run", "post") and surface.where("p", "siftgpu391:run/post").parts[-3:] == ("siftgpu391", "run", "post")
print("ALL SURFACE TESTS PASSED")
