#!/usr/bin/env python3
"""Which way a mesh faces, and where it stands, from its footprint against the boulders lidar keeps as bumps in the
ground. An experiment (README.md here): nothing in the app uses it.

    venv/bin/python experiments/footprint/footprint.py --data DIR [--index server-meshes.json] [--out DIR] [--jobs N]
        [--pairs] [--skyline results.jsonl] [--reach M] [--near W] [--lift M] [--tilt DEG] [--cameras W] [--slack M]
        (--all | MESH_ID...)

DIR holds meshes/<id> and projects/<pid>/photos, as for experiments/skyline; --all takes every current mesh in
DIR/bouldermesh.sqlite with a scale. The ground is the viewer's (backend/terrain.py), from data/cache/terrain. For each
mesh it writes <out>/<id>.png (the lidar's bumps around its point with the mesh's footprint placed on them, and the
loss by heading) and a line to <out>/results.jsonl; with --pairs, a line for each two meshes sharing photos to
<out>/pairs.jsonl.
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "experiments" / "skyline"))
import beta  # noqa: E402
import heading  # noqa: E402
import terrain  # noqa: E402
from skyline import apart, frames_apart, mesh_points  # noqa: E402

SIDE = 300          # metres across the lidar square fetched (the viewer's for a 50 m Nearby)
REACH = 40          # metres the mesh's middle is looked for around its coordinates
COARSE = 1.0        # metres: the distance field's cells and the steps tried first, every COARSE_YAW degrees
COARSE_YAW = 5
FINE = 0.5          # metres: the same, refining the best few headings to 1 degree
AIR, INSIDE = 1.0, 0.5     # weight of a mesh point in the air above the lidar's surface, and one under it (an overhang
CAP = 2.0                  # the lidar filled in, its 1 m smoothing an edge); metres a point's distance counts up to
CAMERAS = 1.0       # weight of the photos' cameras inside the lidar's rock or ground (as much as all the mesh's points)
SLACK = 0.0         # metres a camera may be under the ground before it counts (for a mesh's "up" a few degrees off)
NEAR = 0.02         # added to the loss for each (20 m)² the mesh's middle is from its coordinates: they're usually close
LIFT = 1.5          # metres the mesh's foot may be above or below the lowest ground near its middle
TILTS = (0.0,)      # degrees the mesh's "up" may be off by, tried each way about both level axes when refining
APART = 45          # degrees: the margin is against the best other heading at least this far from the best


# ------------------------------------------------------------------------------------------------ the mesh
def mesh_metres(mesh_dir: Path, metres: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The mesh's points as east, north (at heading 0) and up in metres, about the middle of its outline from above,
    with its lowest (2nd percentile) at 0; its photos' cameras the same way (those within 30 m of it); and that
    middle, in the mesh's own units (x, z)."""
    V = mesh_points(mesh_dir)
    P = np.c_[V[:, 0], -V[:, 2], V[:, 1]] * metres
    mid = np.r_[(P[:, :2].min(0) + P[:, :2].max(0)) / 2, np.percentile(P[:, 2], 2)]
    P -= mid
    cams = np.array([-beta._rotation(c["q"]).T @ np.asarray(c["t"], float) for c in json.loads((mesh_dir / "cameras.json").read_text())])
    C = np.c_[cams[:, 0], -cams[:, 2], cams[:, 1]] * metres - mid
    C = C[(np.hypot(C[:, 0], C[:, 1]) < 30) & (C[:, 2] > -5) & (C[:, 2] < 30)]
    return P, C, np.array([mid[0], -mid[1]]) / metres


def spread_out(P: np.ndarray, cell: float, most: int, seed: int = 0) -> np.ndarray:
    """At most `most` of the points, one a cell `cell` metres across at most, so that each part of the surface counts
    by its area rather than by how densely it was meshed."""
    key = np.floor(P / cell).astype(np.int64)
    _, first = np.unique(key, axis=0, return_index=True)
    Q = P[first]
    return Q[np.random.default_rng(seed).choice(len(Q), min(len(Q), most), replace=False)]


def tilted(P: np.ndarray, a: float, b: float) -> np.ndarray:
    """The points tipped a degrees about the east axis (north going up) then b about the north axis (east going down),
    about the mesh's foot: for an "up" that's a little off."""
    if not (a or b):
        return P
    ca, sa, cb, sb = math.cos(math.radians(a)), math.sin(math.radians(a)), math.cos(math.radians(b)), math.sin(math.radians(b))
    e, n, u = P[:, 0], P[:, 1] * ca - P[:, 2] * sa, P[:, 1] * sa + P[:, 2] * ca
    return np.c_[e * cb + u * sb, n, -e * sb + u * cb]


def turned(P: np.ndarray, deg: float) -> np.ndarray:
    """The points turned so that the mesh's -Z points to bearing deg: east, north, up."""
    h = math.radians(deg)
    return np.c_[P[:, 0] * math.cos(h) + P[:, 1] * math.sin(h), -P[:, 0] * math.sin(h) + P[:, 1] * math.cos(h), P[:, 2]]


# ------------------------------------------------------------------------------------------------ the ground
class Ground:
    """A lidar square around a point: heights 1 m apart, its first cell's middle at (e0, n0) metres east and north of
    the point, rows going south."""

    def __init__(self, heights: np.ndarray, e0: float, n0: float, step: float):
        self.h, self.e0, self.n0, self.step = heights, e0, n0, step

    @classmethod
    def around(cls, cache: Path, lat: float, lng: float) -> "Ground | None":
        record = terrain.square(cache, lat, lng, SIDE)
        if not record:
            return None
        p = terrain.placed(record, lat, lng)
        h = np.frombuffer(record["path"].read_bytes(), "<f4").reshape(p["ny"], p["nx"]).astype(float)
        return cls(h, p["west_m"] + p["dx"] / 2, p["north_m"] - p["dy"] / 2, p["dx"])

    def at(self, e: np.ndarray, n: np.ndarray) -> np.ndarray:
        """Heights at points (metres east and north of the point), bilinear."""
        from scipy.ndimage import map_coordinates
        e, n = np.asarray(e, float), np.asarray(n, float)
        return map_coordinates(self.h, [(self.n0 - n) / self.step, (e - self.e0) / self.step], order=1, mode="nearest")


class Field:
    """How far each place within `radius` metres of (ce, cn) is above the lidar's surface (negative: under it), in
    cells `cell` metres across: a signed distance field, so that a mesh's walls are near the surface too, not only its
    tops."""

    def __init__(self, ground: Ground, ce: float, cn: float, radius: float, cell: float):
        from scipy.ndimage import distance_transform_edt
        self.cell, self.west, self.north = cell, ce - radius, cn + radius
        n = int(round(2 * radius / cell)) + 1
        E, N = np.meshgrid(self.west + np.arange(n) * cell, self.north - np.arange(n) * cell)
        H = ground.at(E, N)
        self.bottom = float(H.min()) - 4
        nz = int(math.ceil((float(H.max()) + 12 - self.bottom) / cell))
        z = self.bottom + (np.arange(nz) + 0.5) * cell
        inside = z[:, None, None] < H[None]
        self.d = ((distance_transform_edt(~inside) - 0.5).clip(0) - (distance_transform_edt(inside) - 0.5).clip(0)) * cell
        self.H = H

    def at(self, e: np.ndarray, n: np.ndarray, z: np.ndarray) -> np.ndarray:
        nz, ny, nx = self.d.shape
        k = np.clip(np.floor((z - self.bottom) / self.cell).astype(np.int64), 0, nz - 1)
        i = np.clip(np.rint((self.north - n) / self.cell).astype(np.int64), 0, ny - 1)
        j = np.clip(np.rint((e - self.west) / self.cell).astype(np.int64), 0, nx - 1)
        return self.d[k, i, j]

    def ground(self, e: np.ndarray, n: np.ndarray, radius: float) -> np.ndarray:
        """The lowest the lidar is within `radius` metres of each place: where a mesh's foot is taken to stand."""
        from scipy.ndimage import minimum_filter
        if not hasattr(self, "_low") or self._low[0] != radius:
            self._low = (radius, minimum_filter(self.H, size=2 * int(round(radius / self.cell)) + 1, mode="nearest"))
        ny, nx = self.H.shape
        i = np.clip(np.rint((self.north - n) / self.cell).astype(np.int64), 0, ny - 1)
        j = np.clip(np.rint((e - self.west) / self.cell).astype(np.int64), 0, nx - 1)
        return self._low[1][i, j]


def loss_of(d: np.ndarray) -> np.ndarray:
    """The loss of points at signed distances d from the surface (the last axis), averaged."""
    return np.where(d > 0, AIR * np.minimum(d, CAP), INSIDE * np.minimum(-d, CAP)).mean(-1)


def losses(field: Field, Q: np.ndarray, C: np.ndarray, te: np.ndarray, tn: np.ndarray, lifts: np.ndarray, foot: float):
    """How badly points Q (east, north, up, already turned) fit the surface, with cameras C where they could have
    stood, when the mesh's middle is moved to each (te, tn) and its foot is at the ground there (the lowest within
    `foot` metres) plus each lift: the best lift's loss for each move, and the height its 0 is at."""
    out_l, out_z = np.empty(len(te)), np.empty(len(te))
    QC = np.r_[Q, C]
    chunk = max(1, 3_000_000 // (len(QC) * len(lifts)))
    for s in range(0, len(te), chunk):
        e, n = te[s:s + chunk, None] + QC[None, :, 0], tn[s:s + chunk, None] + QC[None, :, 1]
        base = field.ground(te[s:s + chunk], tn[s:s + chunk], foot)
        z0 = base[:, None] + lifts[None, :]                                   # [moves, lifts]
        d = field.at(e[:, None, :], n[:, None, :], QC[None, None, :, 2] + z0[:, :, None])
        loss = loss_of(d[..., :len(Q)])
        if len(C):
            loss = loss + CAMERAS * np.minimum(np.maximum(-d[..., len(Q):] - SLACK, 0), CAP).mean(-1)
        loss = loss + NEAR * (te[s:s + chunk, None] ** 2 + tn[s:s + chunk, None] ** 2) / 400
        best = loss.argmin(1)
        out_l[s:s + chunk] = loss[np.arange(len(best)), best]
        out_z[s:s + chunk] = z0[np.arange(len(best)), best]
    return out_l, out_z


def flat_loss(Q: np.ndarray, lifts: np.ndarray) -> float:
    """The loss on flat ground: what the mesh scores with no rock in the lidar to stand against."""
    return float(min(loss_of(Q[:, 2] + lift) for lift in lifts))


# ------------------------------------------------------------------------------------------------ searching
def minima_of(values: np.ndarray, k: int) -> list[int]:
    """Where a circular sequence is lowest within k either side."""
    n = len(values)
    return [y for y in range(n) if all(values[y] <= values[(y + j) % n] for j in range(-k, k + 1))]


def search(P: np.ndarray, C: np.ndarray, ground: Ground) -> dict:
    """The heading and place that fit best: every COARSE_YAW degrees and COARSE metres within REACH, then the best
    few refined to 1 degree and FINE metres."""
    radius = float(np.hypot(P[:, 0], P[:, 1]).max())
    foot = max(1.0, radius / 2)
    coarse = Field(ground, 0, 0, REACH + radius + 2, COARSE)
    Qc = spread_out(P, 0.5, 400)
    Cc = C[np.random.default_rng(0).choice(len(C), min(len(C), 60), replace=False)] if len(C) else C
    steps = np.arange(-REACH, REACH + 0.01, COARSE)
    te, tn = (a.ravel() for a in np.meshgrid(steps, steps))
    yaws = np.arange(0, 360, COARSE_YAW)
    lifts = np.arange(-LIFT + 0.5, LIFT - 0.49, 0.5)
    by_yaw, where, best_at = np.empty(len(yaws)), [], np.full(len(te), np.inf)
    for y, deg in enumerate(yaws):
        loss, _ = losses(coarse, turned(Qc, deg), turned(Cc, deg), te, tn, lifts, foot)
        i = int(loss.argmin())
        by_yaw[y], best_at = loss[i], np.minimum(best_at, loss)
        where.append((te[i], tn[i]))
    n = len(yaws)
    minima = sorted(minima_of(by_yaw, 3), key=lambda y: by_yaw[y])[:4]
    Qf = spread_out(P, 0.25, 1500, seed=1)
    fsteps = np.arange(-2, 2.01, FINE)
    fe, fn = (a.ravel() for a in np.meshgrid(fsteps, fsteps))
    flifts = np.arange(-LIFT, LIFT + 0.01, 0.25)
    best, basins = None, []
    for y in minima:
        cx, cy = where[y]
        fine = Field(ground, cx, cy, radius + 5, FINE)
        here = None
        for a in TILTS:
            for b in TILTS:
                Qt, Ct = tilted(Qf, a, b), tilted(Cc, a, b)
                for deg in np.arange(yaws[y] - 4, yaws[y] + 4.01, 1.0):
                    loss, z0 = losses(fine, turned(Qt, deg % 360), turned(Ct, deg % 360), fe + cx, fn + cy, flifts, foot)
                    i = int(loss.argmin())
                    if here is None or loss[i] < here["loss"]:
                        here = {"loss": float(loss[i]), "heading": float(deg % 360), "east": float(fe[i] + cx),
                                "north": float(fn[i] + cy), "z0": float(z0[i]), "tilt": [a, b]}
        basins.append({k: round(here[k], 3) for k in ("heading", "loss")})
        if best is None or here["loss"] < best["loss"]:
            best = here
    best["flat"] = flat_loss(Qf, flifts)
    # how clearly the best stands out: the best other heading's basin (a minimum at least APART from it), and the best
    # place at least 5 m away, each as a share of the way from the best up to a typical loss (0: as good, 1: typical)
    low, typical = float(by_yaw.min()), float(np.percentile(by_yaw, 95))
    y0 = int(by_yaw.argmin())
    turn = lambda y: np.abs((yaws[y] - yaws[y0] + 180) % 360 - 180)    # noqa: E731
    others = [y for y in minima_of(by_yaw, 3) if turn(y) >= APART]
    y2 = min(others, key=lambda y: by_yaw[y]) if others else int(np.argmax(by_yaw))
    level = by_yaw <= low + 0.1 * (typical - low)
    width = 0
    while width < n // 2 and level[(y0 + width + 1) % n] and level[(y0 - width - 1) % n]:
        width += 1
    away = np.hypot(te - best["east"], tn - best["north"]) >= 5
    typical_at = float(np.percentile(best_at, 95))
    # how much rock the lidar has where the mesh was put: its highest within the mesh's outline above its foot
    Q = turned(tilted(Qf, *best["tilt"]), best["heading"])
    under = ground.at(Q[:, 0] + best["east"], Q[:, 1] + best["north"])
    return {**best, "second": float(yaws[y2]), "margin": round((float(by_yaw[y2]) - low) / max(typical - low, 1e-9), 2),
            "within_deg": int(width * COARSE_YAW),
            "place_margin": round((float(best_at[away].min()) - low) / max(typical_at - low, 1e-9), 2),
            "lidar_rock_m": round(float(under.max() - best["z0"]), 1), "basins": basins, "by_yaw": [round(float(v), 4) for v in by_yaw]}


# ------------------------------------------------------------------------------------------------ one mesh
def climbs_of(db: Path, mid: str) -> list[dict]:
    """A climb's own mesh's climbs with their text, as the server's heading_inputs() finds them (boulders' left out)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    m = con.execute("SELECT boulder FROM meshes WHERE id=?", (mid,)).fetchone()
    if not m or m["boulder"]:
        return []
    rows = con.execute("SELECT c.id, c.name, c.data FROM mesh_links l JOIN climbs c ON c.id=l.climb_id WHERE l.mesh_id=? "
                       "AND (l.note IS NULL OR l.note NOT LIKE 'same boulder as %')", (mid,)).fetchall()
    out = []
    for r in rows:
        data = json.loads(r["data"] or "{}")
        area = (data.get("area_path") or [""])[-1] if isinstance(data.get("area_path"), list) else ""
        parts = [str(data[k]) for k in ("description", "location") if data.get(k) not in (None, "", "None")]
        out.append({"id": r["id"], "name": r["name"], "text": "\n".join(parts + ([f"In {area}."] if area else [])),
                    "own": len(rows) == 1})
    return out


def one(job: dict) -> dict:
    try:
        return place(job)
    except Exception as e:      # noqa: BLE001
        return {"id": job["mesh"]["id"], "name": job["mesh"]["name"], "skipped": f"{type(e).__name__}: {e}"}


def place(job: dict) -> dict:
    global CAMERAS, SLACK, TILTS, REACH, NEAR, LIFT
    CAMERAS, SLACK, TILTS = job.get("cameras", CAMERAS), job.get("slack", SLACK), tuple(job.get("tilts", TILTS))
    REACH, NEAR, LIFT = job.get("reach", REACH), job.get("near", NEAR), job.get("lift", LIFT)
    t0 = time.time()
    mesh_dir, m = Path(job["mesh_dir"]), job["mesh"]
    result = {"id": m["id"], "name": m["name"], "lat": m["lat"], "lng": m["lng"]}
    scale = mesh_dir / "scale.json"
    metres = json.loads(scale.read_text()).get("metres_per_unit") if scale.exists() else None
    if not metres:
        return {**result, "skipped": "no scale"}
    ground = Ground.around(Path(job["cache"]), m["lat"], m["lng"])
    if ground is None:
        return {**result, "skipped": "no lidar"}
    P, C, mid = mesh_metres(mesh_dir, metres)
    size = float(np.ptp(P[:, :2], axis=0).max())
    result.update(metres_per_unit=metres, size_m=round(size, 1), relief_m=round(float(np.ptp(np.percentile(P[:, 2], [2, 98]))), 1),
                  middle=[float(v) for v in mid])
    if size > 2 * REACH:
        return {**result, "skipped": f"{size:.0f} m across"}
    found = search(P, C, ground)
    result["cameras"] = len(C)
    result.update({k: (round(v, 3) if isinstance(v, float) else v) for k, v in found.items() if k != "by_yaw"})
    result["fit"] = round(found["loss"] / max(found["flat"], 1e-9), 3)      # below 1: a better fit than flat ground
    photos = Path(job["photos"]) if job.get("photos") else None
    compass = heading.from_compass(mesh_dir, photos) if photos else None
    text = heading.from_text(mesh_dir, job["climbs"]) if job.get("climbs") else None
    marks = {"footprint": (found["heading"], (214, 39, 40)), "compass": ((compass or {}).get("deg"), (31, 119, 180)),
             "text": ((text or {}).get("deg"), (44, 160, 44)), "skyline": (job.get("skyline"), (255, 127, 14))}
    for name, (deg, _) in list(marks.items())[1:]:
        if deg is not None:
            result[name] = round(deg, 1)
            result[f"vs_{name}"] = round(apart(found["heading"], deg), 1)
    draw(Path(job["out"]) / f"{m['id']}.png", ground, P, C, found, marks, f"{m['name']}: heading {found['heading']:.0f}, "
         f"{found['east']:+.1f} m E {found['north']:+.1f} m N, fit {result['fit']:.2f}, margin x{found['margin']}")
    result["seconds"] = round(time.time() - t0, 1)
    return result


def draw(path: Path, ground: Ground, P: np.ndarray, C: np.ndarray, found: dict, marks: dict, title: str):
    """Left: the lidar's bumps 50 m around the point (lighter is higher than the ground around), the mesh placed on
    them (red, lighter higher), its photos' cameras (cyan), its point (blue cross) and the mesh's middle (red cross).
    Right: the loss by heading."""
    from scipy.ndimage import gaussian_filter
    px, R = 4, 50
    bumps = ground.h - gaussian_filter(ground.h, 8 / ground.step)
    W = 2 * R * px
    e = (np.arange(W) + 0.5) / px - R
    E, N = np.meshgrid(e, -e)
    g = Ground(bumps, ground.e0, ground.n0, ground.step).at(E, N)
    im = Image.fromarray((np.clip((g + 2) / 8, 0, 1) * 255).astype(np.uint8)).convert("RGB")
    im = im.resize((W, W)).copy()
    canvas = Image.new("RGB", (W + 380, W + 20), "white")
    canvas.paste(im, (0, 0))
    d = ImageDraw.Draw(canvas)
    Q = turned(spread_out(P, 0.25, 3000), found["heading"])
    for x, y, t in sorted(zip(Q[:, 0] + found["east"], Q[:, 1] + found["north"], Q[:, 2]), key=lambda q: q[2]):
        u, v = (x + R) * px, (R - y) * px
        shade = int(np.clip(90 + 165 * t / max(float(Q[:, 2].max()), 1e-6), 90, 255))
        d.rectangle([u - 1, v - 1, u + 1, v + 1], fill=(shade, 40, 40))
    for x, y, _ in turned(C, found["heading"]) if len(C) else []:
        u, v = (x + found["east"] + R) * px, (R - y - found["north"]) * px
        d.rectangle([u - 1, v - 1, u + 1, v + 1], fill=(0, 190, 220))
    d.line([(R * px - 6, R * px), (R * px + 6, R * px)], fill=(30, 90, 220), width=2)
    d.line([(R * px, R * px - 6), (R * px, R * px + 6)], fill=(30, 90, 220), width=2)
    u, v = (found["east"] + R) * px, (R - found["north"]) * px
    d.line([(u - 5, v - 5), (u + 5, v + 5)], fill=(255, 60, 60), width=2)
    d.line([(u - 5, v + 5), (u + 5, v - 5)], fill=(255, 60, 60), width=2)
    d.text((4, W + 4), title, fill=(0, 0, 0))
    by = np.array(found["by_yaw"])
    x0, top_y, h = W + 10, 20, W - 60
    lo, hi = float(by.min()), float(np.percentile(by, 95))
    pts = [(x0 + k * 360 / len(by), top_y + h - (min(v, hi) - lo) / max(hi - lo, 1e-9) * h) for k, v in enumerate(by)]
    d.line(pts, fill=(60, 60, 60), width=1)
    for a in range(0, 361, 90):
        d.text((x0 + a - 4, top_y + h + 4), str(a), fill=(120, 120, 120))
    for k, (name, (deg, colour)) in enumerate(marks.items()):
        if deg is None:
            continue
        d.line([(x0 + deg, top_y), (x0 + deg, top_y + h)], fill=colour, width=2)
        d.text((x0 + deg + 3, top_y + 14 * k), f"{name} {deg:.0f}", fill=colour)
    canvas.save(path)


# ------------------------------------------------------------------------------------------------ pairs
def where_photos(mesh_dir: Path, r: dict, origin: tuple[float, float]) -> dict[str, np.ndarray]:
    """Where each of a mesh's photos was taken from, east and north in metres of origin (lat, lng), with the mesh
    placed as found."""
    kx, ky = terrain.metres_per_degree(origin[0])
    base = np.array([(r["lng"] - origin[1]) * kx + r["east"], (r["lat"] - origin[0]) * ky + r["north"]])
    h = math.radians(r["heading"])
    out = {}
    for c in json.loads((mesh_dir / "cameras.json").read_text()):
        x, _, z = -beta._rotation(c["q"]).T @ np.asarray(c["t"], float)
        ex, ny = (x - r["middle"][0]) * r["metres_per_unit"], -(z - r["middle"][1]) * r["metres_per_unit"]
        out[c["name"]] = base + np.array([ex * math.cos(h) + ny * math.sin(h), -ex * math.sin(h) + ny * math.cos(h)])
    return out


def pairs(data: Path, results: dict[str, dict]):
    """For each two meshes found that share photos: their frames' turn against their footprints', and how far apart
    the two put the photos they share."""
    names = {mid: {c["name"] for c in json.loads((data / "meshes" / mid / "cameras.json").read_text())} for mid in results}
    ids = sorted(results)
    for x, a in enumerate(ids):
        for b in ids[x + 1:]:
            if len(names[a] & names[b]) < 3:
                continue
            pair = frames_apart(data / "meshes" / a, data / "meshes" / b)
            if not pair:
                continue
            ra, rb = results[a], results[b]
            said = (ra["heading"] - rb["heading"] + 180) % 360 - 180
            origin = (ra["lat"], ra["lng"])
            pa, pb = where_photos(data / "meshes" / a, ra, origin), where_photos(data / "meshes" / b, rb, origin)
            shared = sorted(set(pa) & set(pb))
            gap = float(np.median([np.hypot(*(pa[n] - pb[n])) for n in shared]))
            pair.update(a=a, b=b, names=[ra["name"], rb["name"]], footprints_say=round(said, 1),
                        off=round(apart(said, pair["a_minus_b"]), 1), photos_apart_m=round(gap, 1),
                        fits=[ra["fit"], rb["fit"]], margins=[ra["margin"], rb["margin"]])
            yield pair


# ------------------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--index", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument("--cache", type=Path, default=ROOT / "data" / "cache" / "terrain")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--pairs", action="store_true")
    ap.add_argument("--skyline", type=Path, action="append", default=[], help="experiments/skyline results to compare with")
    ap.add_argument("--cameras", type=float, default=CAMERAS, help="weight of cameras under the ground (0: none)")
    ap.add_argument("--slack", type=float, default=SLACK, help="metres a camera may be under the ground before it counts")
    ap.add_argument("--tilt", type=float, default=0, help="also try the mesh's up this many degrees off, and half that")
    ap.add_argument("--reach", type=float, default=REACH, help="metres from its coordinates a mesh is looked for")
    ap.add_argument("--near", type=float, default=NEAR, help="loss added for each (20 m)² from its coordinates")
    ap.add_argument("--lift", type=float, default=LIFT, help="metres the foot may be off the lowest ground near it")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("meshes", nargs="*")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    db = args.data / "bouldermesh.sqlite"
    if args.index:
        index = {m["id"]: m for m in json.loads(args.index.read_text())}
    else:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        index = {r["id"]: dict(r) for r in con.execute("SELECT id, name, source, lat, lng FROM meshes WHERE replaced_by IS NULL")}
    ids = sorted(index) if args.all else args.meshes
    ids = [i for i in ids if (args.data / "meshes" / i / "scale.json").exists() and index[i].get("lat") is not None]
    skyline = {}
    for f in args.skyline:
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if r.get("heading") is not None:
                skyline[r["id"]] = r["heading"]
    print(f"{len(ids)} meshes; fetching what lidar isn't kept yet", flush=True)
    for i in ids:     # one at a time, here, so that the service is asked once for each square
        try:
            terrain.square(args.cache, index[i]["lat"], index[i]["lng"], SIDE)
        except Exception as e:      # noqa: BLE001
            print(f"{i}: no lidar ({e})", flush=True)
    jobs = []
    for i in ids:
        m = index[i]
        src = m.get("source") or ""
        jobs.append({"mesh_dir": str(args.data / "meshes" / i), "mesh": {k: m.get(k) for k in ("id", "name", "lat", "lng")},
                     "photos": str(args.data / "projects" / src.split(":", 1)[1] / "photos") if src.startswith("project:") else None,
                     "climbs": climbs_of(db, i) if db.exists() else [], "skyline": skyline.get(i),
                     "cache": str(args.cache), "out": str(args.out), "cameras": args.cameras, "slack": args.slack, "reach": args.reach, "near": args.near, "lift": args.lift,
                     "tilts": [-args.tilt, -args.tilt / 2, 0.0, args.tilt / 2, args.tilt] if args.tilt else [0.0]})
    results = {}
    with ProcessPoolExecutor(args.jobs) as pool, open(args.out / "results.jsonl", "a") as out:
        for r in pool.map(one, jobs):
            print(json.dumps({k: v for k, v in r.items() if k not in ("middle",)}), flush=True)
            out.write(json.dumps(r) + "\n")
            if "heading" in r:
                results[r["id"]] = r
    if args.pairs:
        with open(args.out / "pairs.jsonl", "a") as out:
            for pair in pairs(args.data, results):
                print(json.dumps(pair), flush=True)
                out.write(json.dumps(pair) + "\n")


if __name__ == "__main__":
    main()
