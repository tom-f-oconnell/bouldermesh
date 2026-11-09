"""Where climbers put their hands and feet, from the videos a mesh was built from.

For each video with frames placed in a mesh: download it again, take FPS frames a second, find the
climber's hands and feet in each with a pose model (RTMPose, Halpe26 body with feet, through rtmlib),
and cast each one onto the mesh from the camera of the placed frame of the same video that the frame
matches best. A hand or foot whose point on the mesh stays still for HOLD_SECONDS is in contact with a
hold. Contacts close together are one hold, given a role as the Tension Board app colours them: the
first hands' holds are start holds, the last hands' finish holds, other hands' hand holds, and holds
only feet touched foot holds. People's masks only say where people are, not which parts are hands
and which feet, which is why a pose model is used.

  beta.json      per video: its holds, each limb's track on the mesh, and how far it got
  beta_<tag>.jpg the video's placed frame most matched, with the tracks and holds drawn on it
  holds.json     holds marked by hand in the viewer: [{p: [x, y, z], role}]
  beta_poses.json per video, each frame's climber: {t, kp, sc, ref, H}, all their keypoints and scores,
                 the placed frame it matched and the homography into it, which scale.py measures them by

Positions are in the mesh's own coordinates, as are its cameras in cameras.json.
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import shutil
import time
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
FILE, HOLDS_FILE, POSES_FILE = "beta.json", "holds.json", "beta_poses.json"
ROLES = ("start", "hand", "finish", "foot")     # the order the Tension Board app cycles a hold through
COLORS = {"start": (60, 210, 25), "hand": (255, 107, 47), "finish": (42, 42, 255), "foot": (245, 61, 255)}   # BGR
FPS = 2.0               # frames a second looked at
MAX_SECONDS = 150       # of each video
DET_EVERY = 5           # frames between person detections; the tracker follows them in between
HAND_REACH = 0.35       # a hand's point on a hold: this far past the wrist, as a share of the forearm
STILL = 0.03            # a hand or foot moving less than this share of the rock's size between frames is still
HOLD_SECONDS = 1.0      # still for this long, it's on a hold
HOLD_RADIUS = 0.04      # contacts closer than this share of the rock's size are the same hold
GROUND = 0.04           # feet this close to the lowest part of the mesh are on the ground
MIN_INLIERS = 25        # feature matches a frame needs with a placed frame to be placed by it
POSE = {"det": "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/yolox_m_8xb8-300e_humanart-c2c7a14a.zip",
        "det_input_size": (640, 640),
        "pose": "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/"
                "rtmpose-m_simcc-body7_pt-body7-halpe26_700e-256x192-4d3e73dd_20230605.zip",
        "pose_input_size": (192, 256)}
# Halpe26 keypoints: wrists 9, 10; elbows 7, 8; ankles 15, 16; big toes 20, 21
LIMBS = {"lh": ("hand", 9, 7), "rh": ("hand", 10, 8), "lf": ("foot", 15, 20), "rf": ("foot", 16, 21)}
KP_MIN = 0.3            # keypoint scores below this aren't used


def available() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("numpy", "cv2", "rtmlib", "onnxruntime"))


# ------------------------------------------------------------------ cameras and the mesh
def _rotation(q):
    import numpy as np
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def lens(cam: dict, size: tuple[int, int]) -> tuple[float, float, float, float, bool]:
    """f, cx, cy, k (radial) of a camera, for an image of `size`; approximate if COLMAP's wasn't kept,
    from its usual starting guess."""
    w, h = size
    L = cam.get("lens")
    if L and L["model"] in (0, 2) and L["width"]:
        s = w / L["width"]
        f, cx, cy = (p * s for p in L["params"][:3])
        return f, cx, cy, (L["params"][3] if L["model"] == 2 else 0.0), False
    return 1.2 * max(w, h), w / 2, h / 2, 0.0, True


def normalized(cam: dict, size, uv) -> tuple[float, float]:
    """Pixel uv of a placed photo as x/z, y/z in its camera's coordinates, its lens's distortion undone."""
    f, cx, cy, k, _ = lens(cam, size)
    xd, yd = (uv[0] - cx) / f, (uv[1] - cy) / f
    x, y = xd, yd
    for _ in range(5):     # undo SIMPLE_RADIAL distortion: xd = x (1 + k r^2)
        r2 = x * x + y * y
        x, y = xd / (1 + k * r2), yd / (1 + k * r2)
    return x, y


def depth(cam: dict, X) -> float:
    """How far in front of a placed photo's camera a point is, along its axis."""
    import numpy as np
    return float((_rotation(cam["q"]) @ np.asarray(X, float) + np.asarray(cam["t"], float))[2])


def camera_ray(cam: dict, size, uv) -> tuple:
    """The ray through pixel uv of a placed photo, in the mesh's coordinates: origin, unit direction."""
    import numpy as np
    x, y = normalized(cam, size, uv)
    R, t = _rotation(cam["q"]), np.asarray(cam["t"], float)
    d = R.T @ np.array([x, y, 1.0])
    return -R.T @ t, d / np.linalg.norm(d)


def project(cam: dict, size, X) -> tuple[float, float] | None:
    """Where a point in the mesh's coordinates appears in a placed photo, or None if it's behind it."""
    import numpy as np
    f, cx, cy, k, _ = lens(cam, size)
    p = _rotation(cam["q"]) @ np.asarray(X, float) + np.asarray(cam["t"], float)
    if p[2] <= 0:
        return None
    x, y = p[0] / p[2], p[1] / p[2]
    r = 1 + k * (x * x + y * y)
    return float(f * x * r + cx), float(f * y * r + cy)


class Mesh:
    """A mesh's triangles, ready to cast rays at, and its size."""

    def __init__(self, V, F):
        import numpy as np
        V = np.asarray(V, float)
        self.V, self.F = V, np.asarray(F)
        self.v0 = V[F[:, 0]]
        self.e1, self.e2 = V[F[:, 1]] - self.v0, V[F[:, 2]] - self.v0
        # each triangle's bounding sphere, so that a ray is only tested against the few it passes near
        self.c = self.v0 + (self.e1 + self.e2) / 3
        self.r = np.sqrt(np.max([np.einsum("ij,ij->i", d, d) for d in (self.v0 - self.c, self.e1 + self.v0 - self.c,
                                                                         self.e2 + self.v0 - self.c)], axis=0))
        self.cc = np.einsum("ij,ij->i", self.c, self.c)
        lo, hi = np.percentile(V, 5, axis=0), np.percentile(V, 95, axis=0)
        self.size = float(max(hi - lo))
        self.ground = float(np.percentile(V[:, 1], 2))    # +Y is up in a levelled mesh

    @classmethod
    def load(cls, mesh_dir: Path) -> "Mesh":
        import meshclean
        path = meshclean.mesh_file(mesh_dir)
        if not path:
            raise ValueError("only GLB meshes can be read here")
        V, F, _ = meshclean._geometry(*meshclean._read_glb(path))
        return cls(V, F)

    def hit(self, origin, direction):
        """The nearest point where a ray meets the mesh, or None (Möller-Trumbore, on the triangles whose
        bounding spheres it passes through)."""
        import numpy as np
        origin, direction = np.asarray(origin, float), np.asarray(direction, float)
        along = self.c @ direction - origin @ direction
        perp2 = self.cc - 2 * (self.c @ origin) + origin @ origin - along * along
        near = np.flatnonzero((along > -self.r) & (perp2 <= self.r * self.r))
        if not near.size:
            return None
        e1, e2, v0 = self.e1[near], self.e2[near], self.v0[near]
        p = np.cross(direction, e2)
        det = np.einsum("ij,ij->i", e1, p)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        s = origin - v0
        u = np.einsum("ij,ij->i", s, p) * inv
        qv = np.cross(s, e1)
        v = (qv @ direction) * inv
        t = np.einsum("ij,ij->i", e2, qv) * inv
        good = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9)
        if not good.any():
            return None
        return origin + direction * float(t[good].min())

    def view(self, cam: dict, size) -> "View":
        """Ready to cast many rays from one placed photo's camera."""
        return View(self, cam, size)


class View:
    """A mesh as one placed photo's camera sees it: each triangle's bounding sphere projected into the
    photo once, and filed by where it lands, so that a ray through a pixel is only tested against the
    triangles whose spheres cover it."""

    def __init__(self, mesh: Mesh, cam: dict, size):
        import numpy as np
        self.mesh, self.cam, self.size = mesh, cam, size
        R, t = _rotation(cam["q"]), np.asarray(cam["t"], float)
        p = mesh.c @ R.T + t
        z = p[:, 2]
        front = np.flatnonzero(z + mesh.r > 0)
        z, r = np.maximum(z[front], 1e-9), mesh.r[front]
        x, y = p[front, 0] / z, p[front, 1] / z
        # generously: a sphere off the axis looks wider, by up to 1 + x^2 + y^2, and one close by more than r/z
        rad = np.where(z > 2 * r, r / np.maximum(z - r, 1e-9) * (1 + x ** 2 + y ** 2), np.inf)
        corners = [normalized(cam, size, uv) for uv in ((0, 0), (size[0], 0), (0, size[1]), size)]
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        seen = (x + rad >= min(xs)) & (x - rad <= max(xs)) & (y + rad >= min(ys)) & (y - rad <= max(ys))
        front, x, y, rad = front[seen], x[seen], y[seen], rad[seen]
        finite = rad[np.isfinite(rad)]
        self.CELL = max(float(np.quantile(finite, 0.98)), 1e-4) if finite.size else 1.0
        small = rad <= self.CELL
        self.big = front[~small]
        ids = self._cell(x[small], y[small])
        order = np.argsort(ids)
        self.ids, self.tri = ids[order], front[small][order]
        self.origin = -R.T @ t
        self.R = R

    def _cell(self, x, y):
        import numpy as np
        return np.floor(np.asarray(x) / self.CELL).astype(np.int64) * 1_000_003 + np.floor(np.asarray(y) / self.CELL).astype(np.int64)

    def hit(self, uv, normal: bool = False):
        """Where the ray through pixel uv meets the mesh, and its depth along the camera's axis, or (None, None);
        with normal, also the unit normal of the triangle it meets, or None."""
        import numpy as np
        x, y = normalized(self.cam, self.size, uv)
        keys = [self._cell(x + dx * self.CELL, y + dy * self.CELL) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
        lo, hi = np.searchsorted(self.ids, keys, "left"), np.searchsorted(self.ids, keys, "right")
        near = np.concatenate([self.big, *(self.tri[a:b] for a, b in zip(lo, hi))])
        if not near.size:
            return (None, None, None) if normal else (None, None)
        d = self.R.T @ np.array([x, y, 1.0])
        d /= np.linalg.norm(d)
        m = self.mesh
        e1, e2, v0 = m.e1[near], m.e2[near], m.v0[near]
        p = np.cross(d, e2)
        det = np.einsum("ij,ij->i", e1, p)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        s = self.origin - v0
        u = np.einsum("ij,ij->i", s, p) * inv
        qv = np.cross(s, e1)
        v = (qv @ d) * inv
        t = np.einsum("ij,ij->i", e2, qv) * inv
        good = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9)
        if not good.any():
            return (None, None, None) if normal else (None, None)
        i = int(np.argmin(np.where(good, t, np.inf)))
        X = self.origin + d * float(t[i])
        if not normal:
            return X, depth(self.cam, X)
        n = np.cross(e1[i], e2[i])
        return X, depth(self.cam, X), n / max(np.linalg.norm(n), 1e-12)


# ------------------------------------------------------------------ contacts and holds
def contacts(track: list, still: float, min_frames: int) -> list[dict]:
    """Where a hand or foot stayed still on the mesh.

    Args:
        track: [(t, point or None)], in time order.
        still: largest move between frames that counts as still.
        min_frames: how many frames in a row it has to be still for.

    Returns:
        [{t0, t1, p}], p the median point of the run.
    """
    import numpy as np
    out, run = [], []

    def close():
        if len(run) >= min_frames:
            out.append({"t0": run[0][0], "t1": run[-1][0], "p": [float(x) for x in np.median([p for _, p in run], axis=0)]})

    for t, p in track:
        if p is None:
            close()
            run = []
            continue
        p = np.asarray(p, float)
        if run and np.linalg.norm(p - run[-1][1]) > still:
            close()
            run = []
        run.append((t, p))
    close()
    return out


def holds(found: dict[str, list[dict]], radius: float, ground: float | None = None, ground_margin: float = 0.0) -> list[dict]:
    """Contacts of each limb grouped into holds, each given a role.

    Args:
        found: contacts() of each limb, by LIMBS key.
        radius: contacts closer than this are the same hold.
        ground: feet below ground + ground_margin are standing on the ground, not on a hold.

    Returns:
        [{id, role, p, contacts: [{limb, t0, t1}]}], in the order they were first touched.
    """
    import numpy as np
    events = sorted((c["t0"], limb, c) for limb, cs in found.items() for c in cs
                    if not (LIMBS[limb][0] == "foot" and ground is not None and c["p"][1] < ground + ground_margin))
    groups: list[dict] = []
    for _, limb, c in events:
        p = np.asarray(c["p"])
        near = min(groups, key=lambda g: np.linalg.norm(g["_p"] - p), default=None)
        if near is None or np.linalg.norm(near["_p"] - p) > radius:
            near = {"_ps": [], "contacts": []}
            groups.append(near)
        near["_ps"].append(p)
        near["_p"] = np.mean(near["_ps"], axis=0)
        near["contacts"].append({"limb": limb, "t0": c["t0"], "t1": c["t1"]})
    hands = [(c["t0"], c["t1"], c["limb"], i) for i, g in enumerate(groups) for c in g["contacts"] if LIMBS[c["limb"]][0] == "hand"]
    first = {min((x for x in hands if x[2] == limb), default=None) for limb in ("lh", "rh")} - {None}
    last = {max((x for x in hands if x[2] == limb), key=lambda x: x[1], default=None) for limb in ("lh", "rh")} - {None}
    starts, finishes = {x[3] for x in first}, {x[3] for x in last}
    out = []
    for i, g in enumerate(groups):
        handed = any(LIMBS[c["limb"]][0] == "hand" for c in g["contacts"])
        role = "start" if i in starts else "finish" if i in finishes else "hand" if handed else "foot"
        out.append({"id": i, "role": role, "p": [round(float(x), 6) for x in g["_p"]], "contacts": g["contacts"]})
    return out


# ------------------------------------------------------------------ people in frames
def limb_points(kp, sc) -> dict:
    """Each hand's and foot's point of contact in a frame, from one person's keypoints, or None."""
    import numpy as np
    out = {}
    for limb, (kind, a, b) in LIMBS.items():
        if kind == "hand":
            ok = sc[a] >= KP_MIN and sc[b] >= KP_MIN
            out[limb] = tuple(kp[a] + HAND_REACH * (kp[a] - kp[b])) if ok else None
        else:   # the big toe where it's found, as feet stand on their toes; the ankle otherwise
            out[limb] = tuple(kp[b]) if sc[b] >= KP_MIN else tuple(kp[a]) if sc[a] >= KP_MIN else None
    return {k: (None if v is None else (float(v[0]), float(v[1]))) for k, v in out.items()}


def climber(kps, scs, previous=None) -> int | None:
    """Which of the people in a frame is the climber: the largest found well, or nearest where they were."""
    import numpy as np
    best, best_key = None, None
    for i, (kp, sc) in enumerate(zip(kps, scs)):
        good = sc >= KP_MIN
        if good.sum() < 8:
            continue
        pts = kp[good]
        area = float(np.prod(pts.max(axis=0) - pts.min(axis=0)))
        centre = pts.mean(axis=0)
        key = (area / (1 + (np.linalg.norm(centre - previous) / np.sqrt(area + 1)) ** 2)) if previous is not None else area
        if best_key is None or key > best_key:
            best, best_key = i, key
    return best


# ------------------------------------------------------------------ one video
def frames_of(url: str, tmp: Path, log: Log) -> list[tuple[float, Path]]:
    """FPS frames a second of a video, up to MAX_SECONDS, at up to 720p."""
    from climbs import youtube
    from climbs.common import jobctl
    import subprocess
    tmp.mkdir(parents=True, exist_ok=True)
    youtube.call(["yt-dlp", "-q", "--no-playlist", "-f", "bv*[height<=720]/b[height<=720]/b", "--max-filesize", "300m",
                  "-o", str(tmp / "video.%(ext)s"), url], timeout=900, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    video = next(p for p in tmp.glob("video.*") if not p.name.endswith((".json", ".part")))
    jobctl.run(["ffmpeg", "-loglevel", "error", "-t", str(MAX_SECONDS), "-i", str(video), "-vf", f"fps={FPS},scale=-2:'min(720,ih)'",
                "-q:v", "3", str(tmp / "f%05d.jpg")], timeout=900)
    video.unlink()
    return [((i + 0.5) / FPS, p) for i, p in enumerate(sorted(tmp.glob("f*.jpg")))]


class Matcher:
    """Finds which of a video's placed frames a frame of it matches, and maps points into that frame."""

    def __init__(self, placed: dict[str, Path]):
        import cv2
        self.orb = cv2.ORB_create(2000)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.refs, self.last = {}, None
        for name, path in placed.items():
            im = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if im is None:
                continue
            s = 960 / max(im.shape)
            small = cv2.resize(im, None, fx=s, fy=s) if s < 1 else im
            kp, des = self.orb.detectAndCompute(small, None)
            if des is not None:
                self.refs[name] = ([(p.pt[0] / min(s, 1), p.pt[1] / min(s, 1)) for p in kp], des, (im.shape[1], im.shape[0]))

    def _try(self, name, pts, des):
        import cv2
        import numpy as np
        rpts, rdes, _ = self.refs[name]
        pairs = [m for m, n in (x for x in self.bf.knnMatch(des, rdes, k=2) if len(x) == 2) if m.distance < 0.75 * n.distance]
        if len(pairs) < MIN_INLIERS:
            return 0, None
        H, inl = cv2.findHomography(np.float32([pts[m.queryIdx] for m in pairs]), np.float32([rpts[m.trainIdx] for m in pairs]), cv2.RANSAC, 6.0)
        n = int(inl.sum()) if inl is not None else 0
        return n, H

    def match(self, gray, person_box=None):
        """The placed frame a frame matches, and the homography into it, or (None, None).

        Args:
            person_box: (x0, y0, x1, y1) to leave out, as the climber moves and the rock doesn't.
        """
        import numpy as np
        mask = np.full(gray.shape, 255, np.uint8)
        if person_box is not None:
            x0, y0, x1, y1 = (int(v) for v in person_box)
            mask[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
        kp, des = self.orb.detectAndCompute(gray, mask)
        if des is None or len(kp) < MIN_INLIERS:
            return None, None
        pts = [p.pt for p in kp]
        if self.last in self.refs:
            n, H = self._try(self.last, pts, des)
            if n >= 2 * MIN_INLIERS:
                return self.last, H
        best = (0, None, None)
        for name in self.refs:
            n, H = self._try(name, pts, des)
            if n > best[0]:
                best = (n, name, H)
        if best[0] < MIN_INLIERS:
            return None, None
        self.last = best[1]
        return best[1], best[2]


def _pose_model(log: Log):
    from rtmlib import BodyWithFeet, PoseTracker
    from rtmlib.tools.file import download_checkpoint
    from climbs.common import CACHE_DIR
    where = str(CACHE_DIR.parent / "models" / "rtmlib")
    det, pose = download_checkpoint(POSE["det"], where), download_checkpoint(POSE["pose"], where)
    log(f"beta: pose model {Path(pose).name}")
    # the tracker makes its own model, from whatever it's given to call: here, one with these files
    model = functools.partial(BodyWithFeet, det=det, pose=pose, det_input_size=POSE["det_input_size"],
                              pose_input_size=POSE["pose_input_size"])
    return PoseTracker(model, det_frequency=DET_EVERY, backend="onnxruntime", device="cpu")


def analyse_video(url: str, placed: dict[str, dict], photos: Path, mesh: Mesh, tracker, tmp: Path, log: Log,
                  progress: Callable[[str], None] = lambda s: None) -> dict:
    """The holds a video's climber used on a mesh.

    Args:
        placed: the video's frames placed in the mesh: their cameras.json entries, by file name.
        photos: where those frames are.

    Returns:
        {frames, with_climber, placed_frames, approximate_lens, holds, tracks, reference, poses}: tracks are
        each limb's [t, x, y, z] on the mesh, reference the placed frame most matched, poses the climber in
        each frame they're found in: {t, kp, sc, ref, H}, ref and H None where it matched no placed frame.
    """
    import cv2
    import numpy as np
    from climbs.common import jobctl
    frames = frames_of(url, tmp, log)
    matcher = Matcher({n: photos / n for n in placed})
    sizes = {n: v[2] for n, v in matcher.refs.items()}
    tracker.reset()
    tracks = {limb: [] for limb in LIMBS}
    used, seen, prev, views, poses = {}, 0, None, {}, []
    for i, (t, path) in enumerate(frames):
        jobctl.check()
        if i % 10 == 0:
            progress(f"frame {i + 1} of {len(frames)}")
        im = cv2.imread(str(path))
        kps, scs = tracker(im)
        who = climber(kps, scs, prev) if len(kps) else None
        points = {limb: None for limb in LIMBS}
        if who is not None:
            seen += 1
            good = scs[who] >= KP_MIN
            prev = kps[who][good].mean(axis=0)
            lo, hi = kps[who][good].min(axis=0), kps[who][good].max(axis=0)
            pad = 0.15 * (hi - lo)
            name, H = matcher.match(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), (*(lo - pad), *(hi + pad)))
            poses.append({"t": round(t, 2), "kp": [[round(float(a), 1), round(float(b), 1)] for a, b in kps[who]],
                          "sc": [round(float(x), 3) for x in scs[who]], "ref": name,
                          "H": None if H is None else [round(float(x), 7) for x in np.asarray(H).ravel()]})
            if name is not None:
                used[name] = used.get(name, 0) + 1
                if name not in views:
                    views[name] = mesh.view(placed[name], sizes[name])
                for limb, uv in limb_points(kps[who], scs[who]).items():
                    if uv is None:
                        continue
                    q = cv2.perspectiveTransform(np.float32([[uv]]), H)[0][0]
                    points[limb] = views[name].hit(q)[0]
        for limb in LIMBS:
            tracks[limb].append((t, points[limb]))
        path.unlink()
    shutil.rmtree(tmp, ignore_errors=True)
    min_frames = max(2, int(round(HOLD_SECONDS * FPS)))
    found = {limb: contacts(tr, STILL * mesh.size, min_frames) for limb, tr in tracks.items()}
    result = holds(found, HOLD_RADIUS * mesh.size, mesh.ground, GROUND * mesh.size)
    reference = max(used, key=used.get) if used else None
    return {"frames": len(frames), "with_climber": seen, "placed_frames": sum(used.values()),
            "approximate_lens": any(lens(placed[n], sizes[n])[4] for n in used), "reference": reference, "holds": result,
            "tracks": {limb: [[round(t, 2), *(round(float(x), 5) for x in p)] for t, p in tr if p is not None] for limb, tr in tracks.items()},
            "poses": poses}


def annotate(photo: Path, cam: dict, result: dict, out: Path) -> None:
    """Draw a video's tracks and holds on one of its placed frames."""
    import cv2
    im = cv2.imread(str(photo))
    size = (im.shape[1], im.shape[0])
    for limb, tr in result["tracks"].items():
        pts = [project(cam, size, p[1:]) for p in tr]
        colour = (235, 235, 235) if LIMBS[limb][0] == "hand" else (60, 200, 255)
        for a, b in zip(pts, pts[1:]):
            if a and b:
                cv2.line(im, tuple(map(int, a)), tuple(map(int, b)), colour, max(1, size[0] // 640), cv2.LINE_AA)
    r = max(8, size[0] // 75)     # open rings, as the viewer draws them
    w = max(2, r // 4)
    for h in result["holds"]:
        uv = project(cam, size, h["p"])
        if uv:
            cv2.circle(im, tuple(map(int, uv)), r, (20, 20, 20), w + 3, cv2.LINE_AA)
            cv2.circle(im, tuple(map(int, uv)), r, COLORS[h["role"]], w, cv2.LINE_AA)
    s = 960 / max(size)
    if s < 1:
        im = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(out), im, [cv2.IMWRITE_JPEG_QUALITY, 85])


# ------------------------------------------------------------------ a mesh
def videos(mesh_dir: Path) -> dict[str, dict]:
    """A mesh's videos with frames placed in it: {url: {tag, title, uploader, frames: {name: camera}}}."""
    import provenance
    record = provenance.load(mesh_dir)
    cams = mesh_dir / "cameras.json"
    if not record or not cams.exists():
        return {}
    placed = {c["name"]: c for c in json.loads(cams.read_text())}
    out = {}
    for p in record["photos"]:
        if p.get("source") == "youtube" and p.get("url") and p["file"] in placed:
            v = out.setdefault(p["url"], {"tag": hashlib.sha1(p["url"].encode()).hexdigest()[:8], "title": p.get("title"),
                                           "uploader": p.get("credit"), "frames": {}})
            v["frames"][p["file"]] = placed[p["file"]]
    return out


def save(mesh_dir: Path, record: dict) -> None:
    tmp = mesh_dir / (FILE + ".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(mesh_dir / FILE)


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((mesh_dir / FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def summary(record: dict | None) -> dict | None:
    """For the list of meshes: how far finding it got, and what it found."""
    if not record:
        return None
    return {"status": record["status"], "progress": record.get("progress"), "note": record.get("note"),
            "waiting": record.get("waiting"), "videos": len(record["videos"]), "holds": sum(len(v.get("holds") or []) for v in record["videos"])}


def carry_on(mesh_dir: Path) -> None:
    """Queue again a search that a restart stopped, as finding beta carries on after one."""
    record = load(mesh_dir)
    if record and record["status"] == "running":
        record.update(status="queued", progress=None, resume=True)
        save(mesh_dir, record)


def find(mesh_dir: Path, photos: Path, log: Log) -> dict:
    """Find the beta in each of a mesh's videos, saving beta.json as it goes, so the page can follow it.
    Videos already done by a search that YouTube's refusal or a restart stopped partway aren't done again.

    Returns:
        The record saved.

    Raises:
        youtube.Refused: YouTube is refusing downloads; beta.json says "waiting", with the videos done so far.
    """
    from climbs import youtube
    found = videos(mesh_dir)
    before = load(mesh_dir) or {}
    kept = [v for v in before.get("videos", []) if before.get("resume") and v["url"] in found and v["status"] in ("done", "no holds found")]
    record = {"status": "running", "started": time.time(), "videos": kept, "progress": None}
    if not found:
        record.update(status="done", note="none of its photos are frames of videos, or where they were placed wasn't kept")
        save(mesh_dir, record)
        return record
    save(mesh_dir, record)
    mesh = Mesh.load(mesh_dir)
    tracker = _pose_model(log)
    try:
        poses = {u: p for u, p in json.loads((mesh_dir / POSES_FILE).read_text()).items() if u in {v["url"] for v in kept}}
    except (FileNotFoundError, json.JSONDecodeError):
        poses = {}
    for n, (url, v) in enumerate(found.items()):
        if url in {k["url"] for k in kept}:
            continue
        entry = {"url": url, "tag": v["tag"], "title": v["title"], "uploader": v["uploader"], "placed": len(v["frames"])}

        def progress(s, n=n):
            record["progress"] = f"video {n + 1} of {len(found)}: {s}"
            save(mesh_dir, record)
        progress("downloading")
        try:
            result = analyse_video(url, v["frames"], photos, mesh, tracker, mesh_dir / "beta_tmp", log, progress)
            poses[url] = result.pop("poses")
            (mesh_dir / POSES_FILE).write_text(json.dumps(poses))
            entry.update(result, status="done" if result["holds"] else "no holds found")
            if result["reference"]:
                annotate(photos / result["reference"], v["frames"][result["reference"]], result, mesh_dir / f"beta_{v['tag']}.jpg")
                entry["image"] = f"beta_{v['tag']}.jpg"
            log(f"beta: {url}: {len(result['holds'])} holds, climber in {result['with_climber']} of {result['frames']} frames, "
                f"{result['placed_frames']} placed")
        except youtube.Refused as e:
            log(f"beta: {e}: waiting")
            record.update(status="queued", progress=None, waiting=e.state["until"], resume=True)
            save(mesh_dir, record)
            raise
        except Exception as e:
            entry.update(status=f"failed: {type(e).__name__}: {e}")
            log(f"beta: {url} failed: {e}")
        finally:
            shutil.rmtree(mesh_dir / "beta_tmp", ignore_errors=True)
        record["videos"].append(entry)
        save(mesh_dir, record)
    record.update(status="done", progress=None, finished=time.time())
    save(mesh_dir, record)
    return record


def load_holds(mesh_dir: Path) -> dict:
    try:
        return json.loads((mesh_dir / HOLDS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"holds": []}


def save_holds(mesh_dir: Path, marked: list) -> dict:
    """Keep holds marked by hand, checking each is a point and a role."""
    out = []
    for h in marked:
        p, role = h.get("p"), h.get("role")
        if role not in ROLES or not isinstance(p, list) or len(p) != 3 or not all(isinstance(x, (int, float)) for x in p):
            raise ValueError("each hold needs p: [x, y, z] and a role: " + ", ".join(ROLES))
        out.append({"p": [float(x) for x in p], "role": role})
    record = {"holds": out, "updated": time.time()}
    (mesh_dir / HOLDS_FILE).write_text(json.dumps(record))
    return record
