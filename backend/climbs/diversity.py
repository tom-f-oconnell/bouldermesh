"""Is this pile of internet photos diverse enough to attempt photogrammetry?

Three tiers, each used when its dependency is available:

  1. always (PIL): resolution filter + perceptual-hash near-duplicate removal.
     Duplicates are moved to photos/_rejected/ so COLMAP never sees them.
  2. OpenCV (optional): ORB features + ratio-test matching + fundamental-matrix RANSAC
     between every pair; builds a view graph.  The largest connected component of
     images with >=MIN_INLIERS geometrically-consistent matches estimates how many
     images COLMAP could register together.  Pairs with *too many* matches
     (near-identical viewpoint) are discounted so 30 crops of one photo don't count
     as 30 views.
  3. COLMAP itself (pipeline.py): the final arbiter is the number of images registered
     in the largest sparse model.

Verdicts: good (>=12 connected views), marginal (6-11), poor (<6), unknown (no OpenCV).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
MIN_SIDE = 480
DUP_HAMMING = 6
MIN_INLIERS = 40          # geometrically consistent matches for a pair to count as overlapping views
MIN_INLIER_RATIO = 0.2    # inliers / putative matches; lower = repetitive texture, not real overlap
IDENTICAL_INLIERS = 150   # above this, check whether it's just the same shot (crop / re-encode / pan-zoom)
IDENTICAL_SIM_FRACTION = 0.9  # fraction of inliers a 2-D similarity must explain for 'no parallax'


def _dhash(img, size=8) -> int:
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (px[r * (size + 1) + c] > px[r * (size + 1) + c + 1])
    return bits


def _ham(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedupe(photos_dir: Path, log: Log) -> list[Path]:
    from PIL import Image
    rejected = photos_dir.parent / "rejected"   # outside photos/ so COLMAP never sees them
    keep: list[tuple[Path, int, int]] = []     # (path, hash, pixels)
    files = sorted(p for p in photos_dir.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    for p in files:
        try:
            with Image.open(p) as im:
                w, h = im.size
                if min(w, h) < MIN_SIDE:
                    _reject(p, rejected, "small")
                    continue
                hsh = _dhash(im)
        except Exception:
            _reject(p, rejected, "unreadable")
            continue
        dup = next((k for k in keep if _ham(k[1], hsh) <= DUP_HAMMING), None)
        if dup:
            # keep the larger of the two
            if w * h > dup[2]:
                _reject(dup[0], rejected, "dup")
                keep[keep.index(dup)] = (p, hsh, w * h)
            else:
                _reject(p, rejected, "dup")
            continue
        keep.append((p, hsh, w * h))
    log(f"dedupe: {len(files)} -> {len(keep)} unique usable image(s)")
    return [k[0] for k in keep]


def _reject(p: Path, rejected: Path, why: str):
    rejected.mkdir(exist_ok=True)
    p.rename(rejected / f"{why}_{p.name}")


def view_graph(paths: list[Path], log: Log, max_images: int = 120) -> dict:
    try:
        import cv2
        import numpy as np
    except ImportError:
        log("diversity: OpenCV not installed -> skipping view-graph estimate (pip install opencv-python-headless)")
        return {"available": False}
    paths = paths[:max_images]
    orb = cv2.ORB_create(nfeatures=2500)
    feats = []
    for p in paths:
        im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if im is None:
            feats.append(None)
            continue
        s = 1024 / max(im.shape)
        if s < 1:
            im = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        kp, des = orb.detectAndCompute(im, None)
        feats.append((kp, des) if des is not None and len(kp) > 50 else None)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    n = len(paths)
    inl = np.zeros((n, n), dtype=int)
    ident = np.zeros((n, n), dtype=bool)
    for i in range(n):
        if feats[i] is None:
            continue
        for j in range(i + 1, n):
            if feats[j] is None:
                continue
            m = bf.knnMatch(feats[i][1], feats[j][1], k=2)
            good = [a for a, b in (x for x in m if len(x) == 2) if a.distance < 0.75 * b.distance]
            if len(good) < 15:
                continue
            p1 = np.float32([feats[i][0][g.queryIdx].pt for g in good])
            p2 = np.float32([feats[j][0][g.trainIdx].pt for g in good])
            F, mask = cv2.findFundamentalMat(p1, p2, cv2.FM_RANSAC, 2.0, 0.99)
            # when estimation fails OpenCV returns F=None and an *uninitialised* mask -> treat as no overlap
            if F is None or F.shape != (3, 3) or mask is None:
                continue
            mask = (mask.ravel() == 1)
            k = int(mask.sum())
            # repetitive textures produce many putative matches with a low inlier ratio -> ignore
            if k / max(1, len(good)) < MIN_INLIER_RATIO:
                k = 0
            inl[i, j] = inl[j, i] = k
            if k >= IDENTICAL_INLIERS:
                # No parallax?  If a 2-D similarity (shift/scale/rotate) explains nearly all inliers the
                # pair is a crop / resize / re-encode of one shot, or a pure pan-zoom: useless for 3-D.
                A, am = cv2.estimateAffinePartial2D(p1[mask], p2[mask], method=cv2.RANSAC, ransacReprojThreshold=3.0)
                if A is not None and am is not None and (am.ravel() == 1).mean() >= IDENTICAL_SIM_FRACTION:
                    ident[i, j] = ident[j, i] = True
    # graph of useful pairs: enough inliers but not near-identical
    useful = (inl >= MIN_INLIERS) & ~ident
    comp = _components(useful)
    largest = max(comp, key=len) if comp else []
    identical_pairs = int(ident.sum() // 2)
    return {"available": True, "images_checked": n, "largest_component": len(largest),
            "component_files": [paths[k].name for k in largest],
            "n_components_ge3": sum(1 for c in comp if len(c) >= 3),
            "useful_pairs": int(useful.sum() // 2), "near_identical_pairs": identical_pairs,
            "pairs_per_image_in_component": (float(useful[np.ix_(largest, largest)].sum() / max(1, len(largest)))
                                             if largest else 0.0)}


def _components(adj) -> list[list[int]]:
    n = adj.shape[0]
    seen, comps = set(), []
    for s in range(n):
        if s in seen:
            continue
        stack, comp = [s], []
        seen.add(s)
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in range(n):
                if adj[u, v] and v not in seen:
                    seen.add(v)
                    stack.append(v)
        comps.append(comp)
    return comps


def assess(photos_dir: Path, log: Log) -> dict:
    paths = dedupe(photos_dir, log)
    report = {"unique_images": len(paths)}
    vg = view_graph(paths, log)
    report["view_graph"] = vg
    if vg.get("available"):
        k = vg["largest_component"]
        verdict = "good" if k >= 12 else "marginal" if k >= 6 else "poor"
        report["recommend_files"] = vg["component_files"]
    else:
        k = len(paths)
        verdict = "unknown-good" if k >= 15 else "unknown-marginal" if k >= 8 else "poor"
    report["verdict"] = verdict
    report["estimated_registerable"] = k
    log(f"diversity: {len(paths)} unique, est. {k} registerable -> {verdict}")
    return report


if __name__ == "__main__":
    import sys
    print(json.dumps(assess(Path(sys.argv[1]), print), indent=1))
