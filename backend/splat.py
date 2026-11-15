"""Gaussian splats of the scene around a mesh: the ground, trees and rocks its photos also show, for the viewer to draw
behind the mesh (its *Scene* background), where the mesh shows only the boulder.

A splat is fitted, with gsplat on a CUDA GPU, to the photos a reconstruction placed: their cameras (cameras.json, in the
mesh's own frame), the photos themselves, and their people masks, so that passers-by aren't learnt. It starts from the
mesh's vertices and a shell of points around the scene for what's far off. Each photo gets a colour correction of its
own while fitting, as web photos are taken in all kinds of light, and the splat keeps the neutral colours. Blobs seen
by fewer than MIN_VIEWS photos, or nearly transparent, are dropped, so that the viewer's plain sky and ground show where
the photos didn't look rather than smears; so are those over the rock, which the mesh shows, and smears by the cameras. The result is scene.splat (32 bytes a blob: position, scale, colour,
rotation), which three.js viewers read.

A capture from the phone apps can bring frames of its own for this (its scenery: app/FORMAT.md), walked round the rock
and from the turn on the spot. capture_task() places them in the mesh's coordinates by the capture's own photos that
the mesh placed (each spell of tracking tied by those cameras: frames.tie()), and fit_capture() fits the splat to
those frames and those photos only, all the capture's own (so the splat is as free to show as they are), refining
each frame's pose a little as it goes, and stitches the turn's frames into a panorama of what's far off
(panorama()). Done on a machine that connects to the server (remote_job.py --task splat), as it needs a GPU.

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
SMALL_FILE = "scene-small.splat"   # the most visible SMALL_SPLATS of it, for phones
PANO_FILE = "scene-pano.webp"      # what's far off, all round: an equirectangular picture with transparency (panorama())
RECORD = "scene.json"
SMALL_SPLATS = 150_000
PANO_W = 4096           # the panorama's width; its height is half that
PANO_LOWEST_DEG = -8.0  # below this, a turn's frames see the ground near by, which the splat and the drawn ground show
REFINE_FROM = 1000      # steps before frames' poses start being refined
SHIFT_PENALTY = 10.0    # how much a frame's shift (as a share of the scene's radius) is held back, against its turn's 0.01
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
        # as COLMAP placed it: in the stored pixels, not turned by its EXIF Orientation as OpenCV would by default
        img = cv2.imread(str(photos / c["name"]), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
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
        m = cv2.imread(str(masks / f"{c['name']}.png"), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION) if masks else None
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
    cams = json.loads((mesh_dir / "cameras.json").read_text())
    data = views(mesh_dir, photos, masks, max_side)
    points, centre, radius = starting_points(mesh_dir, cams)
    centres = np.array([-np.array(rotation(v["q"])).T @ np.array(v["t"]) for v in cams])
    return fit_views(data, centres, points, centre, radius, rock_box(mesh_dir), log, steps, out or mesh_dir / FILE)


def fit_views(data: list[dict], centres, points, centre, radius: float, box, log: Log = print, steps: int = 7000, out: Path | None = None,
              extra: dict | None = None) -> dict:
    """Fit a splat to views (views()' dicts; one with "refine" has its pose refined as it goes, by a small turn and shift
    of its own) from blobs starting at points, and write it as out, with what it is in out's folder's RECORD; also
    SMALL_FILE beside it, its most visible SMALL_SPLATS. centres: where the cameras are, round which blobs are dropped.

    Raises:
        RuntimeError: too few views to go on.
    """
    import numpy as np
    import torch
    from gsplat import rasterization
    from gsplat.strategy import DefaultStrategy
    t0, dev = time.time(), "cuda"
    if len(data) < MIN_VIEWS:
        raise RuntimeError(f"only {len(data)} placed photo(s) could be read: at least {MIN_VIEWS} are needed")
    log(f"splat: {len(data)} views, {steps} steps")
    n = len(points)
    means = torch.tensor(np.asarray(points), dtype=torch.float32, device=dev)
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
    # each photo's own colour correction, as web photos are taken in every light (and a phone's frames as the light
    # changes over a walk): a 3x3 matrix and an offset
    looks = torch.nn.Parameter(torch.zeros((len(data), 3, 4), device=dev))
    look_opt = torch.optim.Adam([looks], lr=1e-3)
    # a frame placed by the phone's tracking: a small turn (axis times angle) and shift of its own, learnt as it goes
    nudges = torch.nn.Parameter(torch.zeros((len(data), 6), device=dev))
    nudge_opt = torch.optim.Adam([{"params": [nudges], "lr": 1e-4}])
    refined = torch.tensor([bool(v.get("refine")) for v in data], device=dev)
    images = [torch.tensor(v["image"], device=dev) for v in data]
    valid = [torch.tensor(v["valid"], device=dev) for v in data]
    Ks = [torch.tensor(v["K"], dtype=torch.float32, device=dev) for v in data]
    viewmats = [torch.tensor(v["viewmat"], dtype=torch.float32, device=dev) for v in data]
    rng = np.random.default_rng(0)

    def nudged(i: int):
        if not bool(refined[i]):
            return viewmats[i]
        w, t = nudges[i, :3], nudges[i, 3:] * radius
        angle = torch.linalg.norm(w) + 1e-12
        k = w / angle
        Kx = torch.zeros((3, 3), device=dev)
        Kx[0, 1], Kx[0, 2], Kx[1, 0], Kx[1, 2], Kx[2, 0], Kx[2, 1] = -k[2], k[1], k[2], -k[0], -k[1], k[0]
        R = torch.eye(3, device=dev) + torch.sin(angle) * Kx + (1 - torch.cos(angle)) * (Kx @ Kx)
        T = torch.eye(4, device=dev)
        T[:3, :3], T[:3, 3] = R, t
        return T @ viewmats[i]

    def render(i: int, mode: str = "RGB"):
        img = images[i]
        return rasterization(params["means"], params["quats"], torch.exp(params["scales"]), torch.sigmoid(params["opacities"]),
                             torch.sigmoid(params["colors"]), nudged(i)[None], Ks[i][None], img.shape[1], img.shape[0],
                             packed=False, render_mode=mode)

    for step in range(steps):
        i = int(rng.integers(len(data)))
        colors, alphas, info = render(i)
        strategy.step_pre_backward(params, optimizers, state, step, info)
        bg = torch.rand(3, device=dev)     # a random background each step: only what the photos show becomes opaque
        rgb = colors[0] + (1 - alphas[0]) * bg
        rgb = rgb @ (torch.eye(3, device=dev) + looks[i, :, :3]).T + looks[i, :, 3]
        m = valid[i][..., None]
        loss = (torch.abs(rgb - images[i]) * m).sum() / (m.sum() * 3 + 1)
        if bool(refined[i]):        # a frame's own nudge stays small unless the photos really want it; its shift more so,
            # as where a frame was is what the tracking knows best, and what's far off says little of it
            loss = loss + 1e-2 * (nudges[i, :3] ** 2).sum() + SHIFT_PENALTY * (nudges[i, 3:] ** 2).sum()
        loss.backward()
        strategy.step_post_backward(params, optimizers, state, step, info, packed=False)
        for opt in [*optimizers.values(), look_opt] + ([nudge_opt] if step >= REFINE_FROM else []):
            opt.step()
            opt.zero_grad(set_to_none=True)
        if step < REFINE_FROM:
            nudge_opt.zero_grad(set_to_none=True)
        if step % 1000 == 0 or step == steps - 1:
            log(f"splat: step {step + 1} of {steps}, {len(params['means'])} blobs, loss {loss.item():.4f}")

    with torch.no_grad():
        seen = torch.zeros(len(params["means"]), dtype=torch.int32, device=dev)
        residual = []
        for i in range(len(data)):
            colors, alphas, info = render(i)
            seen += (info["radii"][0].reshape(len(seen), -1) > 0).any(dim=1).int()
            rgb = (colors[0] + (1 - alphas[0]) * 0.5) @ (torch.eye(3, device=dev) + looks[i, :, :3]).T + looks[i, :, 3]
            m = valid[i][..., None]
            residual.append(float((torch.abs(rgb - images[i]) * m).sum() / (m.sum() * 3 + 1)))
        opacity = torch.sigmoid(params["opacities"])
        scales = torch.exp(params["scales"])
        means = params["means"]
        keep = (seen >= MIN_VIEWS) & (opacity >= MIN_OPACITY)
        # what's behind and around the rock only: the mesh shows the rock, and blobs over it would hide it. Blobs right by
        # where photos were taken, and big ones near the scene, are mostly smears that only fit a photo or two.
        if box:
            lo, hi = (torch.tensor(b, dtype=torch.float32, device=dev) for b in box)
            keep &= ~((means >= lo) & (means <= hi)).all(dim=1)
        cams = torch.tensor(np.asarray(centres), dtype=torch.float32, device=dev)
        keep &= torch.cdist(means, cams).min(dim=1).values > 0.1 * radius
        far = torch.linalg.norm(means - torch.tensor(centre, dtype=torch.float32, device=dev), dim=1) > 2 * radius
        keep &= far | (scales.max(dim=1).values < 0.3 * radius)
        weight = opacity * scales.prod(dim=1).pow(1 / 3)        # the most visible first, for the cap
        order = torch.argsort(torch.where(keep, weight, torch.full_like(weight, -1)), descending=True)
        order = order[:min(int(keep.sum()), MAX_SPLATS)]
        out = out or Path(FILE)
        args = (means[order].cpu().numpy(), scales[order].cpu().numpy(), torch.nn.functional.normalize(params["quats"][order], dim=1).cpu().numpy(),
                torch.sigmoid(params["colors"][order]).cpu().numpy(), opacity[order].cpu().numpy())
        write(out, *args)
        write(out.parent / SMALL_FILE, *(a[:SMALL_SPLATS] for a in args))
        moved = [(float(torch.linalg.norm(nudges[i, :3])) * 180 / math.pi, float(torch.linalg.norm(nudges[i, 3:])) * radius)
                 for i in range(len(data)) if bool(refined[i])]
    record = {"splats": len(order), "dropped": len(seen) - len(order), "photos": len(data), "steps": steps,
              "seconds": round(time.time() - t0), "created": time.time(), "centre": [float(x) for x in centre], "radius": radius,
              "small": min(len(order), SMALL_SPLATS)}
    if moved:       # how far the frames placed by the tracking had to be moved, and how well each kind of view is fitted
        deg, shift = sorted(m[0] for m in moved), sorted(m[1] for m in moved)
        record["refined"] = {"views": len(moved), "deg_median": round(deg[len(deg) // 2], 3), "deg_max": round(deg[-1], 3),
                             "shift_median": round(shift[len(shift) // 2], 5), "shift_max": round(shift[-1], 5)}
        fixed = [r for r, v in zip(residual, data) if not v.get("refine")]
        free = [r for r, v in zip(residual, data) if v.get("refine")]
        record["residual"] = {"fixed": round(float(np.median(fixed)), 4) if fixed else None, "refined": round(float(np.median(free)), 4)}
        # each frame's own nudge (a turn, axis times radians, then a shift in the scene's units), to see where it went
        record["nudges"] = {v["name"]: [round(float(x), 6) for x in nudges[i, :3].tolist()] + [round(float(x) * radius, 6) for x in nudges[i, 3:].tolist()]
                            for i, v in enumerate(data) if v.get("refine")}
    record.update(extra or {})
    (out.parent / RECORD).write_text(json.dumps(record))
    log(f"splat: {record['splats']} blobs kept ({record['dropped']} over the rock, by a camera, or seen by too few photos), "
        f"in {record['seconds']} s")
    return record


# ---- a capture's scenery ------------------------------------------------------------------------------------------
def _conj(q) -> list[float]:
    return [q[0], -q[1], -q[2], -q[3]]


def _spell_name(base: str, k: int) -> str:
    """The `ar.s` of a capture's k-th spell of tracking (1 the first), as the apps name them."""
    return base if k <= 1 else f"{base}.{k}"


def read_points(path: Path) -> list[tuple[float, float, float, int]]:
    """A capture's tracked points file (app/FORMAT.md): (x, y, z, spell), in its tracking's world."""
    import struct
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if data[:4] != b"BMP1" or len(data) < 8:
        return []
    n = struct.unpack_from("<I", data, 4)[0]
    out = []
    for k in range(min(n, (len(data) - 8) // 14)):
        x, y, z, spell = struct.unpack_from("<fffH", data, 8 + 14 * k)
        out.append((x, y, z, spell))
    return out


def capture_task(mesh_dir: Path, project_dir: Path) -> dict:
    """What a machine needs to fit a capture's scenery (fit_capture()): its frames and the capture's own photos that the
    mesh placed, each as {name, folder, K, viewmat, refine, kind}, in the mesh's coordinates; points to start blobs at
    (the mesh's own, and the tracked points of each spell that's tied); the rock's box; and how each spell was tied.

    A spell of tracking is tied to the mesh by its photos the mesh placed (frames.tie(): the turn from their rotations,
    scale and shift from where they are); a spell with too few of those can't be, and its frames are left out.

    Raises:
        ValueError: the capture has no frames of its scenery, or none of its spells could be tied.
    """
    import numpy as np
    import frames as frames_mod
    rec = json.loads((project_dir / "capture.json").read_text())
    scenery = [f for f in rec.get("scenery") or [] if (project_dir / "scenery" / f["name"]).is_file()]
    if not scenery:
        raise ValueError("the capture has no frames of its scenery here")
    cams = json.loads((mesh_dir / "cameras.json").read_text())
    by_name = {c["name"]: c for c in cams}
    # the capture's photos the mesh placed, as the tracking had them: in cameras.json's terms (world to camera)
    tracked: dict[str, list[dict]] = {}
    for shot in rec.get("shots") or []:
        ar = (shot.get("blob") or {}).get("ar")
        if not shot.get("kept") or not ar or shot["name"] not in by_name:
            continue
        R = np.asarray(rotation(_conj(ar["q"])))
        tracked.setdefault(ar["s"], []).append({"name": shot["name"], "q": _conj(ar["q"]), "t": list(-R @ np.asarray(ar["p"], float))})
    ties = {s: frames_mod.tie(cs, cams) for s, cs in tracked.items()}
    tied = {s: t for s, t in ties.items() if t["status"] == "tied"}
    if not tied:
        raise ValueError("none of the capture's spells of tracking could be tied to its mesh: " +
                         "; ".join(f"{s}: {t.get('why')}" for s, t in ties.items()) if ties else "none of its photos were placed")
    views = []
    for f in scenery:
        tie = tied.get(f["ar"]["s"])
        if tie is None:
            continue
        s, Rt, t = tie["scale"], np.asarray(tie["rot"], float), np.asarray(tie["shift"], float)
        Rcw = np.asarray(rotation(_conj(f["ar"]["q"])))                 # world to camera, in the tracking's world
        c = s * Rt @ np.asarray(f["ar"]["p"], float) + t      # where it was, in the mesh's
        R = Rcw @ Rt.T
        k = f["k"]
        views.append({"name": f["name"], "folder": "scenery", "kind": f["kind"], "refine": True,
                      "K": [[k["fx"], 0, k["cx"]], [0, k["fy"], k["cy"]], [0, 0, 1]], "size": [k["w"], k["h"]],
                      "viewmat": [list(map(float, R[0])) + [float(-(R @ c)[0])], list(map(float, R[1])) + [float(-(R @ c)[1])],
                                  list(map(float, R[2])) + [float(-(R @ c)[2])], [0.0, 0.0, 0.0, 1.0]]})
    # and the capture's own photos the mesh placed, as it placed them: these hold the rock's part where the mesh has it
    for spell, cs in tracked.items():
        for c in cs:
            cam = by_name[c["name"]]
            K, dist, _ = intrinsics(cam["lens"], 1.0)
            vm = np.eye(4)
            vm[:3, :3], vm[:3, 3] = np.asarray(rotation(cam["q"])), cam["t"]
            views.append({"name": c["name"], "folder": "photos", "kind": "photo", "refine": False, "K": K.tolist(), "dist": dist.tolist(),
                          "size": [cam["lens"]["width"], cam["lens"]["height"]], "viewmat": vm.tolist()})
    base = rec.get("shots", [{}])[0].get("blob", {}).get("ar", {}).get("s", "a").split(".")[0] if rec.get("shots") else "a"
    pts = []
    for x, y, z, spell in read_points(project_dir / "capture-points.bin"):
        tie = tied.get(_spell_name(base, spell))
        if tie:
            pts.append((tie["scale"] * np.asarray(tie["rot"], float) @ np.array([x, y, z]) + np.asarray(tie["shift"], float)).tolist())
    box = rock_box(mesh_dir)
    return {"views": views, "points": pts, "box": [list(map(float, b)) for b in box] if box else None,
            "ties": {s: {k: t.get(k) for k in ("status", "why", "scale", "shared", "of_shared", "rms", "rot_deg")} for s, t in ties.items()},
            "frames": len(scenery), "placed": sum(1 for v in views if v["kind"] != "photo")}


def task_views(job: Path, task: dict, max_side: int = MAX_SIDE) -> list[dict]:
    """capture_task()'s views, read from a job's folders and shrunk to fit: as views() gives them."""
    import cv2
    import numpy as np
    out = []
    for v in task["views"]:
        img = cv2.imread(str(job / v["folder"] / v["name"]), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is None:
            continue
        w, h = v["size"]
        if (img.shape[1], img.shape[0]) != (w, h):
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        K = np.asarray(v["K"], float)
        dist = np.asarray(v.get("dist") or [0, 0, 0, 0], float)
        if np.any(dist):
            img = cv2.undistort(img, K, dist)
        valid = np.ones(img.shape[:2], np.uint8)
        mask = job / "masks" / f"{v['name']}.png"
        m = cv2.imread(str(mask), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION) if mask.is_file() else None
        if m is not None:
            valid = (cv2.resize(m, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST) > 127).astype(np.uint8)
        scale = min(1.0, max_side / max(img.shape[:2]))
        size = (round(img.shape[1] * scale), round(img.shape[0] * scale))
        img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
        valid = cv2.resize(valid, size, interpolation=cv2.INTER_NEAREST)
        out.append({"name": v["name"], "kind": v["kind"], "refine": v["refine"], "image": np.ascontiguousarray(img[:, :, ::-1], dtype=np.float32) / 255,
                    "valid": valid.astype(bool), "K": np.diag([scale, scale, 1.0]) @ K, "viewmat": np.asarray(v["viewmat"], float)})
    return out


def fit_capture(job: Path, out_dir: Path, log: Log = print, steps: int = 15000, max_side: int = MAX_SIDE) -> dict:
    """Fit a capture's scenery as capture_task() set it out in job/task.json, with the job's scenery/ and photos/, into
    out_dir: the splat (FILE and SMALL_FILE), its RECORD, and the panorama of the turn's frames (PANO_FILE) if it had some.

    Raises:
        RuntimeError: it can't be done here, or there are too few views.
    """
    why = unavailable()
    if why:
        raise RuntimeError(f"can't fit a splat: {why}")
    import numpy as np
    task = json.loads((job / "task.json").read_text())
    data = task_views(job, task, max_side)
    centres = np.array([-v["viewmat"][:3, :3].T @ v["viewmat"][:3, 3] for v in data])
    mesh_dir = job / "mesh"
    points, centre, radius = starting_points(mesh_dir, [{"q": [1, 0, 0, 0], "t": list(-c)} for c in centres])
    if task.get("points"):      # where the tracking found the world: blobs start on the ground and the trees too
        tracked = np.asarray(task["points"], float)
        points = np.concatenate([points, tracked[np.random.default_rng(1).permutation(len(tracked))[:40_000]]])
    box = tuple(np.asarray(b, float) for b in task["box"]) if task.get("box") else rock_box(mesh_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    extra = {"sources": "capture", "ties": task.get("ties"), "frames": task.get("frames")}
    pano = [v for v in data if v["kind"] == "pano"]
    if pano:
        try:
            extra["pano"] = panorama(job, task, out_dir / PANO_FILE)
        except Exception as e:      # a backdrop without its far part is still one
            log(f"splat: the panorama failed: {type(e).__name__}: {e}")
    return fit_views(data, centres, points, centre, radius, box, log, steps, out_dir / FILE, extra)


def panorama(job: Path, task: dict, out: Path, width: int = PANO_W) -> dict:
    """Stitch the frames of the turn on the spot (capture_task()'s views of kind "pano") into an equirectangular picture
    of what's far off, by which way each looked alone (what's far off is the same from a step or two apart), feathered
    where they overlap, transparent where none looked. In the mesh's coordinates, +Y up: the pixel (i, j) of a W by H
    picture looks along (cos(lat) sin(lon), sin(lat), cos(lat) cos(lon)), lon = (i + 0.5) / W * 2 pi - pi, lat = pi / 2 -
    (j + 0.5) / H * pi. Saved as WebP with transparency. Returns what it covers.

    What's near is left out, as on a picture of what's far off it would be painted on the sky: the rock (rays through
    its box, task["box"], from where each frame was taken) and the ground near by (below PANO_LOWEST_DEG)."""
    import cv2
    import numpy as np
    from PIL import Image
    H = width // 2
    acc = np.zeros((H, width, 3), np.float32)
    wsum = np.zeros((H, width), np.float32)
    lon = (np.arange(width) + 0.5) / width * 2 * np.pi - np.pi
    lat = np.pi / 2 - (np.arange(H) + 0.5) / H * np.pi
    used = 0
    box = [np.asarray(b, float) for b in task["box"]] if task.get("box") else None
    for v in task["views"]:
        if v["kind"] != "pano":
            continue
        img = cv2.imread(str(job / v["folder"] / v["name"]), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is None:
            continue
        K = np.asarray(v["K"], float)
        sx, sy = img.shape[1] / v["size"][0], img.shape[0] / v["size"][1]
        R = np.asarray(v["viewmat"], float)[:3, :3]
        # only the rows and columns this frame could reach: its view's directions' latitudes, and all longitudes
        fwd = R.T @ np.array([0.0, 0.0, 1.0])
        half = math.atan(max(v["size"]) / 2 / min(K[0, 0], K[1, 1])) * 1.05
        flat = math.asin(max(-1.0, min(1.0, fwd[1])))
        rows = np.flatnonzero((lat > flat - half) & (lat < flat + half) & (lat > math.radians(PANO_LOWEST_DEG)))
        if not len(rows):
            continue
        la, lo = np.meshgrid(lat[rows], lon, indexing="ij")
        d = np.stack([np.cos(la) * np.sin(lo), np.sin(la), np.cos(la) * np.cos(lo)], axis=-1)
        c = d @ R.T
        z = c[..., 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (K[0, 0] * c[..., 0] / z + K[0, 2]) * sx
            w = (K[1, 1] * c[..., 1] / z + K[1, 2]) * sy
        inside = (z > 0) & (u >= 0) & (u < img.shape[1] - 1) & (w >= 0) & (w < img.shape[0] - 1)
        if box is not None:     # the rock itself, from here: rays through its box (slabs)
            vm = np.asarray(v["viewmat"], float)
            o = -vm[:3, :3].T @ vm[:3, 3]
            with np.errstate(divide="ignore", invalid="ignore"):
                t1, t2 = (box[0] - o) / d, (box[1] - o) / d
            near_t = np.nanmax(np.minimum(t1, t2), axis=-1)
            far_t = np.nanmin(np.maximum(t1, t2), axis=-1)
            inside &= ~((far_t >= np.maximum(near_t, 0)) & (far_t > 0))
        if not inside.any():
            continue
        sample = cv2.remap(img, np.where(inside, u, -1).astype(np.float32), np.where(inside, w, -1).astype(np.float32), cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_CONSTANT)
        # weighted most at the frame's middle, nothing at its edge
        edge = np.minimum(np.minimum(u, img.shape[1] - 1 - u) / (img.shape[1] / 2), np.minimum(w, img.shape[0] - 1 - w) / (img.shape[0] / 2))
        weight = np.where(inside, np.clip(edge, 0, 1) ** 2, 0).astype(np.float32)
        acc[rows] += sample[..., ::-1].astype(np.float32) * weight[..., None]
        wsum[rows] += weight
        used += 1
    if not used:
        raise ValueError("no frame of the turn could be read")
    rgb = np.where(wsum[..., None] > 0, acc / np.maximum(wsum, 1e-9)[..., None], 0)
    alpha = np.clip(wsum / 0.25, 0, 1) * 255         # opaque where frames overlapped well, fading out at the band's edges
    picture = np.concatenate([np.clip(rgb, 0, 255), alpha[..., None]], axis=-1).astype(np.uint8)
    tmp = out.with_suffix(".partial.webp")
    Image.fromarray(picture, "RGBA").save(tmp, "WEBP", quality=80)
    tmp.replace(out)
    covered = wsum > 0
    rows = np.flatnonzero(covered.any(axis=1))
    return {"frames": used, "width": width, "height": H, "covered": round(float(covered.mean()), 4),
            "lat_deg": [round(float(math.degrees(lat[rows[-1]])), 1), round(float(math.degrees(lat[rows[0]])), 1)]}


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
