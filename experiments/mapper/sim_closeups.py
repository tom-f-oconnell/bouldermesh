"""Simulated close-ups of one hold, to see what the pipeline does with them: sim_closeups.py

    sim_closeups.py make <project> <new project> [--zooms 4] [--views 24] [--run global_tied-s0+complete@prod]
    sim_closeups.py report <new project> [...]        Markdown, after `bench.py all <new project> global_tied 0 ...
                                                      --post complete --mvs as23` has run

make: picks a "hold" in a boulder's finished model (the 3D point near the rock's middle that most photos see; its
region is HOLD_R of the scene's size round it), and from the placed photos that see it, in directions spread apart,
cuts the window round it and enlarges that by each zoom: closeup_x<zoom>_<photo>. A window for zoom z is 6 hold
radii across times (largest zoom / z), so --zooms 2,4 is a ladder: a half-way photo for each close one. The new
project (<BENCH>/projects/<new project>) is the boulder's own photos and masks plus those, with truth.json.

What this does and doesn't simulate. An enlarged window is a longer lens from the same spot, not a phone held near:
no new parallax, and no detail the photo didn't have. But the dense step works at a quarter of each photo's size
(--resolution-level 2), so a window enlarged 4 times reaches it at the photo's own pixels: the region gets depth maps
16 times as dense as it had, from real pixels. And matching has to bridge the zoom, as it would a real step closer.
Real close-ups are harder in one way this can't show: they have fine texture the wide photos never saw.

report: how many close-ups ended placed and by which step, how right their cameras are (against the photo each was cut
from: where it stands, where it looks, its focal length), and the mesh's triangles in the hold's region against the
boulder's own run's.
"""
import json
import re
import shutil
import struct
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "backend")]
import bench  # noqa: E402
import consensus  # noqa: E402
import surface  # noqa: E402

MIN_WINDOW = 240        # a window is at least this many of its photo's own pixels across
HOLD_R = 0.04           # the hold's region, as a share of the scene's size (16 cm if cameras stand 4 m off)
RUN = "global_tied-s0+complete@prod"
POST = "post-complete+as23@prod"


def model_of(P: str, run: str = RUN) -> Path:
    return bench.R / P / "sift391" / run / "rest" / "rest2"


def images_by_id(model: Path) -> dict[int, str]:
    out = {}
    with open(model / "images.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            iid = struct.unpack("<I", f.read(4))[0]
            f.read(60)
            name = b""
            while (c := f.read(1)) != b"\0":
                name += c
            f.read(24 * struct.unpack("<Q", f.read(8))[0])
            out[iid] = name.decode()
    return out


def points(model: Path) -> tuple[np.ndarray, list[list[int]]]:
    """(each 3D point, the ids of the images that see it)."""
    xyz, seen = [], []
    with open(model / "points3D.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(8)
            xyz.append(struct.unpack("<3d", f.read(24)))
            f.read(3 + 8)
            k = struct.unpack("<Q", f.read(8))[0]
            seen.append([i for i, _ in struct.iter_unpack("<II", f.read(8 * k))])
    return np.array(xyz), seen


def lenses(model: Path) -> dict[str, dict]:
    import orientation
    return {c["name"]: c["lens"] for c in orientation.read_cameras(model)}


def scene(cams: dict) -> float:
    c = np.array([x["c"] for x in cams.values()])
    return float(np.median(np.linalg.norm(c - np.median(c, axis=0), axis=1)))


def make(P: str, new: str, zooms: list[int], views: int, run: str) -> None:
    from PIL import Image
    model = model_of(P, run)
    cams, lens, names = consensus.load(model), lenses(model), images_by_id(model)
    xyz, seen = points(model)
    size, rock = scene(cams), np.median(xyz, axis=0)
    near = np.linalg.norm(xyz - rock, axis=1) < 0.35 * size
    hold = xyz[max(np.flatnonzero(near), key=lambda i: len(set(seen[i])))]
    r = HOLD_R * size
    around = np.linalg.norm(xyz - hold, axis=1) < 2 * r
    sees = {}                                    # photo: how many of the model's points round the hold it sees
    for i in np.flatnonzero(around):
        for iid in set(seen[i]):
            sees[names[iid]] = sees.get(names[iid], 0) + 1
    cands = []
    for n, k in sees.items():
        L, c = lens[n], cams[n]
        if k < 10 or L["model"] != 2:
            continue
        f, cx, cy, kd = L["params"]
        x = c["R"] @ (hold - c["c"])
        if x[2] <= 0:
            continue
        xn = x[:2] / x[2]
        u, v = f * xn * (1 + kd * xn @ xn) + (cx, cy)
        half = max(3 * r * f / x[2], MIN_WINDOW / 2)     # the closest window: 6 hold radii across, or MIN_WINDOW pixels
        if not (half < u < L["width"] - half and half < v < L["height"] - half):
            continue                             # the window leaves the picture
        cands.append({"photo": n, "u": float(u), "v": float(v), "half": float(half), "depth": float(x[2] / size),
                      "dir": (c["c"] - hold) / np.linalg.norm(c["c"] - hold), "f": float(f)})
    chosen = [max(cands, key=lambda c: c["half"])]
    while len(chosen) < min(views, len(cands)):      # then whichever looks from furthest from those chosen
        chosen.append(max((c for c in cands if c not in chosen), key=lambda c: min(1 - c["dir"] @ d["dir"] for d in chosen)))
    src, dst = bench.BENCH / "projects" / P, bench.BENCH / "projects" / new
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src / "photos", dst / "photos")
    if (src / "masks").exists():
        shutil.copytree(src / "masks", dst / "masks")
    truth = {"from": P, "run": run, "hold": hold.tolist(), "r": r, "size": size, "zooms": zooms, "candidates": len(cands), "closeups": []}
    for c in chosen:
        with Image.open(src / "photos" / c["photo"]) as im:
            im = im.convert("RGB")
            mask = src / "masks" / f"{c['photo']}.png"
            for z in zooms:
                half = min(c["half"] * max(zooms) / z, c["u"], c["v"], im.width - c["u"], im.height - c["v"])
                box = tuple(int(round(t)) for t in (c["u"] - half, c["v"] - half, c["u"] + half, c["v"] + half))
                out = (int(round(2 * half * z)),) * 2
                name = f"closeup_x{z}_{Path(c['photo']).stem}.jpg"
                im.crop(box).resize(out, Image.LANCZOS).save(dst / "photos" / name, quality=95)
                if mask.exists():
                    with Image.open(mask) as m:      # a mask can be smaller than its photo; cut and enlarged by index
                        arr, k = np.asarray(m.convert("L")), m.width / im.width      # (this PIL's NEAREST resize fails on them)
                    ys = np.clip(((np.arange(out[1]) + 0.5) / z + box[1]) * k, 0, arr.shape[0] - 1).astype(int)
                    xs = np.clip(((np.arange(out[0]) + 0.5) / z + box[0]) * k, 0, arr.shape[1] - 1).astype(int)
                    Image.fromarray(arr[np.ix_(ys, xs)]).save(dst / "masks" / f"{name}.png")
                truth["closeups"].append({"name": name, "photo": c["photo"], "zoom": z, "box": box, "f": c["f"] * z,
                                          "own_pixels": box[2] - box[0], "depth": c["depth"]})
    (dst / "truth.json").write_text(json.dumps(truth, indent=1))
    angles = [float(np.degrees(np.arccos(np.clip(a["dir"] @ b["dir"], -1, 1)))) for i, a in enumerate(chosen) for b in chosen[i + 1:]]
    print(f"{new}: a hold that {len(sees)} photos see, {len(cands)} of them with room for a window; {len(chosen)} chosen "
          f"(windows of {min(c['half'] for c in chosen) * 2:.0f}-{max(c['half'] for c in chosen) * 2:.0f} of their own pixels, "
          f"up to {max(angles, default=0):.0f} degrees apart), x{zooms}: {len(truth['closeups'])} close-ups, "
          f"{len(list((dst / 'photos').iterdir()))} photos in all")


def edges(V: np.ndarray, F: np.ndarray, p: np.ndarray, radius: float) -> tuple[int, float]:
    """(faces within radius of p, their median edge length)."""
    T = V[F[np.linalg.norm(V[F].mean(1) - p, axis=1) < radius]]
    if not len(T):
        return 0, float("nan")
    return len(T), float(np.median(np.concatenate([np.linalg.norm(T[:, i] - T[:, (i + 1) % 3], axis=1) for i in range(3)])))


def report(new: str) -> None:
    truth = json.loads((bench.BENCH / "projects" / new / "truth.json").read_text())
    P, hold, r = truth["from"], np.array(truth["hold"]), truth["r"]
    d = bench.R / new / "sift391" / RUN
    ref, got = consensus.load(model_of(P, truth["run"])), consensus.load(d / "rest" / "rest2")
    o = consensus.overlay({k: v for k, v in ref.items()}, {k: v for k, v in got.items() if k in ref})
    s, R, t = o["fit"]                                       # the boulder's own model's frame into this run's
    mapped = json.loads((d / "models.json").read_text())
    by_mapper = set(max(mapped.values(), key=len))
    dropped = set((d / "rest" / "drop.txt").read_text().split())
    ups = truth["closeups"]
    placed = [c for c in ups if c["name"] in got]
    lens, lens_ref = lenses(d / "rest" / "rest2"), lenses(model_of(P, truth["run"]))
    print(f"\n### {new}: {len(ups)} close-ups (x{truth['zooms']}) of one hold added to {P[:4]}'s {len(ref)}-photo model\n")
    print(f"- Photos placed: {len(got)} ({len([k for k in got if not k.startswith('closeup_')])} of the boulder's own; its own run: {len(ref)}).")
    for z in truth["zooms"]:
        mine = [c for c in ups if c["zoom"] == z]
        ok = [c for c in mine if c["name"] in got]
        print(f"- x{z}: {len(ok)} of {len(mine)} placed: {sum(c['name'] in by_mapper and c['name'] not in dropped for c in ok)} by the "
              f"global mapper, {sum(c['name'] not in by_mapper or c['name'] in dropped for c in ok)} by the rest step "
              f"({sum(c['name'] in dropped for c in mine)} the mapper had left untied).")
    pos, ang, foc, seen = [], [], [], []
    for c in placed:
        a, b = got[c["name"]], ref[c["photo"]]
        pos.append(np.linalg.norm(a["c"] - (s * R @ b["c"] + t)) / (s * truth["size"]))
        Lr = lens_ref[c["photo"]]["params"]                       # where the window's middle looks, in the photo it was cut from
        want = b["R"].T @ np.array([((c["box"][0] + c["box"][2]) / 2 - Lr[1]) / Lr[0], ((c["box"][1] + c["box"][3]) / 2 - Lr[2]) / Lr[0], 1.0])
        ang.append(np.degrees(np.arccos(np.clip(a["rays"][0] @ (R @ want / np.linalg.norm(want)), -1, 1))))
        foc.append(lens[c["name"]]["params"][0] / (c["zoom"] * (lens[c["photo"]]["params"][0] if c["photo"] in lens else c["f"] / c["zoom"])))
        seen.append(a["seen"])
    if placed:
        q = lambda v: f"{np.median(v):.2f} (90th {np.percentile(v, 90):.2f})"      # noqa: E731
        print(f"- The placed close-ups' cameras against the photo each was cut from: standing {np.median(pos):.1%} of the scene's size "
              f"away (90th {np.percentile(pos, 90):.1%}); looking {q(ang)} degrees off; focal length {q(foc)} times what the zoom "
              f"makes it; points seen {np.median(seen):.0f} (fewest {min(seen)}).")
    logs = lambda step: (d / POST / f"{step}.log").read_text(errors="replace") if (d / POST / f"{step}.log").exists() else ""      # noqa: E731
    sel = re.findall(r"Selecting images for dense reconstruction completed: (\d+) images", logs("dense"))
    Va, Fa = surface.mesh(bench.R / P / "sift391" / truth["run"] / POST / surface.GLB)
    Vb, Fb = surface.mesh(d / POST / surface.GLB)
    h2 = s * R @ hold + t
    print(f"- Depth maps: {sel[-1] if sel else '?'} photos. The mesh in the hold's region, the boulder's own run against this one:\n")
    print("| within | faces before | faces now | median edge before, of the scene's size | now | finer by |\n|---|---|---|---|---|---|")
    for k in (1, 2, 4):
        na, ea = edges(Va, Fa, hold, k * r)
        nb, eb = edges(Vb, Fb, h2, k * r * s)
        print(f"| {k} hold radii | {na:,} | {nb:,} | {ea / truth['size']:.3%} | {eb / (s * truth['size']):.3%} | {ea * s / eb:.1f}x |")
    # is the finer surface the same surface? points spread over each mesh within 3 radii, the old laid over the new there
    from scipy.spatial import cKDTree
    rng = np.random.default_rng(0)
    cut = lambda V, F, p, rad: F[np.linalg.norm(V[F].mean(1) - p, axis=1) < rad]      # noqa: E731
    A = surface.spread(Va, cut(Va, Fa, hold, 3 * r), 20000, rng) @ (s * R).T + t
    B = surface.spread(Vb, cut(Vb, Fb, h2, 3.5 * r * s), 200000, rng)
    tree = cKDTree(B)
    before = np.median(tree.query(A)[0])
    for _ in range(15):
        dist, i = tree.query(A)
        k = dist <= np.percentile(dist, 70)
        s2, R2, t2 = surface.similarity(A[k], B[i[k]])
        A = A @ (s2 * R2).T + t2
    dist = tree.query(A)[0] / (s * truth["size"])
    print(f"\n- The old surface against the new within 3 hold radii: median {before / (s * truth['size']):.2%} of the scene's size apart as "
          f"the cameras lay them, {np.median(dist):.2%} (90th {np.percentile(dist, 90):.2%}) once fitted there: "
          f"{np.median(dist) * truth['size'] / ea:.1f} of the old mesh's own edge.")


if __name__ == "__main__":
    if sys.argv[1] == "make":
        a = sys.argv[2:]
        opt = lambda flag, default: a[a.index(flag) + 1] if flag in a else default      # noqa: E731
        make(a[0], a[1], [int(z) for z in opt("--zooms", "4").split(",")], int(opt("--views", "24")), opt("--run", RUN))
    else:
        for name in sys.argv[2:]:
            report(name)
