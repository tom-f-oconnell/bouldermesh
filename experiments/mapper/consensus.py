"""Whether a boulder's models are the same model, judged by each other and not by one of them: consensus.py

    consensus.py [project ...]        tables in Markdown for every boulder with runs, or for those named
    consensus.py trade <project> <run a> <run b>     whether two models' cameras stand apart only as their lenses differ

bench.py agree lays each pair of models over each other with every camera they share and compares the cameras'
rotations. Two things mislead there. A model with a bent part and one that is simply elsewhere look alike. And with a
lens per photo and the principal point refined (the app's settings), a camera can turn a few degrees and shift its
principal point to match, changing nothing it sees: rotations differ where the models don't.

So here a camera is its viewing rays: where five of its pixels (the middle, and halfway to each corner) look, through
its own lens. Each pair of models is laid over each other on the largest set of cameras that agree about how the two
are turned (within TOL_DEG), and the result is the share of the shared cameras whose rays then land within 1, 2 and 5
degrees, and whose positions within 1, 2 and 5% of the scene's size. A wrong model shows as one whose shares are low
against every other model, whichever method made it. Positions are the weaker sign: a camera can also stand further
from the rock with a longer lens and see the same (trade measures how much of a pair's disagreement is that). With each model's own numbers (points, track length,
reprojection error, lenses that can't be real, cameras that see next to none of its points) to see whether a wrong
one gives itself away, the spread of the phones'
gravity readings in its frame where the photos carry any (backend/orientation.py: every reading is the same "down",
so in a right model they agree), and which photos it places that the baseline's first run doesn't.

Models are a run's after the app's rest step (<BENCH>/r2/<project>/<features>/<run>/rest/rest2), and the incremental
pipeline's states (<BENCH>/incr/r/<project>/<state>/model). Needs numpy, and
PIL for the gravity readings (the system's python3 has both; the benchmark's venv has no PIL).
"""
import struct
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "backend")]
import bench  # noqa: E402

TOL_DEG = 2.0       # cameras whose rays, or whose accounts of how the two models are turned, differ by less agree
AGREE = (2.0, 0.05)     # "the same camera" in two models: rays within this many degrees, and this share of the scene's size
BASE = "base-s0@prod"
TIED = 50       # a camera that sees fewer of the model's points than this is hardly tied to it
PIXELS = [(0.5, 0.5), (0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)]      # as shares of width and height


def load(model: Path) -> dict[str, dict]:
    """A model's placed photos by name: R (world to camera), its centre, its five rays in the model's frame, how many
    of its features became points, and whether its lens can be a real one."""
    import orientation
    out = {}
    for c in orientation.read_cameras(model):
        w, x, y, z = c["q"]
        R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                      [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                      [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])
        L = c["lens"]
        f, cx, cy = L["params"][:3] if L["model"] in (0, 2, 3) else (L["params"][0], *L["params"][2:4])
        k = L["params"][3] if L["model"] in (2, 3) else 0.0       # one radial term, as the app's SIMPLE_RADIAL has
        rays = []
        for u, v in PIXELS:
            xd, yd = (u * L["width"] - cx) / f, (v * L["height"] - cy) / f
            xu, yu = xd, yd
            for _ in range(8):      # undo the distortion
                r2 = xu * xu + yu * yu
                xu, yu = xd / (1 + k * r2), yd / (1 + k * r2)
            d = np.array([xu, yu, 1.0]) * (1 if f > 0 else -1)
            rays.append(R.T @ (d / np.linalg.norm(d)))
        side = max(L["width"], L["height"])
        odd = not (0.3 * side < f < 6 * side) or np.hypot(cx - L["width"] / 2, cy - L["height"] / 2) > 0.25 * side
        out[c["name"]] = {"R": R, "c": -R.T @ np.array(c["t"]), "rays": np.array(rays), "seen": c["seen"], "odd": bool(odd),
                          "f": f / side}
    return out


def angles(M: np.ndarray, N: np.ndarray) -> np.ndarray:
    """Degrees between each rotation of M (k,3,3) and each of N (j,3,3)."""
    tr = np.einsum("kab,jab->kj", M, N)
    return np.degrees(np.arccos(np.clip((tr - 1) / 2, -1, 1)))


def proper(U: np.ndarray, Vt: np.ndarray) -> np.ndarray:
    return U @ np.diag([1, 1, np.sign(np.linalg.det(U @ Vt))]) @ Vt


def overlay(a: dict, b: dict) -> dict | None:
    """Model a laid over model b on the largest set of shared cameras that agree how the models are turned.

    Returns:
        The shared cameras' names, how far each one's rays are from its twin's (degrees, the mean over its five) and
        its position (as a share of the scene's size: the median distance of b's shared cameras from their middle),
        that size, and the fit (scale, rotation, shift) taking a point of a's frame into b's. None with fewer than
        3 shared.
    """
    names = sorted(set(a) & set(b))
    if len(names) < 3:
        return None
    M = np.array([b[n]["R"].T @ a[n]["R"] for n in names])      # each camera's own account of the turn from a to b
    ra, rb = np.array([a[n]["rays"] for n in names]), np.array([b[n]["rays"] for n in names])
    ca, cb = np.array([a[n]["c"] for n in names]), np.array([b[n]["c"] for n in names])
    inl = angles(M, M) < TOL_DEG
    fit = inl[inl.sum(1).argmax()]
    Rs = proper(*np.linalg.svd(M[fit].sum(0))[::2])
    off = lambda: np.degrees(np.arccos(np.clip(((ra @ Rs.T) * rb).sum(2), -1, 1))).mean(1)      # noqa: E731
    for _ in range(3):      # then the turn that brings the rays of those it fits onto their twins
        ok = off() < TOL_DEG
        if ok.sum() < 3:
            break
        fit = ok
        Rs = proper(*np.linalg.svd(np.einsum("nka,nkb->ab", rb[fit], ra[fit]))[::2])
    deg = off()
    # the scene's size: how far b's shared cameras typically stand from their middle (medians: a few cameras placed
    size = float(np.median(np.linalg.norm(cb - np.median(cb, axis=0), axis=1))) or 1.0      # far off don't stretch it)
    for again in (True, False):     # scale and shift from where those stand; once more without the farthest fifth
        X, Y = ca[fit] @ Rs.T, cb[fit]
        mx, my = X.mean(0), Y.mean(0)
        s = ((Y - my) * (X - mx)).sum() / max(((X - mx) ** 2).sum(), 1e-12)
        pos = np.linalg.norm(s * ca @ Rs.T + (my - s * mx) - cb, axis=1) / size
        keep = fit & (pos <= np.percentile(pos[fit], 80))
        if not again or keep.sum() < 3:
            break
        fit = keep
    return {"names": names, "deg": deg, "pos": pos, "fit": (float(s), Rs, my - s * mx), "size": size}


def middle(model: Path) -> np.ndarray:
    """The middle (median) of a model's 3D points: where the rock is."""
    xyz = []
    with open(model / "points3D.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(8)
            xyz.append(struct.unpack("<3d", f.read(24)))
            f.read(3 + 8)
            f.read(8 * struct.unpack("<Q", f.read(8))[0])
    return np.median(np.array(xyz), axis=0)


def trade(ma: Path, mb: Path) -> None:
    """Whether two models' cameras stand in different places only because their lenses differ to match: with a lens
    per photo, a camera further from the rock with a longer lens sees the same. Prints, for the cameras both place
    and for those among them that stand more than AGREE[1] of the scene's size apart: how much of the gap between
    the two positions lies along the line of sight, and how the ratio of the two focal lengths follows the ratio
    of the two distances to the rock (the slope and correlation of their logarithms: 1 and 1 if it is all that)."""
    a, b = load(ma), load(mb)
    o = overlay(a, b)
    s, Rs, t = o["fit"]
    ca, cb = middle(ma), middle(mb)
    rows = []
    for n, pos, deg in zip(o["names"], o["pos"], o["deg"]):
        pa, pb = s * Rs @ a[n]["c"] + t, b[n]["c"]
        sight = b[n]["R"].T @ np.array([0.0, 0.0, 1.0])
        gap = pa - pb
        along = float((gap @ sight) ** 2 / max(gap @ gap, 1e-18))
        da, db = np.linalg.norm(pa - (s * Rs @ ca + t)), np.linalg.norm(pb - cb)
        if a[n]["f"] > 0 and b[n]["f"] > 0:
            rows.append((pos, deg, along, np.log(da / db), np.log(a[n]["f"] / b[n]["f"])))
    r = np.array(rows)
    for label, sel in (("all shared", np.ones(len(r), bool)), (f"positions over {AGREE[1]:.0%} apart", r[:, 0] > AGREE[1]),
                       (f"positions within {AGREE[1]:.0%}", r[:, 0] <= AGREE[1])):
        if sel.sum() >= 3:
            x, y = r[sel, 3], r[sel, 4]
            slope = float((x * y).sum() / max((x * x).sum(), 1e-18))
            print(f"{label}: {sel.sum()} cameras; gap along the line of sight (median share) {np.median(r[sel, 2]):.0%}; "
                  f"|log distance ratio| median {np.median(np.abs(x)):.3f}, |log focal ratio| median {np.median(np.abs(y)):.3f}; "
                  f"focal follows distance: slope {slope:.2f}, correlation {np.corrcoef(x, y)[0, 1]:.2f}; "
                  f"rays median {np.median(r[sel, 1]):.2f} deg")


def own(model: Path) -> dict:
    """A model's own numbers: 3D points, the mean photos seeing one, and its points' reprojection error in pixels."""
    errs, tracks = [], []
    with open(model / "points3D.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(8 + 24 + 3)
            e, k = struct.unpack("<dQ", f.read(16))
            f.read(8 * k)
            errs.append(e)
            tracks.append(k)
    return {"points": len(errs), "track": float(np.mean(tracks)) if tracks else 0.0,
            "err_mean": float(np.mean(errs)) if errs else 0.0, "err_p90": float(np.percentile(errs, 90)) if errs else 0.0}


def gravity(readings: dict, cams: dict) -> tuple[int, float, float] | None:
    """(readings among the placed photos, median and 90th-percentile degrees of each from their common direction):
    how far the phones' "down" disagree once turned into the model's frame. None with fewer than 3."""
    d = np.array([cams[n]["R"].T @ np.asarray(v) for n, v in readings.items() if n in cams])
    if len(d) < 3:
        return None
    mid = np.median(d, axis=0)
    off = np.degrees(np.arccos(np.clip(d @ (mid / np.linalg.norm(mid)), -1, 1)))
    return len(d), float(np.median(off)), float(np.percentile(off, 90))


def boulder(P: str) -> None:
    import orientation
    models = {}
    for d in sorted(bench.R.glob(f"{P}/*/*")):
        if ".stale" not in d.name and (d / "rest" / "rest2" / "images.bin").exists():
            models[d.name if d.parent.name == "sift391" else f"{d.parent.name}/{d.name}"] = d / "rest" / "rest2"
    for d in sorted((bench.BENCH / "incr" / "r" / P).glob("*/model")):      # the incremental pipeline's states
        if (d / "images.bin").exists():
            models[f"pipeline:{d.parent.name}"] = d
    if len(models) < 2:
        return
    cams = {k: load(v) for k, v in models.items()}
    photos = bench.BENCH / "projects" / P / "photos"
    readings = {p.name: r["down"] for p in sorted(photos.iterdir()) if (r := orientation.reading(p))}
    names = sorted(models, key=lambda k: (k != BASE, k))
    base = cams.get(BASE, {})
    print(f"\n### {P}: {len(list(photos.iterdir()))} photos, {len(readings)} with a gravity reading\n")
    pair = {(x, y): overlay(cams[x], cams[y]) for x in names for y in names if x != y}
    rays = lambda o: float((o["deg"] < AGREE[0]).mean())      # noqa: E731
    stand = lambda o: float((o["pos"] < AGREE[1]).mean())      # noqa: E731
    print(f"Each model against the others: the median (and the least), over the other models, of the share of the "
          f"cameras both place whose rays are within {AGREE[0]:g} degrees, and of the share standing within "
          f"{AGREE[1]:.0%} of the scene's size (a camera can stand further off with a longer lens and see the same: "
          f"consensus.py trade); then its own numbers.\n")
    print(f"| Model | Placed | Rays within {AGREE[0]:g} deg of the others': median; least | Standing within {AGREE[1]:.0%}: median; least "
          "| Points | Track | Reprojection error: mean; 90th (px) "
          f"| Lenses that can't be real | Cameras seeing under {TIED} points | Places that base doesn't / misses |\n"
          "|---|---|---|---|---|---|---|---|---|---|")
    for x in names:
        others = [pair[x, y] for y in names if y != x and pair[x, y]]
        o = own(models[x])
        print(f"| {x} | {len(cams[x])} | {np.median([rays(q) for q in others]):.0%}; {min(rays(q) for q in others):.0%} "
              f"| {np.median([stand(q) for q in others]):.0%}; {min(stand(q) for q in others):.0%} | {o['points']:,} | {o['track']:.1f} "
              f"| {o['err_mean']:.2f}; {o['err_p90']:.2f} | {sum(c['odd'] for c in cams[x].values())} "
              f"| {sum(c['seen'] < TIED for c in cams[x].values())} "
              f"| {len(set(cams[x]) - set(base))} / {len(set(base) - set(cams[x]))} |")
    if any(gravity(readings, cams[x]) for x in names):
        print("\nGravity readings among each model's placed photos (how many; degrees from their common direction, "
              "median and 90th): " + "; ".join(f"{x}: {'%d; %.1f, %.1f' % g}" for x in names if (g := gravity(readings, cams[x]))))
    print("\nEach pair: of the cameras both place, the share whose rays are within 1 / 2 / 5 degrees, and whose positions "
          "within 1 / 2 / 5% of the scene's size.\n")
    print("| A | B | Shared | Rays within 1 / 2 / 5 deg | Position within 1 / 2 / 5% | Rays: median, 90th deg |\n|---|---|---|---|---|---|")
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            if o := pair[x, y]:
                print(f"| {x} | {y} | {len(o['names'])} | " + " / ".join(f"{(o['deg'] < t).mean():.0%}" for t in (1, 2, 5))
                      + " | " + " / ".join(f"{(o['pos'] < t).mean():.0%}" for t in (0.01, 0.02, 0.05))
                      + f" | {np.median(o['deg']):.2f}, {np.percentile(o['deg'], 90):.2f} |")
    for x in names:      # the photos a model places that the baseline doesn't: how well tied, and who else places them
        extra = sorted(set(cams[x]) - set(base))
        if extra and x != BASE:
            seen = [cams[x][n]["seen"] for n in extra]
            others = [f"{y} ({len(set(extra) & set(cams[y]))})" for y in names if y not in (x, BASE) and set(extra) & set(cams[y])]
            print(f"\n{x} places {len(extra)} that {BASE} doesn't. Points each of those sees: {sorted(seen)} (the model's "
                  f"median: {np.median([c['seen'] for c in cams[x].values()]):.0f}). "
                  + (f"Also placed by: {', '.join(others)}." if others else "No other model places any of them."))


if __name__ == "__main__":
    if sys.argv[1:2] == ["trade"]:      # trade <project> <run a> <run b>
        trade(*(bench.R / sys.argv[2] / "sift391" / run / "rest" / "rest2" for run in sys.argv[3:5]))
    else:
        for proj in sys.argv[1:] or sorted(p.name for p in (bench.BENCH / "projects").iterdir()):
            boulder(proj)
