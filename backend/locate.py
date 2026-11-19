"""Placing a new picture of a rock against a mesh's own photos, for lining the mesh up in the capture apps with no taps
(app/shared/.../core/Locate.kt): where the camera was, in the mesh's coordinates.

A mesh's locate pack holds the points of its sparse model (reconstruction.tar.gz's, already in the mesh's frame) with
SIFT descriptors of them from OpenCV, which the phones run too, so that a live frame's features match the pack's
whichever side does the matching. COLMAP's own descriptors aren't kept with the mesh (reconstruction.py), and OpenCV's
are what a phone can make: each placed photo's OpenCV keypoints are tied to the model's points through where COLMAP saw
those points in that photo (images.bin's observations), within a couple of pixels.

    <out>.bin    little-endian: b"BMLP", u32 version (1), u32 points, u32 entries, u32 photos;
                 f32 xyz per point (mesh coordinates); u32 point per entry; u16 photo per entry;
                 128 u8 per entry (RootSIFT, as COLMAP stores SIFT: L1-normalised, square-rooted, times 512)
    <out>.json   {v, points, entries, photos: [{name, c}], max_per_photo, max_points, opencv, made, seconds}

An entry is one photo's descriptor of one point; a point seen in several photos has several (up to PER_POINT).
place() matches a frame's features against the entries, keeps the best match to each point that a match to another
point doesn't nearly equal (Lowe's ratio), and finds the camera with PnP-RANSAC.
"""
from __future__ import annotations

import json
import struct
import tarfile
import tempfile
import time
from pathlib import Path

import orientation

MAGIC = b"BMLP"
VERSION = 1
PER_POINT = 3           # descriptors kept of each point, from as many photos
TIE_PX = 2.0            # an OpenCV keypoint this close to where COLMAP saw a point, in the photo's pixels, is of it
MIN_INLIERS = 25        # Locate.kt's MIN_INLIERS: fewer agreeing matches than this place nothing


class LocateError(Exception):
    """A mesh can't have a locate pack: it kept no reconstruction, or none of its photos are here."""


# ---- reading the model ----------------------------------------------------------------------------------------------

def _read_points(model: Path) -> dict[int, tuple[float, float, float, int]]:
    """points3D.bin: by point id, its x, y, z and how many photos saw it."""
    out = {}
    with open(model / "points3D.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            pid, x, y, z = struct.unpack("<Q3d", f.read(32))
            f.read(3 + 8)                                           # colour, error
            n = struct.unpack("<Q", f.read(8))[0]
            f.read(8 * n)                                           # track: (image id, feature index) each
            out[pid] = (x, y, z, n)
    return out


def _read_images(model: Path) -> list[dict]:
    """images.bin: each photo's name, camera id, world-to-camera q (w, x, y, z) and t, and where it saw which points
    (x, y in COLMAP's pixels, whose first pixel's centre is at 0.5; point id)."""
    import numpy as np
    out = []
    with open(model / "images.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(4)
            q = struct.unpack("<4d", f.read(32)); t = struct.unpack("<3d", f.read(24))
            cam = struct.unpack("<i", f.read(4))[0]
            name = b""
            while (c := f.read(1)) != b"\0":
                name += c
            n = struct.unpack("<Q", f.read(8))[0]
            obs = np.frombuffer(f.read(24 * n), dtype=np.dtype([("x", "<f8"), ("y", "<f8"), ("p", "<i8")]))
            obs = obs[obs["p"] != -1]
            out.append({"name": name.decode(), "cam": cam, "q": q, "t": t, "obs": obs})
    return out


def _rotation(q):
    import numpy as np
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


# ---- features ----------------------------------------------------------------------------------------------------------

def _sift(grey, max_features: int):
    """OpenCV SIFT keypoints (x, y, OpenCV's pixels: first pixel's centre at 0) and RootSIFT descriptors (uint8, as
    COLMAP's), the strongest max_features."""
    import cv2
    import numpy as np
    sift = cv2.SIFT_create(nfeatures=max_features)
    kps, desc = sift.detectAndCompute(grey, None)
    if desc is None or not len(kps):
        return np.zeros((0, 2), np.float32), np.zeros((0, 128), np.uint8)
    xy = np.array([k.pt for k in kps], np.float32)
    return xy, root_sift(desc)


def root_sift(desc):
    """SIFT descriptors as COLMAP keeps them: L1-normalised, square-rooted, times 512, clipped to a byte."""
    import numpy as np
    d = desc.astype(np.float32)
    d /= np.maximum(d.sum(axis=1, keepdims=True), 1e-12)
    return np.clip(np.round(np.sqrt(d) * 512), 0, 255).astype(np.uint8)


def _grey(path: Path):
    import cv2
    return cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)


# ---- the pack ------------------------------------------------------------------------------------------------------------

def build_pack(mesh_dir: Path, photo_dirs: list[Path], out: str = "locate", max_per_photo: int = 1500,
               max_points: int = 30000, exclude: set[str] | None = None) -> dict:
    """Make a mesh's locate pack, beside it, from its kept reconstruction and its photos.

    Args:
        mesh_dir: the mesh's folder (reconstruction.tar.gz in it).
        photo_dirs: where its photos are, searched in order by file name (a pooled mesh's are in several projects).
        out: the pack's name: <out>.bin and <out>.json.
        max_per_photo: OpenCV features taken from each photo, the strongest.
        max_points: points kept, those seen in the most photos first.
        exclude: photos left out (to test placing them against the rest).

    Returns:
        What <out>.json holds.

    Raises:
        LocateError: no reconstruction, or none of its placed photos found.
    """
    import cv2
    import numpy as np
    started = time.time()
    rec = mesh_dir / "reconstruction.tar.gz"
    if not rec.is_file():
        raise LocateError("no reconstruction")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(rec, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.name in ("model/points3D.bin", "model/images.bin", "model/cameras.bin")]
            tar.extractall(tmp, members=members, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        model = Path(tmp) / "model"
        points = _read_points(model)
        images = _read_images(model)
        lenses = orientation.read_lenses(model)

    kept_ids = sorted(points, key=lambda p: -points[p][3])[:max_points]
    index = {pid: i for i, pid in enumerate(kept_ids)}
    entries: dict[int, list[tuple[int, int, np.ndarray, float]]] = {}       # point -> (photo, -, descriptor, response)
    photos, found = [], 0
    for img in images:
        if exclude and img["name"] in exclude:
            continue
        path = next((d / img["name"] for d in photo_dirs if (d / img["name"]).is_file()), None)
        R = _rotation(img["q"]); c = -R.T @ np.array(img["t"])
        photos.append({"name": img["name"], "c": [round(float(v), 5) for v in c]})
        if path is None or not len(img["obs"]):
            continue
        grey = _grey(path)
        if grey is None:
            continue
        found += 1
        lens = lenses.get(img["cam"], {})
        # COLMAP's pixels are of the photo as it was placed; this copy may be another size
        sx = grey.shape[1] / lens["width"] if lens.get("width") else 1.0
        sy = grey.shape[0] / lens["height"] if lens.get("height") else 1.0
        obs = img["obs"]
        seen = np.stack([(obs["x"] - 0.5) * sx, (obs["y"] - 0.5) * sy], axis=1)
        sift = cv2.SIFT_create(nfeatures=max_per_photo)
        kps, desc = sift.detectAndCompute(grey, None)
        if desc is None:
            continue
        xy = np.array([k.pt for k in kps], np.float32)
        desc = root_sift(desc)
        photo = len(photos) - 1
        best: dict[int, tuple[float, int]] = {}         # one keypoint per point in a photo: the closest
        for k, (d, j) in enumerate(_nearest(seen, xy, TIE_PX * max(sx, sy))):
            if j < 0:
                continue
            pid = int(obs["p"][j])
            if pid in index and (pid not in best or d < best[pid][0]):
                best[pid] = (float(d), k)
        for pid, (_, k) in best.items():
            entries.setdefault(index[pid], []).append((photo, 0, desc[k], kps[k].response))
    if not found:
        raise LocateError("none of its photos are here")

    pt_idx, ph_idx, descs = [], [], []
    for p, es in entries.items():
        for photo, _, d, _ in sorted(es, key=lambda e: -e[3])[:PER_POINT]:
            pt_idx.append(p); ph_idx.append(photo); descs.append(d)
    used = sorted(set(pt_idx))
    renum = {p: i for i, p in enumerate(used)}
    xyz = np.array([points[kept_ids[p]][:3] for p in used], np.float32).reshape(-1, 3)
    pt = np.array([renum[p] for p in pt_idx], np.uint32)
    ph = np.array(ph_idx, np.uint16)
    d = np.array(descs, np.uint8).reshape(-1, 128)
    with open(mesh_dir / f"{out}.bin.tmp", "wb") as f:
        f.write(MAGIC + struct.pack("<4I", VERSION, len(xyz), len(pt), len(photos)))
        f.write(xyz.astype("<f4").tobytes()); f.write(pt.astype("<u4").tobytes()); f.write(ph.astype("<u2").tobytes())
        f.write(d.tobytes())
    (mesh_dir / f"{out}.bin.tmp").replace(mesh_dir / f"{out}.bin")
    info = {"v": VERSION, "points": len(xyz), "entries": len(pt), "photos": photos, "photos_found": found,
            "max_per_photo": max_per_photo, "max_points": max_points, "opencv": cv2.__version__,
            "made": time.time(), "seconds": round(time.time() - started, 1)}
    (mesh_dir / f"{out}.json").write_text(json.dumps(info))
    return info


def _nearest(at, xy, reach: float) -> list[tuple[float, int]]:
    """For each of xy, the nearest of `at` within reach and how far: (distance, index), or (inf, -1)."""
    import numpy as np
    cells: dict[tuple[int, int], list[int]] = {}
    for i, (x, y) in enumerate(at):
        cells.setdefault((int(x // reach), int(y // reach)), []).append(i)
    out = []
    for x, y in xy:
        cx, cy = int(x // reach), int(y // reach)
        best = (float("inf"), -1)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in cells.get((cx + dx, cy + dy), ()):
                    d = float(np.hypot(at[i][0] - x, at[i][1] - y))
                    if d <= reach and d < best[0]:
                        best = (d, i)
        out.append(best)
    return out


def load_pack(path: Path) -> dict:
    """A pack's .bin, as numpy arrays: xyz (points, 3), point and photo (entries), desc (entries, 128)."""
    import numpy as np
    raw = Path(path).read_bytes()
    if raw[:4] != MAGIC:
        raise LocateError(f"{path} isn't a locate pack")
    v, n, e, nph = struct.unpack("<4I", raw[4:20])
    if v != VERSION:
        raise LocateError(f"{path}: pack version {v}, not {VERSION}")
    o = 20
    xyz = np.frombuffer(raw, "<f4", n * 3, o).reshape(n, 3); o += 12 * n
    point = np.frombuffer(raw, "<u4", e, o); o += 4 * e
    photo = np.frombuffer(raw, "<u2", e, o); o += 2 * e
    desc = np.frombuffer(raw, np.uint8, e * 128, o).reshape(e, 128)
    return {"xyz": xyz, "point": point, "photo": photo, "desc": desc, "photos": nph}


# ---- placing a frame -------------------------------------------------------------------------------------------------------

def place(pack: dict, grey, lens: dict, ratio: float = 0.8, top_k: int | None = None, iters: int = 2000,
          reproj_px: float = 4.0, max_features: int = 4000, dist=None) -> dict | None:
    """Where the camera that took `grey` was, against a pack.

    Args:
        pack: from load_pack().
        grey: the picture, one byte a pixel (rows as stored).
        lens: its pinhole lens, {fx, fy, cx, cy} in its pixels (OpenCV's: the first pixel's centre at 0).
        ratio: Lowe's ratio: a match is kept when the next best other point is further by more than this.
        top_k: matches kept only to the points of the top_k photos with most matches (None: all).
        iters: RANSAC's rounds.
        reproj_px: a match within this many pixels of where its point lands agrees.
        max_features: the picture's features taken, the strongest.
        dist: OpenCV distortion coefficients, if the picture has any.

    Returns:
        {R, t (world-to-camera, COLMAP's, mesh coordinates), inliers, rms_px, matched}, or None if it couldn't be
        placed with MIN_INLIERS agreeing matches.
    """
    import cv2
    import numpy as np
    xy, desc = _sift(grey, max_features)
    if len(xy) < MIN_INLIERS or not len(pack["desc"]):
        return None
    k = min(4, len(pack["desc"]))
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    knn = matcher.knnMatch(desc.astype(np.float32), pack["desc"].astype(np.float32), k=k)
    q_idx, p_idx, ph = [], [], []
    for qi, ms in enumerate(knn):
        if not ms:
            continue
        best = ms[0]; bp = int(pack["point"][best.trainIdx])
        other = next((m for m in ms[1:] if int(pack["point"][m.trainIdx]) != bp), None)
        if other is not None and best.distance > ratio * other.distance:
            continue
        q_idx.append(qi); p_idx.append(bp); ph.append(int(pack["photo"][best.trainIdx]))
    if top_k:
        counts = np.bincount(np.array(ph, int)) if ph else np.zeros(0)
        keep_photos = set(np.argsort(-counts)[:top_k].tolist())
        sel = [i for i, p in enumerate(ph) if p in keep_photos]
        q_idx = [q_idx[i] for i in sel]; p_idx = [p_idx[i] for i in sel]
    # one match per point: the first (the strongest feature's)
    seen, qs, ps = set(), [], []
    for q, p in zip(q_idx, p_idx):
        if p not in seen:
            seen.add(p); qs.append(q); ps.append(p)
    if len(qs) < MIN_INLIERS:
        return None
    obj = pack["xyz"][ps].astype(np.float64); img = xy[qs].astype(np.float64)
    K = np.array([[lens["fx"], 0, lens["cx"]], [0, lens["fy"], lens["cy"]], [0, 0, 1]], float)
    d = None if dist is None else np.asarray(dist, float)
    ok, rvec, tvec, inl = cv2.solvePnPRansac(obj, img, K, d, iterationsCount=iters, reprojectionError=reproj_px,
                                             confidence=0.999, flags=cv2.SOLVEPNP_AP3P)
    if not ok or inl is None or len(inl) < MIN_INLIERS:
        return None
    inl = inl.ravel()
    rvec, tvec = cv2.solvePnPRefineLM(obj[inl], img[inl], K, d, rvec, tvec)
    proj, _ = cv2.projectPoints(obj[inl], rvec, tvec, K, d)
    err = np.linalg.norm(proj.reshape(-1, 2) - img[inl], axis=1)
    R, _ = cv2.Rodrigues(rvec)
    return {"R": R.tolist(), "t": tvec.ravel().tolist(), "inliers": int(len(inl)),
            "rms_px": float(np.sqrt(np.mean(err ** 2))), "matched": len(qs)}
