#!/usr/bin/env python3
"""Which way a mesh faces, from the skyline in its placed photos against the horizon worked out from elevation data at
its coordinates. An experiment (README.md here): nothing in the app uses it.

    venv/bin/python experiments/skyline/skyline.py --data DIR [--index server-meshes.json] [--out DIR]
        [--parallax] [--halves] [--pairs] [--photos PATTERN] MESH_ID...

DIR holds meshes/<id> (cameras.json, mesh.glb, cleanup) and projects/<pid>/photos; the index gives each mesh's place and
project (GET /api/meshes, saved), else DIR/bouldermesh.sqlite does. For each mesh it writes <out>/<id>.png (the horizon
against what the photos show, at the best heading) and a line of results to <out>/results.jsonl; with --pairs, a line
for each two meshes sharing photos to <out>/pairs.jsonl.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import math
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "6")
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
import beta  # noqa: E402
import heading  # noqa: E402
import meshclean  # noqa: E402

TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"   # AWS Terrain Tiles (Mapzen)
NEAR_Z, NEAR_M, FAR_Z, FAR_M = 14, 2500, 10, 50000     # zoom and reach of the near and far elevation
STEP = 0.25            # degrees of azimuth between horizon samples
EYE = 1.6              # metres above the ground the camera is taken to be
SKY = 2                # ADE20K's sky
DISTANT = {16, 68}     # ADE20K's mountain and hill: what's below a skyline point that's far enough away to be in the elevation data
THREADS = 6            # for the segmentation and numpy: hal is shared
SIGMA = 0.75           # degrees: how well a skyline point is expected to fit
ABOVE = 0.15           # weight of a point above the terrain's horizon (trees, rocks and ground near by may add height)
MODEL = ROOT / "data" / "cache" / "models" / "skyline" / "segformer-b0-ade.onnx"


# ------------------------------------------------------------------------------------------------ elevation
class Elevation:
    """Heights from AWS's Terrain Tiles (Terrarium PNGs), at two zooms, cached under cache/."""

    def __init__(self, cache: Path):
        self.cache, self.tiles = cache, {}

    def tile(self, z: int, x: int, y: int) -> np.ndarray:
        if (z, x, y) not in self.tiles:
            f = self.cache / str(z) / str(x) / f"{y}.png"
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(urllib.request.urlopen(urllib.request.Request(TILES.format(z=z, x=x, y=y),
                                                                            headers={"User-Agent": "BoulderMesh experiment"}), timeout=60).read())
            a = np.asarray(Image.open(f).convert("RGB"), float)
            self.tiles[(z, x, y)] = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
        return self.tiles[(z, x, y)]

    def at(self, z: int, lat: np.ndarray, lng: np.ndarray) -> np.ndarray:
        """Bilinear heights at points, from zoom z's tiles."""
        n = 2 ** z
        px = (lng + 180) / 360 * n * 256 - .5
        py = (1 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2 * n * 256 - .5
        x0, y0 = np.floor(px).astype(int), np.floor(py).astype(int)
        fx, fy = px - x0, py - y0
        out = np.zeros(px.shape)
        for dx, dy, w in ((0, 0, (1 - fx) * (1 - fy)), (1, 0, fx * (1 - fy)), (0, 1, (1 - fx) * fy), (1, 1, fx * fy)):
            gx, gy = x0 + dx, y0 + dy
            for key in {(int(a), int(b)) for a, b in zip((gx // 256).ravel(), (gy // 256).ravel())}:
                sel = (gx // 256 == key[0]) & (gy // 256 == key[1])
                out[sel] += w[sel] * self.tile(z, key[0], key[1])[gy[sel] % 256, gx[sel] % 256]
        return out

    def horizon(self, lat: float, lng: float) -> tuple[np.ndarray, np.ndarray, float]:
        """The horizon's elevation angle (degrees) at each STEP of compass bearing from 0, seen EYE m above the ground
        at (lat, lng), with the earth's curve and refraction; how far away each is; and the ground's height there."""
        kx, ky = 111320 * math.cos(math.radians(lat)), 110574.0
        ground = float(self.at(NEAR_Z, np.array([lat]), np.array([lng]))[0])
        d = np.unique(np.concatenate([np.arange(20, NEAR_M, 10.0), np.geomspace(NEAR_M, FAR_M, 250)]))
        az = np.radians(np.arange(0, 360, STEP))
        la, ln = lat + np.outer(np.cos(az), d) / ky, lng + np.outer(np.sin(az), d) / kx
        near = d <= NEAR_M
        z = np.empty(la.shape)
        z[:, near] = self.at(NEAR_Z, la[:, near], ln[:, near])
        z[:, ~near] = self.at(FAR_Z, la[:, ~near], ln[:, ~near])
        el = np.degrees(np.arctan2(z - d ** 2 / (2 * 6371000) * (1 - 0.13) - ground - EYE, d))
        return el.max(1), d[el.argmax(1)], ground


# ------------------------------------------------------------------------------------------------ the photos' skylines
class Sky:
    def __init__(self):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = opts.inter_op_num_threads = THREADS
        self.sess = ort.InferenceSession(str(MODEL), opts, providers=["CPUExecutionProvider"])
        self.mean, self.std = np.array([0.485, 0.456, 0.406]), np.array([0.229, 0.224, 0.225])

    def labels(self, im: Image.Image) -> np.ndarray:
        """ADE20K classes over a 512 x 512 grid laid over the photo."""
        x = ((np.asarray(im.resize((512, 512), Image.BILINEAR), np.float32) / 255 - self.mean) / self.std).transpose(2, 0, 1)[None]
        logits = self.sess.run(None, {"pixel_values": x.astype(np.float32)})[0][0]
        return np.asarray(Image.fromarray(logits.argmax(0).astype(np.uint8)).resize((512, 512), Image.NEAREST))


def rock_mask(cam: dict, size, V: np.ndarray) -> np.ndarray:
    """Where the mesh is in a photo, on the 512 x 512 grid, a little grown: its outline there is the rock's own."""
    f, cx, cy, k, _ = beta.lens(cam, size)
    p = V @ beta._rotation(cam["q"]).T + np.asarray(cam["t"], float)
    p = p[p[:, 2] > 1e-6]
    x, y = p[:, 0] / p[:, 2], p[:, 1] / p[:, 2]
    r = 1 + k * (x * x + y * y)
    u, v = (f * x * r + cx) * 512 / size[0], (f * y * r + cy) * 512 / size[1]
    m = np.zeros((512, 512), bool)
    ok = (u >= 0) & (u < 512) & (v >= 0) & (v < 512)
    m[v[ok].astype(int), u[ok].astype(int)] = True
    for _ in range(4):     # grown by 4 cells, as vertices are scattered
        m = m | np.roll(m, 1, 0) | np.roll(m, -1, 0) | np.roll(m, 1, 1) | np.roll(m, -1, 1)
    return m


def skyline(lab: np.ndarray, rock: np.ndarray, every: int = 4) -> list[tuple[int, int]]:
    """(column, row) on the 512 grid where sky meets terrain beyond the rock: sky from the top of the column down, then
    at least 3 cells of mountain or hill, not where the mesh itself is."""
    out = []
    for c in range(0, 512, every):
        col = lab[:, c]
        if col[0] != SKY:
            continue
        r = int(np.argmax(col != SKY))
        if r == 0 or r > 500 or r < 5:
            continue
        below = col[r:r + 3]
        if not all(int(b) in DISTANT for b in below) or rock[max(0, r - 2):r + 4, max(0, c - 2):c + 3].any():
            continue
        out.append((c, r))
    return out


def mesh_points(mesh_dir: Path) -> np.ndarray:
    """Up to 60,000 of a mesh's vertices, those cleanup kept."""
    j, bufs = meshclean._read_glb(meshclean.mesh_file(mesh_dir))
    V, F, _ = meshclean._geometry(j, bufs)
    clean = meshclean.load(mesh_dir)
    if clean and clean.get("removed") and (mesh_dir / meshclean.FACES_FILE).exists():
        labels = np.frombuffer((mesh_dir / meshclean.FACES_FILE).read_bytes(), {2: "<u2", 4: "<u4"}[clean["label_bytes"]])
        keep = ~np.isin(labels, clean["removed"])
        V = V[np.unique(F[keep])]
    return V[np.random.default_rng(0).choice(len(V), min(len(V), 60000), replace=False)]


def observe(sky: Sky, mesh_dir: Path, photos: Path, pattern: str = "*"):
    """Each placed photo's skyline points as [n, 2] arrays of (bearing in the mesh's frame, elevation), for photos with
    at least 8; with their cameras' centres and names, and how many photos were looked at."""
    V = mesh_points(mesh_dir)
    obs, centres, names, used = [], [], [], 0
    for cam in json.loads((mesh_dir / "cameras.json").read_text()):
        f = photos / cam["name"]
        if not f.exists() or not fnmatch.fnmatch(cam["name"], pattern):
            continue
        with Image.open(f) as im:
            im = im.convert("RGB")
            size = im.size
            lab = sky.labels(im)
        pts = []
        for c, r in skyline(lab, rock_mask(cam, size, V)):
            _, d = beta.camera_ray(cam, size, ((c + 2) * size[0] / 512, r * size[1] / 512))
            pts.append((bearing(d[0], d[2]), math.degrees(math.asin(max(-1.0, min(1.0, d[1]))))))
        used += 1
        if len(pts) >= 8:
            obs.append(np.array(pts))
            names.append(cam["name"])
            centres.append(-beta._rotation(cam["q"]).T @ np.asarray(cam["t"], float))
    return obs, centres, names, used


def far_only(obs: list[np.ndarray], centres: list[np.ndarray], metres: float, baseline: float = 8.0, tol: float = 0.5):
    """Each photo's skyline points that another photo, taken at least `baseline` metres away, sees at the same bearing
    (within 0.5°) and the same height (within tol degrees): too far away to shift between them (B / D radians, so about
    a kilometre or more for 8 m and 0.5°), so terrain the elevation data can have, not a rock or tree near by."""
    keep = [np.zeros(len(o), bool) for o in obs]
    order = [np.argsort(o[:, 0]) for o in obs]
    for k, (pk, ck) in enumerate(zip(obs, centres)):
        for m, (pm, cm) in enumerate(zip(obs, centres)):
            if m == k or np.hypot(*(ck - cm)[[0, 2]]) * metres < baseline:
                continue
            az = pm[order[m], 0]
            i = np.clip(np.searchsorted(az, pk[:, 0]), 1, len(az) - 1)
            for j in (i - 1, i):
                near = np.abs(az[j] - pk[:, 0]) < 0.5
                same = np.abs(pm[order[m][j], 1] - pk[:, 1]) < tol
                keep[k] |= near & same
    return [o[kp] for o, kp in zip(obs, keep)]


def bearing(x: float, z: float) -> float:
    return math.degrees(math.atan2(x, -z)) % 360


# ------------------------------------------------------------------------------------------------ matching
def match(obs: list[np.ndarray], el: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The loss of each heading (each STEP), for photos' points (each [n, 2]: bearing in the mesh's frame, elevation),
    each photo's mean counting once, with the best constant offset within ±4° (an up a little off); and the offsets."""
    n = len(el)
    heads = np.arange(n)
    loss, offset = np.zeros(n), np.zeros(n)
    best = np.full(n, np.inf)
    for b in np.arange(-4, 4.01, 0.5):
        total = np.zeros(n)
        for pts in obs:
            idx = (np.round(pts[:, 0] / STEP).astype(int)[None, :] + heads[:, None]) % n      # [heads, points]
            r = pts[None, :, 1] - (el[idx] + b)
            rho = np.where(r < 0, np.minimum((r / SIGMA) ** 2, 9), ABOVE * np.minimum((r / SIGMA) ** 2, 9))
            total += rho.mean(1)
        better = total < best
        best[better], offset[better] = total[better], b
    loss = best / max(1, len(obs))
    return loss, offset


def apart(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def halves(obs: list[np.ndarray], names: list[str], el: np.ndarray) -> dict:
    """The heading each of two halves of the photos gives on its own, as a check of how repeatable it is: a video's
    frames all go in one half (they share a viewpoint and lighting), the halves as even in points as they can be."""
    groups: dict[str, list[int]] = {}
    for k, n in enumerate(names):
        groups.setdefault(n.split("_")[1] if n.startswith("yt_") else n, []).append(k)
    if len(groups) < 2:
        return {"halves": None}
    sides, points = ([], []), [0, 0]
    for g in sorted(groups.values(), key=lambda g: -sum(len(obs[k]) for k in g)):
        s = int(points[1] < points[0])
        sides[s].extend(g)
        points[s] += sum(len(obs[k]) for k in g)
    found = []
    for side in sides:
        loss, _ = match([obs[k] for k in side], el)
        found.append(float(loss.argmin() * STEP))
    return {"halves": found, "halves_photos": [len(s) for s in sides], "halves_points": points,
            "halves_apart": round(apart(*found), 1)}


def frames_apart(a: Path, b: Path) -> dict | None:
    """How mesh a's frame is turned from mesh b's, from the photos placed in both. The rotation that best takes
    directions in a's frame to b's gives how much further a's heading is than b's (h_a - h_b, when both headings are
    right), how far their ups differ, and single photos' median disagreement with it. None when they share fewer
    than 3."""
    R = [{c["name"]: beta._rotation(c["q"]) for c in json.loads((d / "cameras.json").read_text())} for d in (a, b)]
    shared = sorted(set(R[0]) & set(R[1]))
    if len(shared) < 3:
        return None
    Ms = [R[1][n].T @ R[0][n] for n in shared]       # each takes a direction in a's frame to b's
    U, _, Vt = np.linalg.svd(sum(Ms))
    M = U @ np.diag([1, 1, np.linalg.det(U @ Vt)]) @ Vt
    angle = lambda X: math.degrees(math.acos(np.clip((np.trace(X) - 1) / 2, -1, 1)))    # noqa: E731
    f = M @ np.array([0, 0, -1.0])
    turn = (bearing(f[0], f[2]) + 180) % 360 - 180
    return {"shared": len(shared), "a_minus_b": round(turn, 2),
            "ups_apart": round(math.degrees(math.acos(np.clip((M @ np.array([0, 1.0, 0]))[1], -1, 1))), 2),
            "photo_disagreement": round(float(np.median([angle(M.T @ m) for m in Ms])), 2)}


def draw(path: Path, el: np.ndarray, obs: list[np.ndarray], h: float, b: float, loss: np.ndarray, marks: dict, title: str):
    """The horizon (grey) against each photo's skyline points turned by heading h (coloured), and the loss by heading."""
    W, H, top = 1440, 330, 150
    lo, hi = min(-1.0, float(el.min()) - 1), max(float(el.max()) + 3, 5.0)
    im = Image.new("RGB", (W, H + top), "white")
    g = ImageDraw.Draw(im)
    y = lambda e: H - 10 - (e - lo) / (hi - lo) * (H - 30)    # noqa: E731
    for a in range(0, 360, 45):
        g.line([(a * 4, 0), (a * 4, H)], fill=(225, 225, 225))
        g.text((a * 4 + 3, 3), "N NE E SE S SW W NW".split()[a // 45], fill=(120, 120, 120))
    g.line([(i, y(el[i])) for i in range(W)], fill=(110, 110, 110), width=2)
    colours = [(214, 39, 40), (31, 119, 180), (44, 160, 44), (255, 127, 14), (148, 103, 189), (140, 86, 75), (227, 119, 194)]
    for k, pts in enumerate(obs):
        for a, e in pts:
            x = ((a + h) % 360) * 4
            g.ellipse([x - 1.5, y(e - b) - 1.5, x + 1.5, y(e - b) + 1.5], fill=colours[k % len(colours)])
    g.text((8, H - 22), title, fill=(0, 0, 0))
    # the loss by heading, with the best and any other estimates marked
    l0, l1 = float(loss.min()), float(np.percentile(loss, 95))
    ly = lambda v: H + top - 10 - (min(v, l1) - l0) / max(1e-9, l1 - l0) * (top - 30)    # noqa: E731
    g.line([(i, ly(loss[i])) for i in range(W)], fill=(60, 60, 60), width=1)
    for name, (deg, colour) in marks.items():
        if deg is None:
            continue
        g.line([(deg * 4, H + 15), (deg * 4, H + top - 5)], fill=colour, width=2)
        g.text((deg * 4 + 4, H + 18 + 14 * list(marks).index(name)), f"{name} {deg:.0f}", fill=colour)
    im.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--index", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument("--cache", type=Path, default=ROOT / "data" / "cache" / "terrarium")
    ap.add_argument("--parallax", action="store_true", help="keep only skyline points that don't shift between photos far apart")
    ap.add_argument("--photos", default="*", help="only photos whose names match this pattern (yt_* are video frames)")
    ap.add_argument("--pairs", action="store_true", help="also check meshes that share photos against each other")
    ap.add_argument("--halves", action="store_true", help="also match two halves of the photos (by video) on their own")
    ap.add_argument("meshes", nargs="+")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.index:
        index = {m["id"]: m for m in json.loads(args.index.read_text())}
    else:
        con = sqlite3.connect(f"file:{args.data / 'bouldermesh.sqlite'}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        index = {r["id"]: dict(r) for r in con.execute("SELECT id, name, source, lat, lng FROM meshes")}
    elev, sky = Elevation(args.cache), Sky()
    found = {}
    for mid in args.meshes:
        m, mesh_dir = index[mid], args.data / "meshes" / mid
        photos = args.data / "projects" / m["source"].split(":", 1)[1] / "photos"
        t0 = time.time()
        el, dist, ground = elev.horizon(m["lat"], m["lng"])
        obs, centres, names, used = observe(sky, mesh_dir, photos, args.photos)
        metres = (json.loads((mesh_dir / "scale.json").read_text()).get("metres_per_unit") if (mesh_dir / "scale.json").exists() else None)
        before = int(sum(len(o) for o in obs))
        if args.parallax and metres:
            kept = [(o, n) for o, n in zip(far_only(obs, centres, metres), names) if len(o) >= 5]
            obs, names = [o for o, _ in kept], [n for _, n in kept]
        compass = heading.from_compass(mesh_dir, photos)
        result = {"id": mid, "name": m["name"], "photos": used, "with_skyline": len(obs), "points": int(sum(len(o) for o in obs)),
                  "points_before": before, "metres_per_unit": metres,
                  "horizon_sd": round(float(el.std()), 2), "horizon_km": round(float(np.median(dist)) / 1000, 1),
                  "compass": round(compass["deg"], 1) if compass else None}
        if obs:
            loss, offset = match(obs, el)
            i = int(loss.argmin())
            far = np.abs((np.arange(len(loss)) - i + len(loss) / 2) % len(loss) - len(loss) / 2) * STEP > 15
            j2 = int(np.flatnonzero(far)[loss[far].argmin()])
            h, b = i * STEP, float(offset[i])
            result.update(heading=h, offset=b, loss=round(float(loss[i]), 3), second=j2 * STEP,
                          margin=round(float(loss[j2] / max(loss[i], 1e-9)), 2))
            if args.halves:
                result.update(halves(obs, names, el))
            if compass:
                result["vs_compass"] = round(apart(h, compass["deg"]), 1)
            draw(args.out / f"{mid}.png", el, obs, h, b, loss, {"skyline": (h, (214, 39, 40)), "compass": ((compass or {}).get("deg"), (31, 119, 180))},
                 f"{m['name']}: heading {h:.1f}, offset {b:+.1f} deg, {len(obs)} photos, margin x{result['margin']}")
        result["seconds"] = round(time.time() - t0, 1)
        print(json.dumps(result), flush=True)
        with open(args.out / "results.jsonl", "a") as out:
            out.write(json.dumps(result) + "\n")
        found[mid] = result
    if args.pairs:      # how far apart the frames of two meshes are turned, against how far apart their skylines put them
        for a in found:
            for b in found:
                pair = a < b and "heading" in found[a] and "heading" in found[b] and \
                    frames_apart(args.data / "meshes" / a, args.data / "meshes" / b)
                if pair:
                    said = (found[a]["heading"] - found[b]["heading"] + 180) % 360 - 180
                    pair.update(a=a, b=b, skylines_say=said, off=round(apart(said, pair["a_minus_b"]), 1))
                    print(json.dumps(pair), flush=True)
                    with open(args.out / "pairs.jsonl", "a") as out:
                        out.write(json.dumps(pair) + "\n")


if __name__ == "__main__":
    main()
