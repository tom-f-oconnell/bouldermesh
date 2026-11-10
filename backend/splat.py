"""Gaussian splats of the scene around a mesh: the ground, trees and rocks its photos also show, for the viewer to draw
behind the mesh (its *Scene* background), where the mesh shows only the boulder.

A splat is fitted, with gsplat on a CUDA GPU, to the photos a reconstruction placed: their cameras (cameras.json, in the
mesh's own frame), the photos themselves, and their people masks, so that passers-by aren't learnt. It starts from the
mesh's vertices and a shell of points around the scene for what's far off. Each photo gets a colour correction of its
own while fitting, as web photos are taken in all kinds of light, and the splat keeps the neutral colours. Blobs seen
by fewer than MIN_VIEWS photos, or nearly transparent, are dropped, so that the viewer's plain sky and ground show where
the photos didn't look rather than smears; so are those over the rock, which the mesh shows, and smears by the cameras. The result is scene.splat (32 bytes a blob: position, scale, colour,
rotation), which three.js viewers read.

Off unless SPLATS=1, as it needs CUDA builds of torch and gsplat, which the image doesn't have (its torch is the CPU
one), and meshes placed from few photos give poor splats: design.md, "Scenes around meshes". By hand:

    python3 backend/splat.py data/meshes/<id> data/projects/<project>/photos [--steps 7000]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
FILE = "scene.splat"
RECORD = "scene.json"
MIN_VIEWS = 3           # a blob fewer photos saw is a guess
MIN_OPACITY = 0.02
MAX_SPLATS = 400_000    # about 13 MB
MAX_SIDE = 1000         # photos are fitted at most this many pixels across
SHELL = 5.0             # far points start on a sphere this many times the scene's radius out


def enabled() -> bool:
    return os.environ.get("SPLATS") == "1"


def unavailable() -> str | None:
    """Why splats can't be fitted here, or None if they can."""
    try:
        import torch
    except ImportError:
        return "torch isn't installed"
    try:
        import gsplat  # noqa: F401
    except ImportError:
        return "gsplat isn't installed (pip install gsplat, with a CUDA build of torch)"
    if not torch.cuda.is_available():
        return "there's no CUDA GPU (or torch was built without CUDA)"
    return None


def rotation(q) -> "list[list[float]]":
    """World to camera, from COLMAP's quaternion w, x, y, z."""
    w, x, y, z = q
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]


def intrinsics(lens: dict, scale: float):
    """A camera matrix, and OpenCV distortion coefficients, for COLMAP's lens models, scaled to the fitting size."""
    import numpy as np
    p, model = lens["params"], lens["model"]
    if model in (0, 2, 3):          # SIMPLE_PINHOLE, SIMPLE_RADIAL, RADIAL: f, cx, cy[, k1[, k2]]
        fx = fy = p[0]
        cx, cy = p[1], p[2]
        dist = [p[3] if len(p) > 3 else 0.0, p[4] if len(p) > 4 else 0.0, 0.0, 0.0]
    elif model in (1, 4):           # PINHOLE, OPENCV: fx, fy, cx, cy[, k1, k2, p1, p2]
        fx, fy, cx, cy = p[:4]
        dist = list(p[4:8]) + [0.0] * (4 - len(p[4:8]))
    else:
        raise ValueError(f"COLMAP lens model {model} isn't supported")
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], float)
    return K, np.array(dist, float), np.diag([scale, scale, 1.0]) @ K


def views(mesh_dir: Path, photos: Path, masks: Path | None, max_side: int = MAX_SIDE) -> list[dict]:
    """The placed photos, undistorted and shrunk: {name, image (H, W, 3 floats 0-1), valid (H, W), K, viewmat}."""
    import cv2
    import numpy as np
    out = []
    for c in json.loads((mesh_dir / "cameras.json").read_text()):
        img = cv2.imread(str(photos / c["name"]), cv2.IMREAD_COLOR)
        lens = c.get("lens")
        if img is None or not lens:
            continue
        if (img.shape[1], img.shape[0]) != (lens["width"], lens["height"]):
            img = cv2.resize(img, (lens["width"], lens["height"]), interpolation=cv2.INTER_AREA)
        scale = min(1.0, max_side / max(img.shape[:2]))
        K, dist, Ks = intrinsics(lens, scale)
        if np.any(dist):
            img = cv2.undistort(img, K, dist)
        valid = np.ones(img.shape[:2], np.uint8)
        m = cv2.imread(str(masks / f"{c['name']}.png"), cv2.IMREAD_GRAYSCALE) if masks else None
        if m is not None:           # COLMAP's masks: black where a person is
            valid = (cv2.resize(m, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST) > 127).astype(np.uint8)
            if np.any(dist):
                valid = cv2.undistort(valid, K, dist)
        size = (round(img.shape[1] * scale), round(img.shape[0] * scale))
        img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
        valid = cv2.resize(valid, size, interpolation=cv2.INTER_NEAREST)
        viewmat = np.eye(4)
        viewmat[:3, :3], viewmat[:3, 3] = rotation(c["q"]), c["t"]
        out.append({"name": c["name"], "image": np.ascontiguousarray(img[:, :, ::-1], dtype=np.float32) / 255, "valid": valid.astype(bool),
                    "K": Ks, "viewmat": viewmat})
    return out


def rock_box(mesh_dir: Path):
    """Where the rock is, which the mesh shows: its vertices' 5th to 95th percentiles, with a margin (as the viewer frames
    it), so that strays far off don't count. None without a mesh."""
    import numpy as np
    import meshclean
    path = meshclean.mesh_file(mesh_dir)
    if not path:
        return None
    V = np.asarray(meshclean._geometry(*meshclean._read_glb(path))[0], float)
    lo, hi = np.percentile(V, 5, axis=0), np.percentile(V, 95, axis=0)
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


def starting_points(mesh_dir: Path, cams: list[dict], n_mesh: int = 60_000, n_shell: int = 30_000, seed: int = 0):
    """Where blobs start: the mesh's vertices, and a shell around the cameras and the mesh for what's far off. Also the
    scene's centre and radius."""
    import numpy as np
    import meshclean
    rng = np.random.default_rng(seed)
    centres = np.array([-np.array(rotation(v["q"])).T @ np.array(v["t"]) for v in cams])
    pts = [centres]
    path = meshclean.mesh_file(mesh_dir)
    if path:
        V = np.asarray(meshclean._geometry(*meshclean._read_glb(path))[0], float)
        pts.append(V[rng.choice(len(V), min(n_mesh, len(V)), replace=False)])
    allp = np.concatenate(pts)
    centre = np.median(allp, axis=0)
    radius = float(np.percentile(np.linalg.norm(allp - centre, axis=1), 95)) or 1.0
    d = rng.normal(size=(n_shell, 3))
    shell = centre + d / np.linalg.norm(d, axis=1, keepdims=True) * radius * SHELL * rng.uniform(0.8, 1.2, (n_shell, 1))
    return np.concatenate(pts[1:] + [shell]) if len(pts) > 1 else shell, centre, radius


def fit(mesh_dir: Path, photos: Path, masks: Path | None, log: Log = print, steps: int = 7000, max_side: int = MAX_SIDE,
        out: Path | None = None) -> dict:
    """Fit a splat to a mesh's placed photos and save it as out (mesh_dir/scene.splat), with what it is in scene.json.

    Raises:
        RuntimeError: it can't be done here (unavailable()), or there are too few photos to go on.
    """
    why = unavailable()
    if why:
        raise RuntimeError(f"can't fit a splat: {why}")
    import numpy as np
    import torch
    from gsplat import rasterization
    from gsplat.strategy import DefaultStrategy
    t0, dev = time.time(), "cuda"
    cams = json.loads((mesh_dir / "cameras.json").read_text())
    data = views(mesh_dir, photos, masks, max_side)
    if len(data) < MIN_VIEWS:
        raise RuntimeError(f"only {len(data)} placed photo(s) could be read: at least {MIN_VIEWS} are needed")
    log(f"splat: {len(data)} photos, {steps} steps")
    points, centre, radius = starting_points(mesh_dir, cams)
    n = len(points)
    means = torch.tensor(points, dtype=torch.float32, device=dev)
    near = torch.cat([torch.cdist(means[i:i + 512], means).topk(4, largest=False).values[:, 1:].mean(dim=1)
                      for i in range(0, n, 512)])        # each blob starts as wide as the gap to its 3 nearest
    params = torch.nn.ParameterDict({
        "means": torch.nn.Parameter(means),
        "scales": torch.nn.Parameter(torch.log(near.clamp(min=radius * 1e-4))[:, None].repeat(1, 3)),
        "quats": torch.nn.Parameter(torch.tensor([[1.0, 0, 0, 0]], device=dev).repeat(n, 1)),
        "opacities": torch.nn.Parameter(torch.full((n,), math.log(0.1 / 0.9), device=dev)),
        "colors": torch.nn.Parameter(torch.zeros((n, 3), device=dev)),
    })
    lrs = {"means": 1.6e-4 * radius, "scales": 5e-3, "quats": 1e-3, "opacities": 5e-2, "colors": 2.5e-3}
    optimizers = {k: torch.optim.Adam([{"params": params[k], "lr": lr, "name": k}], eps=1e-15) for k, lr in lrs.items()}
    strategy = DefaultStrategy(refine_start_iter=500, refine_stop_iter=int(steps * 0.7), refine_every=100,
                               reset_every=3000, verbose=False)
    strategy.check_sanity(params, optimizers)
    state = strategy.initialize_state(scene_scale=radius)
    # each photo's own colour correction, as web photos come in every light: a 3x3 matrix and an offset
    looks = torch.nn.Parameter(torch.zeros((len(data), 3, 4), device=dev))
    look_opt = torch.optim.Adam([looks], lr=1e-3)
    images = [torch.tensor(v["image"], device=dev) for v in data]
    valid = [torch.tensor(v["valid"], device=dev) for v in data]
    Ks = [torch.tensor(v["K"], dtype=torch.float32, device=dev) for v in data]
    viewmats = [torch.tensor(v["viewmat"], dtype=torch.float32, device=dev) for v in data]
    rng = np.random.default_rng(0)

    def render(i: int):
        img = images[i]
        return rasterization(params["means"], params["quats"], torch.exp(params["scales"]), torch.sigmoid(params["opacities"]),
                             torch.sigmoid(params["colors"]), viewmats[i][None], Ks[i][None], img.shape[1], img.shape[0],
                             packed=False)

    for step in range(steps):
        i = int(rng.integers(len(data)))
        colors, alphas, info = render(i)
        strategy.step_pre_backward(params, optimizers, state, step, info)
        bg = torch.rand(3, device=dev)     # a random background each step: only what the photos show becomes opaque
        rgb = colors[0] + (1 - alphas[0]) * bg
        rgb = rgb @ (torch.eye(3, device=dev) + looks[i, :, :3]).T + looks[i, :, 3]
        m = valid[i][..., None]
        loss = (torch.abs(rgb - images[i]) * m).sum() / (m.sum() * 3 + 1)
        loss.backward()
        strategy.step_post_backward(params, optimizers, state, step, info, packed=False)
        for opt in [*optimizers.values(), look_opt]:
            opt.step()
            opt.zero_grad(set_to_none=True)
        if step % 1000 == 0 or step == steps - 1:
            log(f"splat: step {step + 1} of {steps}, {len(params['means'])} blobs, loss {float(loss):.4f}")

    with torch.no_grad():
        seen = torch.zeros(len(params["means"]), dtype=torch.int32, device=dev)
        for i in range(len(data)):
            radii = render(i)[2]["radii"][0]
            seen += (radii.reshape(len(seen), -1) > 0).any(dim=1).int()
        opacity = torch.sigmoid(params["opacities"])
        scales = torch.exp(params["scales"])
        means = params["means"]
        keep = (seen >= MIN_VIEWS) & (opacity >= MIN_OPACITY)
        # what's behind and around the rock only: the mesh shows the rock, and blobs over it would hide it. Blobs right by
        # where photos were taken, and big ones near the scene, are mostly smears that only fit a photo or two.
        box = rock_box(mesh_dir)
        if box:
            lo, hi = (torch.tensor(b, dtype=torch.float32, device=dev) for b in box)
            keep &= ~((means >= lo) & (means <= hi)).all(dim=1)
        centres = torch.tensor(np.array([-np.array(rotation(v["q"])).T @ np.array(v["t"]) for v in cams]), dtype=torch.float32, device=dev)
        keep &= torch.cdist(means, centres).min(dim=1).values > 0.1 * radius
        far = torch.linalg.norm(means - torch.tensor(centre, dtype=torch.float32, device=dev), dim=1) > 2 * radius
        keep &= far | (scales.max(dim=1).values < 0.3 * radius)
        weight = opacity * scales.prod(dim=1).pow(1 / 3)        # the most visible first, for the cap
        order = torch.argsort(torch.where(keep, weight, torch.full_like(weight, -1)), descending=True)
        order = order[:min(int(keep.sum()), MAX_SPLATS)]
        out = out or mesh_dir / FILE
        write(out, means[order].cpu().numpy(), scales[order].cpu().numpy(),
              torch.nn.functional.normalize(params["quats"][order], dim=1).cpu().numpy(),
              torch.sigmoid(params["colors"][order]).cpu().numpy(), opacity[order].cpu().numpy())
    record = {"splats": len(order), "dropped": len(seen) - len(order), "photos": len(data), "steps": steps,
              "seconds": round(time.time() - t0), "created": time.time(), "centre": centre.tolist(), "radius": radius}
    (out.parent / RECORD).write_text(json.dumps(record))
    log(f"splat: {record['splats']} blobs kept ({record['dropped']} over the rock, by a camera, or seen by too few photos), "
        f"in {record['seconds']} s")
    return record


def write(path: Path, means, scales, quats, colors, opacity) -> None:
    """The .splat layout: per blob, position and scale (float32 x 3 each), colour and opacity (uint8 x 4), and rotation
    as a quaternion w, x, y, z mapped to 0-255."""
    import numpy as np
    n = len(means)
    rows = np.zeros(n, dtype=[("pos", "<f4", 3), ("scale", "<f4", 3), ("rgba", "u1", 4), ("rot", "u1", 4)])
    rows["pos"], rows["scale"] = means, scales
    rows["rgba"] = np.clip(np.concatenate([colors, opacity[:, None]], axis=1) * 255, 0, 255).round()
    rows["rot"] = np.clip(quats * 128 + 128, 0, 255).round()
    tmp = path.with_suffix(".partial")
    tmp.write_bytes(rows.tobytes())
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mesh_dir", type=Path)
    ap.add_argument("photos", type=Path)
    ap.add_argument("--masks", type=Path, help="the project's masks/ (by default, beside photos/)")
    ap.add_argument("--steps", type=int, default=7000)
    ap.add_argument("--max-side", type=int, default=MAX_SIDE)
    ap.add_argument("--out", type=Path, help=f"where to write it (by default the mesh's {FILE})")
    a = ap.parse_args()
    masks = a.masks or (a.photos.parent / "masks")
    fit(a.mesh_dir, a.photos, masks if masks.is_dir() else None, steps=a.steps, max_side=a.max_side, out=a.out)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
