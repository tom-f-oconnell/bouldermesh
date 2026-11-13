"""The size of a hold's region from a bank card in the photo: an ID-1 card (ISO/IEC 7810, 85.60 x 53.98 mm) held flat
against the rock beside the hold, in close-ups the app marks (`ref: "id1-card"` and `ref_hold`, the hold's id, in a photo's readings).

    find(image)                  the card's four corners in a photo (OpenCV), or None
    corners_3d(...)              those corners on the reconstructed surface: each corner's ray from its camera, met
                                 with the mesh (or, near enough, with the plane through the dense points about it)
    scale(corners, ...)          metres a model unit from one photo's corners: the card's two sides against their
                                 known lengths, checked for its shape (aspect, right angles, flatness)
    combine(estimates)           one region's metres a unit and its uncertainty, from every card photo of it
    mask(image shape, corners)   the card's quadrilateral (grown a little), to leave out of densifying and texturing

    card.py corners <photos> <priors.json> <out.json>   the corners in the card photos (OpenCV), with their holds
    card.py render <out dir> [--n 8] [--seed 0]   synthetic close-ups: a textured rock plane with a card on it,
                                                  rendered from known cameras, with truth.json (for the test)
    card.py test <dir>                            find the card in each, scale from it, against the truth

Geometry is numpy only; find() needs OpenCV (the app's image has it).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

W_MM, H_MM = 85.60, 53.98
ASPECT = W_MM / H_MM


def find(image: np.ndarray, min_share: float = 0.01) -> np.ndarray | None:
    """The card's corners (4 x 2 pixels, going round), in a photo as an H x W x 3 array: the largest convex
    quadrilateral among the image's edges taking at least min_share of the picture whose sides, as seen, are in
    roughly the card's proportions (perspective allows a wide margin; corners_3d() checks it properly)."""
    import cv2
    g = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    g = cv2.GaussianBlur(g, (5, 5), 0)
    best, best_area = None, 0.0
    for lo, hi in ((30, 90), (50, 150), (80, 200)):
        edges = cv2.dilate(cv2.Canny(g, lo, hi), np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_share * g.size or area <= best_area:
                continue
            quad = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
            if len(quad) != 4 or not cv2.isContourConvex(quad):
                continue
            q = quad.reshape(4, 2).astype(float)
            sides = np.linalg.norm(q - np.roll(q, -1, axis=0), axis=1)
            seen = max(sides[0] + sides[2], sides[1] + sides[3]) / min(sides[0] + sides[2], sides[1] + sides[3])
            if 1.1 < seen < 2.6:
                best, best_area = q, area
    if best is None:
        return None
    import cv2 as _cv
    crit = (_cv.TERM_CRITERIA_EPS + _cv.TERM_CRITERIA_MAX_ITER, 40, 0.01)
    return _cv.cornerSubPix(g, best.astype(np.float32).reshape(-1, 1, 2), (5, 5), (-1, -1), crit).reshape(4, 2).astype(float)


def rays(corners: np.ndarray, K: np.ndarray, R: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each corner's ray in the model: the camera's centre, and unit directions (R, t world to camera; K pinhole)."""
    c = -R.T @ t
    px = np.c_[corners, np.ones(len(corners))]
    d = (R.T @ (np.linalg.inv(K) @ px.T)).T
    return c, d / np.linalg.norm(d, axis=1, keepdims=True)


def meet_plane(origin: np.ndarray, dirs: np.ndarray, point: np.ndarray, normal: np.ndarray) -> np.ndarray:
    """Where rays from origin meet the plane through point with normal."""
    n = normal / np.linalg.norm(normal)
    s = ((point - origin) @ n) / (dirs @ n)
    return origin + dirs * s[:, None]


def fit_plane(P: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """The plane nearest points (centre, unit normal) and their RMS distance from it."""
    c = P.mean(axis=0)
    _, sv, Vt = np.linalg.svd(P - c)
    return c, Vt[2], float(sv[2] / math.sqrt(len(P)))


def scale(X: np.ndarray, max_aspect_err: float = 0.06, max_angle_err_deg: float = 6.0) -> dict:
    """Metres a model unit from a card's four corners on the surface (in the model, going round), with how well they
    look like a card: the two long sides and two short ones against 85.60 and 53.98 mm, their aspect, the corners'
    angles, and how flat the four lie. ok is False when the shape is too far from a card's to trust."""
    sides = np.linalg.norm(X - np.roll(X, -1, axis=0), axis=1)
    if sides[0] + sides[2] < sides[1] + sides[3]:       # make sides 0 and 2 the long ones
        X = np.roll(X, -1, axis=0)
        sides = np.roll(sides, -1)
    long_, short = (sides[0] + sides[2]) / 2, (sides[1] + sides[3]) / 2
    angles = []
    for i in range(4):
        a, b = X[i - 1] - X[i], X[(i + 1) % 4] - X[i]
        angles.append(math.degrees(math.acos(np.clip(a @ b / np.linalg.norm(a) / np.linalg.norm(b), -1, 1))))
    _, _, flat = fit_plane(X)
    per = [W_MM / 1000 / sides[0], H_MM / 1000 / sides[1], W_MM / 1000 / sides[2], H_MM / 1000 / sides[3]]
    mpu = float(np.mean(per))
    aspect_err = abs(long_ / short / ASPECT - 1)
    angle_err = max(abs(a - 90) for a in angles)
    return {"metres_per_unit": mpu, "spread": float(np.std(per) / mpu), "aspect_err": round(float(aspect_err), 4),
            "angle_err_deg": round(float(angle_err), 2), "flat_mm": round(flat * mpu * 1000, 3),
            "ok": bool(aspect_err <= max_aspect_err and angle_err <= max_angle_err_deg)}


def combine(estimates: list[dict]) -> dict | None:
    """One region's metres a unit from its card photos' estimates (those ok), and its uncertainty: the standard error
    of their mean, and never less than each one's own side-to-side spread allows."""
    good = [e for e in estimates if e.get("ok")]
    if not good:
        return None
    v = np.array([e["metres_per_unit"] for e in good])
    own = np.median([e["spread"] for e in good]) * v.mean() / math.sqrt(4 * len(good))
    between = v.std(ddof=1) / math.sqrt(len(v)) if len(v) > 1 else 0.0
    err = float(max(own, between))
    return {"metres_per_unit": float(v.mean()), "err": err, "rel_err": err / float(v.mean()), "photos": len(good),
            "refused": len(estimates) - len(good)}


def mask(shape: tuple[int, int], corners: np.ndarray, grow: float = 0.08) -> np.ndarray:
    """The card's quadrilateral grown by `grow` of its size about its middle, as a bool array of the photo's size:
    where the card is (a flat print, not rock), to leave out of densifying and texturing."""
    h, w = shape
    m = corners.mean(axis=0)
    q = m + (corners - m) * (1 + grow)
    yy, xx = np.mgrid[0:h, 0:w]
    inside = np.ones((h, w), bool)
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        cross = (b[0] - a[0]) * (yy - a[1]) - (b[1] - a[1]) * (xx - a[0])
        inside &= cross >= 0
    if not inside.any():        # the other way round
        inside = np.ones((h, w), bool)
        for i in range(4):
            a, b = q[i], q[(i + 1) % 4]
            inside &= (b[0] - a[0]) * (yy - a[1]) - (b[1] - a[1]) * (xx - a[0]) <= 0
    return inside


# ---------------------------------------------------------------- synthetic close-ups, for the test

def render(out: Path, n: int = 8, seed: int = 0, size=(1600, 1200), f: float = 1500.0, mpu: float = 0.02) -> None:
    """Close-ups of a rock plane (y = 0 in the model, its texture made-up noise) with a card on it, from n cameras
    0.12-0.25 m away (6-12 units at mpu metres a unit), turned up to 35 degrees off square. truth.json has each
    photo's camera, the card's corners in the model, and mpu."""
    from PIL import Image
    rng = np.random.default_rng(seed)
    out.mkdir(parents=True, exist_ok=True)
    w, h = size
    tex = rng.random((512, 512))
    for _ in range(3):      # smooth it into blotches, plus fine grain
        tex = (tex + np.roll(tex, 1, 0) + np.roll(tex, 1, 1) + np.roll(tex, -1, 0) + np.roll(tex, -1, 1)) / 5
    tex = 0.35 + 0.3 * (tex - tex.min()) / (np.ptp(tex) or 1) + 0.05 * rng.random((512, 512))
    tex_size = 0.4 / mpu        # the texture covers 40 cm of rock
    cw, ch = W_MM / 1000 / mpu, H_MM / 1000 / mpu
    centre = np.array([0.02 / mpu, 0.0, -0.01 / mpu])
    card = np.array([centre + [-cw / 2, 0, -ch / 2], centre + [cw / 2, 0, -ch / 2], centre + [cw / 2, 0, ch / 2], centre + [-cw / 2, 0, ch / 2]])
    truth = {"mpu": mpu, "card": card.tolist(), "photos": {}}
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])
    for i in range(n):
        dist = rng.uniform(0.12, 0.25) / mpu
        tilt = math.radians(rng.uniform(0, 35))
        az = rng.uniform(0, 2 * math.pi)
        look_at = centre + np.array([rng.uniform(-1, 1) * cw * 0.3, 0, rng.uniform(-1, 1) * ch * 0.3])
        c = look_at + dist * np.array([math.sin(tilt) * math.cos(az), math.cos(tilt), math.sin(tilt) * math.sin(az)])
        fwd = (look_at - c) / np.linalg.norm(look_at - c)
        right = np.cross(fwd, [0.0, 0.0, 1.0])
        right /= np.linalg.norm(right)
        down = np.cross(fwd, right)
        R = np.stack([right, down, fwd])        # world to camera
        t = -R @ c
        yy, xx = np.mgrid[0:h, 0:w].astype(float)
        d = (R.T @ (np.linalg.inv(K) @ np.stack([xx.ravel(), yy.ravel(), np.ones(xx.size)]))).T
        s = -c[1] / d[:, 1]
        X = c + d * s[:, None]
        u = ((X[:, 0] / tex_size + 0.5) * 511).clip(0, 511).astype(int)
        v = ((X[:, 2] / tex_size + 0.5) * 511).clip(0, 511).astype(int)
        val = tex[v, u]
        on = (np.abs(X[:, 0] - centre[0]) <= cw / 2) & (np.abs(X[:, 2] - centre[2]) <= ch / 2)
        img = np.stack([val, val * 0.9, val * 0.8], axis=1)
        img[on] = [0.93, 0.93, 0.95]       # a pale card
        stripe = on & (np.abs(X[:, 2] - (centre[2] - ch * 0.25)) < ch * 0.08)
        img[stripe] = [0.15, 0.2, 0.5]
        img = (img.reshape(h, w, 3) * 255 + rng.normal(0, 2, (h, w, 3))).clip(0, 255).astype(np.uint8)
        name = f"card_{i:02d}.jpg"
        Image.fromarray(img).save(out / name, quality=95)
        truth["photos"][name] = {"K": K.tolist(), "R": R.tolist(), "t": t.tolist()}
    (out / "truth.json").write_text(json.dumps(truth, indent=1))


def test(d: Path) -> dict:
    """Finds the card in each rendered photo, puts its corners on the rock plane, and compares the scale with the truth."""
    from PIL import Image
    truth = json.loads((d / "truth.json").read_text())
    ests = []
    for name, cam in truth["photos"].items():
        img = np.asarray(Image.open(d / name).convert("RGB"))
        q = find(img)
        if q is None:
            ests.append({"photo": name, "found": False, "ok": False})
            continue
        o, dirs = rays(q, np.array(cam["K"]), np.array(cam["R"]), np.array(cam["t"]))
        X = meet_plane(o, dirs, np.zeros(3), np.array([0.0, 1.0, 0.0]))
        e = scale(X)
        e.update(photo=name, found=True, rel_err_vs_truth=round(e["metres_per_unit"] / truth["mpu"] - 1, 5))
        ests.append(e)
    c = combine(ests)
    out = {"photos": len(ests), "found": sum(1 for e in ests if e.get("found")), "estimates": ests, "combined": c}
    if c:
        out["combined_err_vs_truth"] = round(c["metres_per_unit"] / truth["mpu"] - 1, 5)
    return out


def corners_of(photos: Path, priors: Path, out: Path) -> dict:
    """The card's corners in each capture photo its readings mark (`ref: "card"`), as {name: {photo, corners}}: for
    core.py's detail step, which puts them on the region's surface. Runs where OpenCV is (the app's image)."""
    from PIL import Image
    pri = json.loads(priors.read_text())["photos"]
    found = {}
    for name, p in pri.items():
        if not p.get("card") or not (photos / name).exists():
            continue
        q = find(np.asarray(Image.open(photos / name).convert("RGB")))
        if q is not None:
            found[name] = {"photo": name, "corners": q.tolist(), "hold": p.get("ref_hold")}
    out.write_text(json.dumps(found))
    return found


if __name__ == "__main__":
    if sys.argv[1] == "corners":
        print(len(corners_of(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))), "cards found")
    elif sys.argv[1] == "render":
        render(Path(sys.argv[2]), int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 8,
               int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 0)
    elif sys.argv[1] == "test":
        print(json.dumps(test(Path(sys.argv[2])), indent=1))
