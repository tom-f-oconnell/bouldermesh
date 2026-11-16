"""Whether two models of a boulder give the same rock: their meshes laid over each other, and how far apart they are.

    surface.py <project> <run a>/<post folder> <run b>/<post folder> [...]      a Markdown row per pair

A run is of the feature set sift391 unless it says otherwise, as `<feature set>:<run>/<post folder>`
(`siftgpu391:global_tied-s0+complete@prod/post-complete+as23@prod`).

Cameras can differ between two models that see the same (a lens per photo lets one turn, shift or stand back and see
what the other sees: consensus.py), so the test of a model is the surface it gives. Each mesh is a post folder's
textured GLB (its vertices are in the model's own frame). Mesh a is laid over mesh b, the surfaces are matched (fit()),
and the result is the distance from points spread evenly over each surface to the other surface, as a share of the
scene's size (the median distance of b's cameras from their middle): median, 90th and 99th percentile, and the share
of points further than 1% and 2%.

How a is laid over b. The turn is the one that brings the shared cameras' rays onto their twins (consensus.overlay):
that holds whatever the lenses did. Scale and shift can't be had from where the cameras stand, as a camera that
stands back with a longer lens sees the same, so two models' camera positions can differ by a quarter while their
rocks agree; so they're tried several ways (starts()): from the two models' own 3D points (their middles, and how far
they spread from them), from the cameras' positions as before, and each of those larger and smaller about the rock's
middle (first with a few points each, then the best two with all). From each start the surfaces are matched both ways at once (each point of a to the nearest of b and each of b
to the nearest of a: matched one way only, a fit that may scale shrinks a mesh into the other), fitted on the closer
half of what's within one scene's size of the rock's middle, with the scale held within SCALE of the start's; the best fit seen is kept, by the 30th percentile of the
distances near the rock both ways (the part two meshes share reads near nothing when they're laid right, whatever
else only one of them covers), so a fit never ends worse than it began. The row says which start won and the distance before and after
its fit, and "starts disagree" if another start ended as good somewhere else (within a tenth of its distance, at a
scale over 2% away): that row isn't to be trusted without a look. What each start came to goes to stderr. Twice: over everything, and only within one scene's size of the rock's middle (the
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
ROUNDS, KEEP, PATIENCE = 60, 0.5, 6 # of matching and fitting, at most; the closer share of matches fitted on; rounds with nothing gained before it stops
SCORE = 30                          # a fit is scored by this percentile of the distances near the rock, both ways (see fit())
SCALE = 2.0                         # a fit's scale stays within this of its start's, either way
FACTORS = (1.0, 0.8, 0.9, 1.1, 1.25)    # each start, also this much smaller and larger about the rock's middle
QUICK, QUICK_ROUNDS, FINAL = 12_000, 30, 2     # points of each surface a start is first tried with, and rounds; how many of the best go on to all
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


def cloud(model: Path) -> tuple[np.ndarray, float]:
    """A model's 3D points: their middle (the median, where the rock is) and how far they typically are from it."""
    xyz = []
    with open(model / "points3D.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(8)
            xyz.append(struct.unpack("<3d", f.read(24)))
            f.read(3 + 8)
            f.read(8 * struct.unpack("<Q", f.read(8))[0])
    xyz = np.array(xyz)
    mid = np.median(xyz, axis=0)
    return mid, float(np.median(np.linalg.norm(xyz - mid, axis=1)))


def starts(R: np.ndarray, cameras: tuple[float, np.ndarray] | None, mid_a: np.ndarray, spread_a: float, mid_b: np.ndarray,
           spread_b: float) -> list[tuple[str, float, np.ndarray, np.ndarray]]:
    """Ways to lay a over b before the surfaces are matched, each (name, scale, R, shift), all with the turn R: from the
    models' own points (a's middle onto b's, by the ratio of their spreads), from the cameras' fit (its scale and
    shift) if given, and each larger and smaller by FACTORS about the rock's middle."""
    out = []
    base = [("points", spread_b / spread_a, mid_b)] + ([("cameras", cameras[0], cameras[0] * R @ mid_a + cameras[1])] if cameras else [])
    for name, s, at in base:        # `at`: where a's middle lands
        for k in FACTORS:
            out.append((name if k == 1.0 else f"{name} x{k:g}", s * k, R, at - s * k * R @ mid_a))
    return out


def fit(A: np.ndarray, B: np.ndarray, start: tuple[float, np.ndarray, np.ndarray], size: float, rock: np.ndarray,
        tree_b: cKDTree | None = None, rounds: int = 0) -> dict:
    """Match surface samples A (in a's frame) to B (in b's) from one start, both ways at once, and keep the best fit
    seen.

    Args:
        start: (scale, R, shift) taking a's frame into b's.
        size, rock: the scene's size and the rock's middle in b's frame: the score is the SCORE-th percentile of the
            distances both ways among points within one size of the rock.
        tree_b: a tree of a denser sample of b's surface to measure A against, if there's one; else B's own.

    Returns:
        {s, R, t, first, score, rounds}: the fit, the score at the start and at the fit, and the rounds it took.
    """
    s0, R0, t0 = start
    rounds = rounds or ROUNDS
    s, R, t = s0, R0, t0
    tree_b = tree_b or cKDTree(B)
    near_b = np.linalg.norm(B - rock, axis=1) < size
    best = None
    target = np.asarray(tree_b.data)
    for n in range(rounds + 1):
        At = A @ (s * R).T + t
        # nothing is looked for further off than the scene's size: a point that far from the other surface counts as
        # that far, and isn't fitted on (and looking further is what makes a bad start slow)
        d_ab, i = tree_b.query(At, distance_upper_bound=size)
        d_ba, j = cKDTree(At).query(B, distance_upper_bound=size)
        found_a, found_b = np.isfinite(d_ab), np.isfinite(d_ba)
        d_ab, d_ba = np.where(found_a, d_ab, size), np.where(found_b, d_ba, size)
        near_a = np.linalg.norm(At - rock, axis=1) < size
        # a low percentile, not the median: where the two surfaces are the same rock laid right, the part they share
        # reads next to nothing, however much of what's round it only one of them has; a median would rather shrink a
        # mesh towards the other than leave its own part unmatched
        score = (float(np.percentile(d_ab[near_a], SCORE)) if near_a.any() else np.inf) / 2 + (float(np.percentile(d_ba[near_b], SCORE)) if near_b.any() else np.inf) / 2
        if best is None:
            first = score
        if best is None or score < best["score"]:
            best = {"s": s, "R": R, "t": t, "score": score, "rounds": n}
        elif n - best["rounds"] >= PATIENCE:       # no better for a while: it's found what it will
            break
        if n == rounds:
            break
        # fitted on what's near the rock only, and the closer of that: two meshes cover different amounts of what's round
        # it, and the part one has alone, matched to the other's edge, would pull the fit smaller
        if near_a.sum() < 100 or near_b.sum() < 100:
            break
        ka = near_a & found_a & (d_ab <= np.percentile(d_ab[near_a], KEEP * 100))
        kb = near_b & found_b & (d_ba <= np.percentile(d_ba[near_b], KEEP * 100))
        if ka.sum() + kb.sum() < 100:
            break
        X = np.concatenate([At[ka], At[j[kb]]])
        Y = np.concatenate([target[i[ka]], B[kb]])
        s2, R2, t2 = similarity(X, Y)
        s2 = float(np.clip(s2, s0 / SCALE / s, s0 * SCALE / s))
        t2 = Y.mean(0) - s2 * R2 @ X.mean(0)
        s, R, t = s2 * s, R2 @ R, s2 * R2 @ t + t2
    return {**best, "first": first}


def lay(A: np.ndarray, B: np.ndarray, ways: list[tuple[str, float, np.ndarray, np.ndarray]], size: float, rock: np.ndarray,
        tree_b: cKDTree | None = None) -> dict:
    """fit() from each of starts()' ways, and the best of them: {name, s, R, t, first, score, flags, tried}. flags:
    "starts disagree" if another start ended within a tenth of its score at a scale over 2% away."""
    tree_b = tree_b or cKDTree(B)
    # every start with a few points first; then the best of those, from where they got to, with all of them
    said = lambda f, how: print(f"  {how} {f['name']}: {f['first'] / size:.2%} to {f['score'] / size:.2%} in {f['rounds']} rounds, scale {f['s']:.4g}",  # noqa: E731
                                file=sys.stderr, flush=True)
    quick = []
    few = cKDTree(B)        # measured against B's own sample for now: coarser, and quick
    for name, s, R, t in ways:
        quick.append({"name": name, **fit(A[:QUICK], B[:QUICK], (s, R, t), size, rock, few, QUICK_ROUNDS)})
        said(quick[-1], "start")
    quick.sort(key=lambda f: f["score"])
    tried = []
    for q in quick[:FINAL]:
        f = fit(A, B, (q["s"], q["R"], q["t"]), size, rock, tree_b)
        tried.append({**f, "name": q["name"], "first": q["first"]})
        said(tried[-1], "then with every point,")
    tried += [q for q in quick[FINAL:]]
    best = min(tried[:FINAL], key=lambda f: f["score"])
    flags = []
    if any(f is not best and f["score"] <= best["score"] * 1.1 and abs(np.log(f["s"] / best["s"])) > 0.02 for f in tried[:FINAL]):
        flags.append("starts disagree")
    return {**best, "flags": flags, "tried": [(f["name"], f["first"] / size, f["score"] / size, f["s"]) for f in tried]}


def where(P: str, spec: str) -> Path:
    """A run's post folder: "<run>/<post folder>" of sift391, or "<feature set>:<run>/<post folder>"."""
    feat, _, rest = spec.rpartition(":")
    return bench.R / P / (feat or "sift391") / rest


def compare(P: str, a: str, b: str, rng) -> str:
    """The Markdown row of meshes a against b (each where()'s)."""
    model = lambda x: where(P, x).parent / "rest" / "rest2"      # noqa: E731
    o = consensus.overlay(consensus.load(model(a)), consensus.load(model(b)))
    size = o["size"]
    (mid_a, spread_a), (rock, spread_b) = cloud(model(a)), cloud(model(b))
    Va, Fa = mesh(where(P, a) / GLB)
    Vb, Fb = mesh(where(P, b) / GLB)
    tree = cKDTree(spread(Vb, Fb, TARGET, rng))
    laid = lay(spread(Va, Fa, ICP, rng), spread(Vb, Fb, ICP, rng), starts(o["fit"][1], (o["fit"][0], o["fit"][2]), mid_a, spread_a, rock, spread_b),
               size, rock, tree)
    s, R, t = laid["s"], laid["R"], laid["t"]
    A = spread(Va, Fa, SAMPLES, rng) @ (s * R).T + t
    ab = tree.query(A)[0]
    B2 = spread(Vb, Fb, SAMPLES, rng)
    ba = cKDTree(spread(Va, Fa, TARGET, rng) @ (s * R).T + t).query(B2)[0]
    floor = tree.query(B2)[0]
    by_rock = lambda pts, d: d[np.linalg.norm(pts - rock, axis=1) < size]      # noqa: E731
    start = f"{laid['name']}: {laid['first'] / size:.2%} to {laid['score'] / size:.2%}, scale {laid['s'] / o['fit'][0]:.3f} of the cameras'"
    if laid["flags"]:
        start += " (**" + ", ".join(laid["flags"]) + "**)"
    print(f"  {a} on {b}: laid by {start}", file=sys.stderr, flush=True)
    return (f"| {P[:4]} | {a} | {b} | {len(Fa):,} / {len(Fb):,} | {start} | {numbers(ab, size)} | {numbers(ba, size)} "
            f"| {numbers(by_rock(A, ab), size)} | {numbers(by_rock(B2, ba), size)} | {np.median(floor) / size:.2%} |")


if __name__ == "__main__":
    P, runs = sys.argv[1], sys.argv[2:]
    print("| Boulder | A | B | Faces A / B | Start that won: near the rock before to after its fit | A to B: median / 90th / 99th | beyond 1% / 2% "
          "| B to A: median / 90th / 99th | beyond 1% / 2% | Near the rock, A to B | beyond 1% / 2% | Near the rock, B to A | beyond 1% / 2% | Floor |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for x, y in zip(runs[0::2], runs[1::2]):
        print(compare(P, x, y, np.random.default_rng(0)), flush=True)
