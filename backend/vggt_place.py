"""Placing photos with VGGT when COLMAP's mapper can't (design.md, "When COLMAP can't place the photos").

COLMAP's mapper grows a model from a first pair of photos seen from far enough apart, and gives up on climbs whose photos
were all taken from about the same place. VGGT (Meta's Visual Geometry Grounded Transformer, the 1B model) estimates
every photo's camera at once, from the photos alone. Its placements are a few degrees off, so they only start COLMAP off:
COLMAP triangulates the matches it already verified from them, then refines placements, points and lenses together
(bundle adjustment), with tolerances tightening over three rounds. The pipeline carries on from the model that leaves,
as it would from the mapper's.

Needs a CUDA build of torch with room on the GPU, the vggt package, and 4.7 GB of weights, downloaded into the models
cache the first time. VGGT_CPU=1 lets it run on the CPU instead (minutes rather than seconds); VGGT_FALLBACK=0 turns it
off. The weights are licensed for non-commercial use only.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import statistics
import struct
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]

REPO_ID = "facebook/VGGT-1B"
MAX_PHOTOS = 80        # VGGT's time grows with the square of the photos, as each attends to every other
MIN_PHOTOS = 3
MIN_INLIERS = 15       # a verified pair with fewer matches doesn't link two photos (COLMAP's own minimum)
# GPU memory for its camera head alone, measured on hal's 3090: 7.3 GB for 10 photos, 12.1 GB for 60, 15.0 GB for 90
GPU_BASE_MB, GPU_PER_PHOTO_MB, GPU_SPARE_MB = 6300, 100, 1000
# Rounds of (largest angle error in degrees, largest reprojection error in pixels, whether focal lengths are refined)
# for triangulating, keeping points and adjusting. The first takes points from placements a few degrees off; each
# later one, from better ones. experiments/depth/test3.py compares the choices.
ROUNDS = ((8, 150, 1), (3, 20, 1), (2, 4, 1))
MAX_IMAGE_ID = 2147483647  # COLMAP's pair_id is this times the smaller image id, plus the larger


class Unavailable(Exception):
    """VGGT can't run here: why."""


def enabled() -> bool:
    return os.environ.get("VGGT_FALLBACK", "1") != "0"


def unavailable() -> str | None:
    """Why VGGT can't place photos here, or None if it can."""
    if not enabled():
        return "VGGT_FALLBACK=0"
    for m in ("torch", "vggt", "huggingface_hub", "safetensors", "einops"):
        if importlib.util.find_spec(m) is None:
            return f"{m} isn't installed"
    import torch
    if not torch.cuda.is_available() and os.environ.get("VGGT_CPU") != "1":
        return "no CUDA GPU is visible (VGGT_CPU=1 runs it on the CPU, slowly)"
    return None


def capacity() -> tuple[str, int]:
    """Where it runs, and how many photos fit there now."""
    import torch
    if not torch.cuda.is_available():
        return "cpu", MAX_PHOTOS
    free = torch.cuda.mem_get_info()[0] // 2 ** 20
    fits = (free - GPU_BASE_MB - GPU_SPARE_MB) // GPU_PER_PHOTO_MB
    if fits < MIN_PHOTOS:
        raise Unavailable(f"only {free} MB of GPU memory is free, and it needs {GPU_BASE_MB + GPU_SPARE_MB + MIN_PHOTOS * GPU_PER_PHOTO_MB}")
    return "cuda", int(min(MAX_PHOTOS, fits))


def choose(db: Path, limit: int = MAX_PHOTOS) -> list[dict]:
    """The photos worth placing: the largest group linked by COLMAP's verified matches, as the others can't be
    triangulated with it, and of more than limit, those best linked to the rest.

    Returns:
        [{id, name, camera, width, height}], in the database's order.
    """
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        images = {i: (name, cam) for i, name, cam in con.execute("SELECT image_id, name, camera_id FROM images")}
        sizes = {c: (w, h) for c, w, h in con.execute("SELECT camera_id, width, height FROM cameras")}
        links: dict[int, dict[int, int]] = defaultdict(dict)
        for pair, rows in con.execute("SELECT pair_id, rows FROM two_view_geometries WHERE rows >= ?", (MIN_INLIERS,)):
            a, b = divmod(pair, MAX_IMAGE_ID)
            if a in images and b in images:
                links[a][b] = links[b][a] = rows
    finally:
        con.close()
    groups, seen = [], set()
    for start in links:
        if start in seen:
            continue
        group, todo = set(), [start]
        while todo:
            i = todo.pop()
            if i not in group:
                group.add(i)
                todo.extend(links[i])
        seen |= group
        groups.append(group)
    if not groups:
        return []
    group = max(groups, key=lambda g: (len(g), sum(sum(links[i].values()) for i in g)))
    if len(group) > limit:      # grown from the best-linked photo, each time by the one most linked to those taken
        last = max(group, key=lambda i: sum(links[i].values()))
        taken, pull = {last}, defaultdict(int)
        while len(taken) < limit:
            for j, n in links[last].items():
                if j not in taken:
                    pull[j] += n
            last = max(pull, key=pull.get)
            taken.add(last)
            del pull[last]
        group = taken
    return [{"id": i, "name": images[i][0], "camera": images[i][1], "width": sizes[images[i][1]][0],
             "height": sizes[images[i][1]][1]} for i in sorted(group)]


_model = None


def predict(paths: list[Path], device: str, log: Log) -> dict[str, tuple]:
    """VGGT's camera for each photo: {name: (world-to-camera rotation, translation, focal length in the photo's own
    pixels)}. Its model is let go afterwards, so it holds no memory between reconstructions."""
    global _model
    import torch
    from PIL import Image
    from climbs.common import CACHE_DIR
    from vggt.models.vggt import VGGT
    from vggt.utils.load_fn import load_and_preprocess_images
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri
    try:
        if _model is None:
            log("VGGT: loading its model (4.7 GB, downloaded the first time)")
            _model = VGGT.from_pretrained(REPO_ID, cache_dir=str(CACHE_DIR.parent / "models" / "huggingface"),
                                          enable_point=False, enable_depth=False, enable_track=False).eval()
        model = _model.to(device)
        imgs = load_and_preprocess_images([str(p) for p in paths]).to(device)
        half = torch.bfloat16 if device == "cuda" and torch.cuda.get_device_capability()[0] >= 8 else torch.float16
        with torch.no_grad(), torch.autocast(device_type="cuda", dtype=half, enabled=device == "cuda"):
            pred = model(imgs)
        ext, intr = pose_encoding_to_extri_intri(pred["pose_enc"], imgs.shape[-2:])
        ext, intr = ext[0].float().cpu().numpy(), intr[0].float().cpu().numpy()
    finally:
        _model = None
        if device == "cuda":
            torch.cuda.empty_cache()
    out = {}
    for p, e, K in zip(paths, ext, intr):
        w, h = Image.open(p).size
        out[p.name] = (e[:3, :3].tolist(), e[:3, 3].tolist(), focal(K, w, h))
    return out


def focal(K, width: int, height: int) -> float:
    """A focal length in the photo's own pixels, from VGGT's camera matrix for it as VGGT loads photos: 518 wide, its
    height in proportion to the nearest 14, then cropped or padded."""
    scaled = round(height * 518 / width / 14) * 14
    return float((K[0][0] * width / 518 + K[1][1] * height / scaled) / 2)


def quaternion(R) -> tuple[float, float, float, float]:
    """COLMAP's w, x, y, z for a rotation matrix."""
    r = [[float(R[i][j]) for j in range(3)] for i in range(3)]
    w = math.sqrt(max(0.0, 1 + r[0][0] + r[1][1] + r[2][2])) / 2
    x = math.copysign(math.sqrt(max(0.0, 1 + r[0][0] - r[1][1] - r[2][2])) / 2, r[2][1] - r[1][2])
    y = math.copysign(math.sqrt(max(0.0, 1 - r[0][0] + r[1][1] - r[2][2])) / 2, r[0][2] - r[2][0])
    z = math.copysign(math.sqrt(max(0.0, 1 - r[0][0] - r[1][1] + r[2][2])) / 2, r[1][0] - r[0][1])
    n = math.sqrt(w * w + x * x + y * y + z * z)
    return w / n, x / n, y / n, z / n


def write_model(path: Path, chosen: list[dict], placed: dict[str, tuple]) -> None:
    """A COLMAP text model of the chosen photos as VGGT placed them, with no points. Photos sharing a lens in the
    database (single-camera projects) share the middle of VGGT's focal lengths for them."""
    path.mkdir(parents=True, exist_ok=True)
    by_camera = defaultdict(list)
    for c in chosen:
        by_camera[c["camera"]].append(c)
    cams = []
    for cam, cs in sorted(by_camera.items()):
        f = statistics.median(placed[c["name"]][2] for c in cs)
        w, h = cs[0]["width"], cs[0]["height"]
        cams.append(f"{cam} SIMPLE_RADIAL {w} {h} {f:.4f} {w / 2} {h / 2} 0")
    ims = []
    for c in chosen:
        R, t, _ = placed[c["name"]]
        q = " ".join(f"{v:.12f}" for v in quaternion(R))
        ims.append(f"{c['id']} {q} {' '.join(f'{v:.12f}' for v in t)} {c['camera']} {c['name']}\n")   # no points on the 2nd line
    (path / "cameras.txt").write_text("\n".join(cams) + "\n")
    (path / "images.txt").write_text("\n".join(ims) + "\n")
    (path / "points3D.txt").write_text("")


def count(model: Path, what: str) -> int:
    """How many images or points3D a binary COLMAP model holds."""
    try:
        with open(model / f"{what}.bin", "rb") as f:
            return struct.unpack("<Q", f.read(8))[0]
    except (OSError, struct.error):
        return 0


def place(colmap: str, db: Path, images: Path, out: Path, work: Path, log: Log, run: Callable[[list[str]], None],
          rounds=ROUNDS) -> int:
    """Places the photos COLMAP matched, with VGGT and then COLMAP, as a model in out.

    Args:
        colmap: COLMAP's binary.
        db: COLMAP's database, with its features and verified matches.
        images: the photos.
        out: where the model goes; work: where the steps before it go.
        run: runs a command, raising if it fails.

    Returns:
        How many photos the model has.

    Raises:
        Unavailable: VGGT can't run here; RuntimeError: too few photos are linked to place any.
    """
    why = unavailable()
    if why:
        raise Unavailable(why)
    device, limit = capacity()
    chosen = choose(db, limit)
    if len(chosen) < MIN_PHOTOS:
        raise RuntimeError(f"only {len(chosen)} photo(s) share verified matches with each other")
    log(f"VGGT: placing {len(chosen)} photos linked by verified matches, on the {'GPU' if device == 'cuda' else 'CPU'}")
    t0 = time.time()
    placed = predict([images / c["name"] for c in chosen], device, log)
    log(f"VGGT: placed them in {time.time() - t0:.0f} s")
    model = work / "start"
    write_model(model, chosen, placed)
    for i, (angle, err, refine_focal) in enumerate(rounds):
        tri, adjusted = work / f"triangulated{i}", out if i == len(rounds) - 1 else work / f"adjusted{i}"
        tri.mkdir(parents=True, exist_ok=True)
        adjusted.mkdir(parents=True, exist_ok=True)
        log(f"== COLMAP: points from those placements, round {i + 1} of {len(rounds)} (up to {angle}° and {err} px off)")
        run([colmap, "point_triangulator", "--database_path", str(db), "--image_path", str(images),
             "--input_path", str(model), "--output_path", str(tri),
             "--Mapper.tri_create_max_angle_error", str(angle), "--Mapper.tri_continue_max_angle_error", str(angle),
             "--Mapper.tri_re_max_angle_error", str(angle), "--Mapper.tri_merge_max_reproj_error", str(err),
             "--Mapper.tri_complete_max_reproj_error", str(err), "--Mapper.filter_max_reproj_error", str(err),
             "--Mapper.tri_ignore_two_view_tracks", "0", "--Mapper.tri_min_angle", "1", "--Mapper.filter_min_tri_angle", "1"])
        run([colmap, "bundle_adjuster", "--input_path", str(tri), "--output_path", str(adjusted),
             "--BundleAdjustment.refine_focal_length", str(refine_focal)])
        log(f"VGGT + COLMAP: {count(adjusted, 'points3D')} points after round {i + 1}")
        model = adjusted
    (work / "placed.json").write_text(json.dumps({"by": "vggt", "photos": len(chosen), "rounds": [list(r) for r in rounds]}))
    return count(out, "images") or len(chosen)
