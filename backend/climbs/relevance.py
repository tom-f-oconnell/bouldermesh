"""Keep only photos that are substantially of the boulder of interest.

Searching an area returns trucks, ravens, signs, valley vistas and other boulders, and video
frames include title cards. Three methods, each used when its dependency is available:

  cards      (Pillow + numpy) title cards and blank frames: mostly flat once black bars are
             cropped off.
  boulder    (torch + open_clip, optional) CLIP zero-shot classification: does this look like
             a boulder at all? Very safe, but can't tell one boulder from another.
  same-rock  (OpenCV) does this show the same physical rock? SIFT + fundamental-matrix RANSAC
             against anchor photos, by default those from the climb's own page and beta videos,
             then against the photos that matched those. Very precise, but photos in other
             light or from other sides don't match. Only photos from area-wide sources (see
             UNTRUSTED) have to pass it.

Rejected photos move to rejected/ like de-duplication's, and can be restored from there.
design.md has the measurements. This mostly saves time: COLMAP ignores most junk by itself.
"""
from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .common import CACHE_DIR

Log = Callable[[str], None]
Progress = Callable[..., None]   # progress(fraction, detail)

UNTRUSTED = ("commons", "flickr", "google", "ddg", "web")   # file name prefixes of area-wide / search sources
REASONS = {"card": "title card or blank frame", "notboulder": "doesn't look like a boulder",
           "nomatch": "doesn't match known photos of this boulder"}
FLAT_MAX = 0.5            # share of an image that may be flat
BOULDER_MIN_P = 0.5       # CLIP probability of the boulder prompts, against the others
MIN_INLIERS = 16          # same-rock: geometrically consistent SIFT matches to any anchor
SELF_MIN_INLIERS = 20     # stricter when the anchors must come from the candidates themselves
MIN_COVERAGE = 0.05       # share of the image the matched region must span
MAX_ANCHORS = 120
CHAIN_ROUNDS = 3          # how far confirmed photos pass that on to the photos they match
SIFT_FEATURES = 2500
SIFT_SIDE = 1024

CLIP_MODEL, CLIP_WEIGHTS = "ViT-B-32", "laion2b_s34b_b79k"
BOULDER_PROMPTS = ["a photo of a large boulder", "a rock climber on a large boulder",
                   "people bouldering on a big rock outdoors", "a close-up photo of a granite rock face with a climber"]
OTHER_PROMPTS = ["a photo of a car or truck", "a photo of a bird or wild animal", "a sign or notice board",
                 "a tent in a campground", "a landscape of a mountain valley", "a huge granite cliff under a blue sky",
                 "a waterfall or river", "a forest of trees", "an indoor climbing gym with colourful holds",
                 "a poster, flyer or page of text", "a topo map or diagram", "a portrait of a person's face",
                 "a climber on a high multi-pitch big wall with a rope", "a pile of many small rocks",
                 "a black screen with white title text"]
_clip = None


def available() -> list[str]:
    """The methods this install can run."""
    have = lambda m: importlib.util.find_spec(m) is not None  # noqa: E731
    out = []
    if have("numpy") and have("PIL"):
        out.append("cards")
    if have("torch") and have("open_clip"):
        out.append("boulder")
    if have("cv2") and have("numpy"):
        out.append("same-rock")
    return out


def is_trusted(path: Path) -> bool:
    return not path.name.startswith(UNTRUSTED)


# ------------------------------------------------------------------------------ cards
def flat_share(path: Path) -> float:
    """Share of the image, without any black bars, that has no texture at all."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("L")
        im.thumbnail((320, 320))
        a = np.asarray(im, dtype=float)
    bright = a > 16
    ys, xs = np.where(bright.mean(axis=1) > 0.02)[0], np.where(bright.mean(axis=0) > 0.02)[0]
    if len(ys) >= 8 and len(xs) >= 8:
        a = a[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]
    t = 16
    tiles = [a[y:y + t, x:x + t].std() for y in range(0, a.shape[0] - t + 1, t) for x in range(0, a.shape[1] - t + 1, t)]
    return float(np.mean(np.array(tiles) < 4)) if tiles else 1.0


# ------------------------------------------------------------------------------ boulder
def _load_clip(log: Log):
    global _clip
    if _clip is None:
        import open_clip
        import torch
        cache = CACHE_DIR.parent / "models"
        log(f"relevance: loading CLIP {CLIP_MODEL} (a ~350 MB download the first time, kept in {cache})")
        model, _, prep = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=CLIP_WEIGHTS, cache_dir=str(cache))
        model.eval()
        with torch.no_grad():
            text = model.encode_text(open_clip.get_tokenizer(CLIP_MODEL)(BOULDER_PROMPTS + OTHER_PROMPTS))
            text /= text.norm(dim=-1, keepdim=True)
        _clip = (model, prep, text)
    return _clip


def boulder_probability(paths: list[Path], log: Log, progress: Progress | None = None) -> dict[Path, float]:
    """CLIP's probability, for each photo, that one of the boulder prompts fits it best."""
    import torch
    from PIL import Image
    model, prep, text = _load_clip(log)
    out = {}
    for i, p in enumerate(paths):
        if progress:
            progress(i / max(1, len(paths)), f"{i} of {len(paths)} photos")
        with torch.no_grad(), Image.open(p) as im:
            v = model.encode_image(prep(im.convert("RGB")).unsqueeze(0))
            v /= v.norm(dim=-1, keepdim=True)
            probs = (100 * v @ text.T).softmax(dim=-1)[0]
        out[p] = float(probs[:len(BOULDER_PROMPTS)].sum())
    return out


# ------------------------------------------------------------------------------ same rock
def _features(path: Path):
    """RootSIFT keypoints: (points, descriptors, image shape), or None."""
    import cv2
    import numpy as np
    im = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if im is None:
        return None
    s = SIFT_SIDE / max(im.shape)
    if s < 1:
        im = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    kp, des = cv2.SIFT_create(nfeatures=SIFT_FEATURES).detectAndCompute(im, None)
    if des is None or len(kp) < 8:
        return None
    des = np.sqrt(des / (np.abs(des).sum(axis=1, keepdims=True) + 1e-7))
    return np.float32([k.pt for k in kp]), des.astype(np.float32), im.shape


def match(a, b) -> tuple[int, float]:
    """Geometrically consistent matches between two _features(), and the share of a's image they span.

    Matches must be mutual, and fit one fundamental matrix to within a pixel. Without those two
    checks a flyer or a climbing gym can score 50 inliers against rock, from many of its points
    all matching the same handful of points on the rock.
    """
    import cv2
    import numpy as np
    bf = cv2.BFMatcher(cv2.NORM_L2)
    good = [x for x, y in (t for t in bf.knnMatch(a[1], b[1], k=2) if len(t) == 2) if x.distance < 0.8 * y.distance]
    back = {m.queryIdx: m.trainIdx for m in bf.match(b[1], a[1])}
    good = [g for g in good if back.get(g.trainIdx) == g.queryIdx]
    if len(good) < 12:
        return 0, 0.0
    p1 = a[0][[g.queryIdx for g in good]]
    p2 = b[0][[g.trainIdx for g in good]]
    F, mask = cv2.findFundamentalMat(p1, p2, cv2.FM_RANSAC, 1.0, 0.999, 5000)
    # OpenCV can return F=None with an uninitialised mask, or several stacked solutions
    if F is None or F.shape != (3, 3) or mask is None:
        return 0, 0.0
    mask = mask.ravel() == 1
    k = int(mask.sum())
    if k < 8:
        return 0, 0.0
    hull = cv2.convexHull(p1[mask].reshape(-1, 1, 2))
    return k, float(cv2.contourArea(hull) / (a[2][0] * a[2][1]))


def _threads() -> int:
    return min(12, max(1, (os.cpu_count() or 2) - 1))


def _map(fn, items: list, progress: Progress | None, what: str) -> list:
    """fn over items in threads (OpenCV releases the GIL), reporting progress."""
    import cv2
    threads_before = cv2.getNumThreads()
    cv2.setNumThreads(1)
    out = []
    try:
        with ThreadPoolExecutor(_threads()) as ex:
            for r in ex.map(fn, items):
                out.append(r)
                if progress:
                    progress(len(out) / max(1, len(items)), f"{what}: {len(out)} of {len(items)}")
    finally:
        cv2.setNumThreads(threads_before)
    return out


def _spaced(items: list[Path], n: int) -> list[Path]:
    """At most n of the anchors: all the stills, then video frames evenly spread, as neighbouring
    frames are near-duplicates while a single still can be what a photo from that side matches."""
    if len(items) <= n:
        return items
    frames = [p for p in items if p.name.startswith("yt_")]
    stills = [p for p in items if not p.name.startswith("yt_")][:n]
    room = n - len(stills)
    return stills + [frames[int(i * len(frames) / room)] for i in range(room)]


def _self_anchors(paths: list[Path], feats: dict, log: Log, progress: Progress | None) -> list[Path]:
    """With no trusted photos: the largest group of photos that match each other."""
    usable = [p for p in paths if feats[p] is not None]
    pairs = [(i, j) for i in range(len(usable)) for j in range(i + 1, len(usable))]
    link = _map(lambda ij: match(feats[usable[ij[0]]], feats[usable[ij[1]]])[0] >= SELF_MIN_INLIERS, pairs, progress, "pairs")
    parent = list(range(len(usable)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for (i, j), linked in zip(pairs, link):
        if linked:
            parent[root(i)] = root(j)
    groups: dict[int, list[Path]] = {}
    for i, p in enumerate(usable):
        groups.setdefault(root(i), []).append(p)
    best = max(groups.values(), key=len, default=[])
    return best if len(best) >= 3 else []


def same_rock(candidates: list[Path], anchors: list[Path], log: Log, progress: Progress | None = None) -> dict[Path, dict]:
    """For each candidate, its best match found: {"inliers":, "coverage":, "match":}.

    Without anchors the candidates' own largest matching group stands in for them. Returns {}
    if there is nothing to anchor on, as then nothing can be said.
    """
    anchors = _spaced(anchors, MAX_ANCHORS)
    everything = list(dict.fromkeys(anchors + candidates))
    done = _map(_features, everything, (lambda f, d=None: progress(0.2 * f, d)) if progress else None, "reading photos")
    feats = dict(zip(everything, done))
    pair_progress = (lambda f, d=None: progress(0.2 + 0.8 * f, d)) if progress else None
    self_anchored = not anchors
    if self_anchored:
        anchors = _self_anchors(candidates, feats, log, pair_progress)
        if not anchors:
            log("relevance: no anchors, and no group of photos matches each other: can't tell which rock is which")
            return {}
        log(f"relevance: no photos from the climb's page or videos: using the {len(anchors)} that match each other")
    min_inliers = SELF_MIN_INLIERS if self_anchored else MIN_INLIERS

    def passes(result: dict) -> bool:
        return result["inliers"] >= min_inliers and result["coverage"] >= MIN_COVERAGE

    results = {p: {"inliers": 0, "coverage": 0.0, "match": None} for p in candidates}
    frontier = [(a, feats[a]) for a in anchors if feats[a] is not None]
    todo = [p for p in candidates if feats[p] is not None]
    for p in todo:
        if p in anchors:
            results[p] = {"inliers": 10 ** 6, "coverage": 1.0, "match": p.name}
    todo = [p for p in todo if p not in anchors]

    def best_for(p: Path) -> dict:
        best = results[p]
        for a, fa in frontier:
            k, cov = match(feats[p], fa)
            if k > best["inliers"]:
                best = {"inliers": k, "coverage": cov, "match": a.name}
            if passes(best):   # enough: no need for the rest
                break
        return best

    # a photo often overlaps another web photo better than any anchor, so those confirmed so far
    # become anchors in turn. Safe because noise stays well below MIN_INLIERS (see design.md).
    for _ in range(CHAIN_ROUNDS):
        results.update(zip(todo, _map(best_for, todo, pair_progress, "matching")))
        frontier = [(p, feats[p]) for p in todo if passes(results[p])]
        todo = [p for p in todo if not passes(results[p])]
        if not frontier or not todo:
            break
    return results


# ------------------------------------------------------------------------------ all together
def filter_photos(paths: list[Path], rejected: Path | None, log: Log, methods: list[str] | None = None,
                  anchors: list[Path] | None = None, progress: Progress | None = None) -> dict:
    """Filter photos, moving the rejected ones to `rejected` (None: only report).

    Args:
        paths: the (de-duplicated) photos.
        rejected: where rejected photos go, as <reason>_<name>.
        methods: which of available() to run; default all.
        anchors: photos known to show the boulder; default those from trusted sources.
        progress: progress(fraction, detail).

    Returns:
        A JSON-able report: methods run, kept file names, rejected {name: reason}, per-photo scores.
    """
    can = available()
    methods = [m for m in (methods if methods is not None else can) if m in can]
    missing = [m for m in ("cards", "boulder", "same-rock") if m not in can]
    log(f"relevance: checking {len(paths)} photo(s) with {', '.join(methods) or 'nothing'}"
        + (f" ({', '.join(missing)} not installed)" if missing else ""))
    scores: dict[str, dict] = {p.name: {} for p in paths}
    why: dict[str, str] = {}
    alive = list(paths)

    def sub(lo: float, hi: float) -> Progress | None:
        return (lambda f=0.0, d=None: progress(lo + (hi - lo) * f, d)) if progress else None

    if "cards" in methods:
        for p in alive:
            scores[p.name]["flat"] = round(flat_share(p), 3)
            if scores[p.name]["flat"] > FLAT_MAX:
                why[p.name] = "card"
        alive = [p for p in alive if p.name not in why]
    if "boulder" in methods and alive:
        try:
            for p, prob in boulder_probability(alive, log, sub(0.0, 0.3)).items():
                scores[p.name]["boulder"] = round(prob, 3)
                if prob < BOULDER_MIN_P:
                    why[p.name] = "notboulder"
            alive = [p for p in alive if p.name not in why]
        except Exception as e:  # e.g. the model can't be downloaded: only costs this method
            log(f"relevance: CLIP check failed ({e}); skipping it")
    if "same-rock" in methods and alive:
        anchor_list = [p for p in (anchors if anchors is not None else alive) if p in alive and (anchors is not None or is_trusted(p))]
        candidates = [p for p in alive if p not in anchor_list]
        if candidates:
            for p, r in same_rock(candidates, anchor_list, log, sub(0.3, 1.0)).items():
                scores[p.name].update(inliers=r["inliers"], coverage=round(r["coverage"], 3), match=r["match"])
                if r["inliers"] < (MIN_INLIERS if anchor_list else SELF_MIN_INLIERS) or r["coverage"] < MIN_COVERAGE:
                    why[p.name] = "nomatch"
    if paths and len(why) == len(paths):
        # more likely a false alarm than a whole harvest of junk: leave it for the diversity check to judge
        log("relevance: every photo would be rejected: keeping them all")
        why = {}
    if rejected is not None:
        for p in paths:
            if p.name in why:
                rejected.mkdir(exist_ok=True)
                p.rename(rejected / f"{why[p.name]}_{p.name}")
    counts = {r: sum(1 for w in why.values() if w == r) for r in REASONS}
    log(f"relevance: kept {len(paths) - len(why)} of {len(paths)}"
        + "".join(f", {n} rejected: {REASONS[r]}" for r, n in counts.items() if n))
    return {"methods": methods, "kept": [p.name for p in paths if p.name not in why], "rejected": why, "counts": counts,
            "scores": scores}
