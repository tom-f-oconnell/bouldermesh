"""Whether two models of a boulder give the same rock: their meshes laid over each other, and how far apart they are.

    surface.py <project> <run a>/<post folder> <run b>/<post folder> [...]      a Markdown row per pair

Cameras can differ between two models that see the same (a lens per photo lets one turn, shift or stand back and see
what the other sees: consensus.py), so the test of a model is the surface it gives. Each mesh is a post folder's
textured GLB (its vertices are in the model's own frame). Mesh a is laid over mesh b with the fit of the two models'
cameras (consensus.overlay) and then by matching the surfaces themselves (trimmed ICP: turn, shift and scale), and
the result is the distance from points spread evenly over each surface to the other surface, as a share of the
scene's size (the median distance of b's cameras from their middle): median, 90th and 99th percentile, and the share
of points further than 1% and 2%. Twice: over everything, and only within one scene's size of the rock's middle (the
models' 3D points), since two meshes also differ in how much of the surroundings they cover. Compare a pair with the
baseline's two runs through the same OpenMVS: that is the spread a mesh has anyway. "floor" is the distance between
two sets of points spread over the same surface: what the measure reads when there is no difference at all.

Needs numpy and scipy (the system's python3 has both).
"""
import base64
import json
import struct
import sys
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
import consensus  # noqa: E402

GLB = "scene_dense_mesh_texture.glb"
SAMPLES, TARGET, ICP = 300_000, 1_500_000, 60_000      # points measured from, measured to, and matched in each ICP round
DTYPE = {5121: "u1", 5123: "<u2", 5125: "<u4", 5126: "<f4"}


def mesh(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """A GLB's (vertices, triangles), every primitive joined."""
    with open(path, "rb") as f:
        assert f.read(4) == b"glTF"
        f.read(8)
        n, _ = struct.unpack("<I4s", f.read(8))
        doc = json.loads(f.read(n))
        n, _ = struct.unpack("<I4s", f.read(8))
        blob = f.read(n)
    buffers = [base64.b64decode(b["uri"].split(",", 1)[1]) if "uri" in b else blob for b in doc["buffers"]]

    def read(index: int, width: int) -> np.ndarray:
        acc = doc["accessors"][index]
        view = doc["bufferViews"][acc["bufferView"]]
        assert "byteStride" not in view, "interleaved buffers aren't read"
        start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
        return np.frombuffer(buffers[view["buffer"]], DTYPE[acc["componentType"]], acc["count"] * width, start).reshape(-1, width)
    V, F, at = [], [], 0
    for m in doc["meshes"]:
        for p in m["primitives"]:
            v = read(p["attributes"]["POSITION"], 3).astype(np.float64)
            F.append(read(p["indices"], 1).reshape(-1, 3).astype(np.int64) + at)
            V.append(v)
            at += len(v)
    return np.concatenate(V), np.concatenate(F)


def spread(V: np.ndarray, F: np.ndarray, n: int, rng) -> np.ndarray:
    """n points spread evenly over a mesh's surface."""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    area = np.linalg.norm(np.cross(b - a, c - a), axis=1)
    t = rng.choice(len(F), n, p=area / area.sum())
    u, v = rng.random(n), rng.random(n)
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    return a[t] + u[:, None] * (b[t] - a[t]) + v[:, None] * (c[t] - a[t])


def similarity(X: np.ndarray, Y: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """The scale, rotation and shift taking points X onto points Y, least squares (Umeyama)."""
    mx, my = X.mean(0), Y.mean(0)
    U, S, Vt = np.linalg.svd((Y - my).T @ (X - mx))
    D = np.diag([1, 1, np.sign(np.linalg.det(U @ Vt))])
    R = U @ D @ Vt
    s = float((S * np.diag(D)).sum() / ((X - mx) ** 2).sum())
    return s, R, my - s * R @ mx


def numbers(d: np.ndarray, size: float) -> str:
    d = d / size
    return (f"{np.median(d):.2%} / {np.percentile(d, 90):.2%} / {np.percentile(d, 99):.2%} | "
            f"{(d > 0.01).mean():.1%} / {(d > 0.02).mean():.1%}")


def compare(P: str, a: str, b: str, rng) -> str:
    """The Markdown row of meshes a against b (each "<run>/<post folder>")."""
    post = lambda x: bench.R / P / "sift391" / x      # noqa: E731
    model = lambda x: post(x).parent / "rest" / "rest2"      # noqa: E731
    o = consensus.overlay(consensus.load(model(a)), consensus.load(model(b)))
    s, R, t = o["fit"]
    size = o["size"]
    Va, Fa = mesh(post(a) / GLB)
    Vb, Fb = mesh(post(b) / GLB)
    A, B = spread(Va, Fa, SAMPLES, rng) @ (s * R).T + t, spread(Vb, Fb, TARGET, rng)
    tree = cKDTree(B)
    first = float(np.median(tree.query(A[:ICP])[0]) / size)
    for _ in range(20):     # the surfaces themselves: match each point to the nearest, fit on the closer 70%, again
        d, i = tree.query(A[:ICP])
        near = d <= np.percentile(d, 70)
        s2, R2, t2 = similarity(A[:ICP][near], B[i[near]])
        A = A @ (s2 * R2).T + t2
        s, R, t = s2 * s, R2 @ R, s2 * R2 @ t + t2
    rock = consensus.middle(model(b))
    ab = tree.query(A)[0]
    B2 = spread(Vb, Fb, SAMPLES, rng)
    ba = cKDTree(spread(Va, Fa, TARGET, rng) @ (s * R).T + t).query(B2)[0]
    floor = tree.query(B2)[0]
    by_rock = lambda pts, d: d[np.linalg.norm(pts - rock, axis=1) < size]      # noqa: E731
    return (f"| {P[:4]} | {a} | {b} | {len(Fa):,} / {len(Fb):,} | {first:.2%} | {numbers(ab, size)} | {numbers(ba, size)} "
            f"| {numbers(by_rock(A, ab), size)} | {numbers(by_rock(B2, ba), size)} | {np.median(floor) / size:.2%} |")


if __name__ == "__main__":
    P, runs = sys.argv[1], sys.argv[2:]
    print("| Boulder | A | B | Faces A / B | Median before ICP | A to B: median / 90th / 99th | beyond 1% / 2% | B to A: median / 90th / 99th "
          "| beyond 1% / 2% | Near the rock, A to B | beyond 1% / 2% | Near the rock, B to A | beyond 1% / 2% | Floor |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for x, y in zip(runs[0::2], runs[1::2]):
        print(compare(P, x, y, np.random.default_rng(0)), flush=True)
