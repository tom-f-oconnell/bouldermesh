"""Which way a mesh faces, from the skyline in its photos: where sky meets distant mountains, matched against the
horizon worked out from elevation data at its place. Only distant terrain will do (nearby rock, trees and slopes shift
from photo to photo), so it works in open country like the Buttermilks and not among Joshua Tree's domes or in a
forest; each step skips what can't work, cheapest first (experiments/skyline/README.md has the tests, and the costs):

  1. its place: the horizon all round, from AWS's Terrain Tiles (zoom 14 to 2.5 km, zoom 10 to 50 km), with the
     earth's curve; skipped if half of it is nearer than FAR_KM (about 1 s, no photos looked at)
  2. a few of its photos: SegFormer-b0 (ADE20K, ONNX, on the CPU) finds sky meeting mountain or hill, away from the
     mesh's own outline; skipped if none shows that (about 0.3 s a photo)
  3. the rest (a few frames of each video): the points that photos BASELINE m apart see at the same bearing and height,
     too far away to shift, matched by turning the mesh and tipping its up by as much as TILT° (a slightly wrong up,
     on a horizon that rises steadily, otherwise reads as several degrees of turn); kept if the best stands out, and
     two halves of the photos agree

check() does it for one mesh: (found | None, note). Tiles are kept in <cache>/terrarium; the model (15 MB, downloaded
once) in <cache>/models/skyline. SegFormer's weights are NVIDIA's, licensed for non-commercial use.
TERRAIN_OFFLINE=1 fetches nothing (for tests).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import beta
import meshclean

VERSION = 1             # meshes are checked again when this changes
TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
MODEL_URL = "https://huggingface.co/Xenova/segformer-b0-finetuned-ade-512-512/resolve/main/onnx/model.onnx"
MODEL_SHA256 = "3e5c18a4be395f16646438d54c42377ddc202edfa33d5eced0c9506de75c44c2"
MODEL_FILE = "segformer-b0-ade.onnx"
AGENT = "BoulderMesh (which way meshes face)"
NEAR_Z, NEAR_M, FAR_Z, FAR_M = 14, 2500, 10, 50000     # zoom and reach of the near and far elevation
STEP = 0.25             # degrees of bearing between horizon samples
EYE = 1.6               # metres above the ground a camera is taken to be
FAR_KM = 5.0            # step 1: half the horizon at least this far (7.4 km in the Buttermilks, at most 3.3 at Joshua Tree)
SKY, DISTANT = 2, {16, 68}      # ADE20K's sky, and its mountain and hill: what's below a skyline far enough away
SAMPLE = 6              # step 2: photos looked at first
FRAMES_PER_VIDEO = 6    # neighbouring frames of a video show the same skyline
LOWEST_TOP = -2.0       # degrees: a photo whose top edge is lower than this can't show the horizon
BASELINE, SAME = 8.0, 0.5       # metres between photos, and degrees, for a point to count as too far away to shift
SIGMA, ABOVE = 0.75, 0.15       # degrees a point is expected to fit to; the weight of one above the horizon (trees)
TILT = 4.0              # degrees the mesh's up may be tipped each way
MIN_PHOTOS, MIN_POINTS = 4, 100
MIN_MARGIN = 2.5        # the best other heading (45° or more away) must fit this many times worse
MOST_APART = 10.0       # degrees two halves of the photos may disagree by
_model_lock = threading.Lock()


class Unavailable(Exception):
    """Elevation data couldn't be had (offline, or the service failed)."""


# ------------------------------------------------------------------------------------------------ the horizon
def _fetch(url: str, tries: int = 3) -> bytes:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": AGENT}), timeout=60) as r:
                return r.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code < 500):
                raise Unavailable(f"{url}: {e}") from None
            time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


class Elevation:
    """Heights from Terrain Tiles (Terrarium PNGs: height = R * 256 + G + B / 256 - 32768 m), kept under cache/."""

    def __init__(self, cache: Path, fetch: Callable[[str], bytes] = _fetch):
        self.cache, self.fetch, self.tiles = cache, fetch, {}

    def tile(self, z: int, x: int, y: int):
        import numpy as np
        if (z, x, y) not in self.tiles:
            f = self.cache / str(z) / str(x) / f"{y}.png"
            if not f.exists():
                if os.environ.get("TERRAIN_OFFLINE") == "1":
                    raise Unavailable("offline")
                data = self.fetch(TILES.format(z=z, x=x, y=y))
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".partial")
                tmp.write_bytes(data)
                tmp.replace(f)
            from PIL import Image
            with Image.open(f) as im:
                a = np.asarray(im.convert("RGB"), np.float32)
            self.tiles[(z, x, y)] = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
        return self.tiles[(z, x, y)]

    def at(self, z: int, lat, lng):
        """Bilinear heights at points (arrays of degrees), from zoom z's tiles."""
        import numpy as np
        n = 2 ** z
        px = (lng + 180) / 360 * n * 256 - .5
        py = (1 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2 * n * 256 - .5
        x0, y0 = np.floor(px).astype(int), np.floor(py).astype(int)
        fx, fy = px - x0, py - y0
        out = np.zeros(px.shape)
        for dx, dy, w in ((0, 0, (1 - fx) * (1 - fy)), (1, 0, fx * (1 - fy)), (0, 1, (1 - fx) * fy), (1, 1, fx * fy)):
            gx, gy = (x0 + dx) % (n * 256), np.clip(y0 + dy, 0, n * 256 - 1)
            tx, ty = gx // 256, gy // 256
            for key in {(int(a), int(b)) for a, b in zip(tx.ravel(), ty.ravel())}:
                sel = (tx == key[0]) & (ty == key[1])
                out[sel] += w[sel] * self.tile(z, key[0], key[1])[gy[sel] % 256, gx[sel] % 256]
        return out

    def horizon(self, lat: float, lng: float):
        """The horizon's elevation angle (degrees) at each STEP of bearing from north, seen EYE m above the ground at
        (lat, lng), with the earth's curve and refraction; and how far away each is (m)."""
        import numpy as np
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
        return el.max(1), d[el.argmax(1)]


# ------------------------------------------------------------------------------------------------ the photos' skylines
def model_path(models: Path) -> Path:
    """The segmentation model, downloaded (and checked) the first time."""
    f = models / "skyline" / MODEL_FILE
    with _model_lock:
        if not f.exists():
            if os.environ.get("TERRAIN_OFFLINE") == "1":
                raise Unavailable("offline")
            data = _fetch(MODEL_URL)
            if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
                raise Unavailable(f"{MODEL_URL} isn't the model expected")
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".partial")
            tmp.write_bytes(data)
            tmp.replace(f)
    return f


class Segmenter:
    """ADE20K classes over a 512 x 512 grid laid over a photo, on as many threads as the calling thread may use."""

    def __init__(self, models: Path):
        import numpy as np
        import onnxruntime as ort
        opts = ort.SessionOptions()
        cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
        opts.intra_op_num_threads, opts.inter_op_num_threads = cores, 1
        self.sess = ort.InferenceSession(str(model_path(models)), opts, providers=["CPUExecutionProvider"])
        self.mean, self.std = np.array([0.485, 0.456, 0.406]), np.array([0.229, 0.224, 0.225])

    def labels(self, im):
        import numpy as np
        from PIL import Image
        x = ((np.asarray(im.resize((512, 512), Image.BILINEAR), np.float32) / 255 - self.mean) / self.std).transpose(2, 0, 1)[None]
        logits = self.sess.run(None, {"pixel_values": x.astype(np.float32)})[0][0]
        return np.asarray(Image.fromarray(logits.argmax(0).astype(np.uint8)).resize((512, 512), Image.NEAREST))


def mesh_points(mesh_dir: Path):
    """Up to 60,000 of a mesh's vertices, of what cleanup kept; None if it has no mesh file."""
    import numpy as np
    f = meshclean.mesh_file(mesh_dir)
    if f is None:
        return None
    j, bufs = meshclean._read_glb(f)
    V, F, _ = meshclean._geometry(j, bufs)
    clean = meshclean.load(mesh_dir)
    if clean and clean.get("removed") and (mesh_dir / meshclean.FACES_FILE).exists():
        labels = np.frombuffer((mesh_dir / meshclean.FACES_FILE).read_bytes(), {2: "<u2", 4: "<u4"}[clean["label_bytes"]])
        V = V[np.unique(F[~np.isin(labels, clean["removed"])])]
    return V[np.random.default_rng(0).choice(len(V), min(len(V), 60000), replace=False)]


def rock_mask(cam: dict, size, V):
    """Where the mesh is in a photo, on the 512 x 512 grid, grown by 4 cells: the rock's own outline isn't a skyline."""
    import numpy as np
    f, cx, cy, k, _ = beta.lens(cam, size)
    p = V @ beta._rotation(cam["q"]).T + np.asarray(cam["t"], float)
    p = p[p[:, 2] > 1e-6]
    x, y = p[:, 0] / p[:, 2], p[:, 1] / p[:, 2]
    r = 1 + k * (x * x + y * y)
    u, v = (f * x * r + cx) * 512 / size[0], (f * y * r + cy) * 512 / size[1]
    m = np.zeros((512, 512), bool)
    ok = (u >= 0) & (u < 512) & (v >= 0) & (v < 512)
    m[v[ok].astype(int), u[ok].astype(int)] = True
    for _ in range(4):
        m = m | np.roll(m, 1, 0) | np.roll(m, -1, 0) | np.roll(m, 1, 1) | np.roll(m, -1, 1)
    return m


def skyline_cells(lab, rock, every: int = 4) -> list[tuple[int, int]]:
    """(column, row) on the 512 grid where sky running down from the top edge meets at least 3 cells of mountain or
    hill, not where the mesh itself is."""
    import numpy as np
    out = []
    for c in range(0, 512, every):
        col = lab[:, c]
        if col[0] != SKY:
            continue
        r = int(np.argmax(col != SKY))
        if r < 5 or r > 500:
            continue
        if not all(int(b) in DISTANT for b in col[r:r + 3]) or rock[max(0, r - 2):r + 4, max(0, c - 2):c + 3].any():
            continue
        out.append((c, r))
    return out


def bearing(x: float, z: float) -> float:
    """The bearing, in the mesh's own frame, of a horizontal direction: -Z as north, +X as east."""
    return math.degrees(math.atan2(x, -z)) % 360


def ray_angles(cam: dict, size, uv) -> tuple[float, float]:
    """The bearing (in the mesh's frame) and elevation of the ray through pixel uv of a placed photo."""
    _, d = beta.camera_ray(cam, size, uv)
    return bearing(d[0], d[2]), math.degrees(math.asin(max(-1.0, min(1.0, float(d[1])))))


def chosen(cams: list[dict]) -> list[dict]:
    """The photos worth looking at: every still, and FRAMES_PER_VIDEO of each video's frames (yt_<video>_<n>), spread
    over it."""
    stills, videos = [], {}
    for c in cams:
        parts = c["name"].split("_")
        if c["name"].startswith("yt_") and len(parts) >= 3:
            videos.setdefault(parts[1], []).append(c)
        else:
            stills.append(c)
    out = list(stills)
    for frames in videos.values():
        frames.sort(key=lambda c: c["name"])
        k = len(frames)
        out += [frames[round(i * (k - 1) / max(1, FRAMES_PER_VIDEO - 1))] for i in range(min(k, FRAMES_PER_VIDEO))] if k > FRAMES_PER_VIDEO else frames
    return out


def far_only(obs: list, centres: list, metres: float) -> list:
    """Each photo's points that another photo, taken at least BASELINE m away, sees at the same bearing (within 0.5°)
    and height (within SAME°): too far away to shift between them, so terrain the elevation data can have."""
    import numpy as np
    keep = [np.zeros(len(o), bool) for o in obs]
    order = [np.argsort(o[:, 0]) for o in obs]
    for k, (pk, ck) in enumerate(zip(obs, centres)):
        for m, (pm, cm) in enumerate(zip(obs, centres)):
            if m == k or np.hypot(*(ck - cm)[[0, 2]]) * metres < BASELINE:
                continue
            az = pm[order[m], 0]
            i = np.clip(np.searchsorted(az, pk[:, 0]), 1, len(az) - 1)
            for j in (i - 1, i):
                gap = np.abs((az[j] - pk[:, 0] + 180) % 360 - 180)
                keep[k] |= (gap < 0.5) & (np.abs(pm[order[m][j], 1] - pk[:, 1]) < SAME)
    return [o[kp] for o, kp in zip(obs, keep)]


# ------------------------------------------------------------------------------------------------ matching
def match(obs: list, el):
    """The loss of each heading (each STEP of the horizon el), for photos' points ([n, 2]: bearing in the mesh's frame,
    elevation), each photo counting once, with the mesh's up tipped by the best of up to TILT° each way and an offset
    within ±1°; and for each heading, (offset, tip about the east axis, tip about the north axis)."""
    import numpy as np
    n = len(el)
    heads = np.arange(n)
    best, how = np.full(n, np.inf), np.zeros((n, 3))
    steps = np.arange(-TILT, TILT + 0.01, 1.0)
    idx = [(np.round(p[:, 0] / STEP).astype(int)[None, :] + heads[:, None]) % n for p in obs]
    for ta in steps:
        for tb in steps:
            if ta * ta + tb * tb > TILT * TILT + 0.01:
                continue
            tipped = [p[:, 1] - ta * np.sin(np.radians(p[:, 0])) - tb * np.cos(np.radians(p[:, 0])) for p in obs]
            for b in np.arange(-1, 1.01, 0.5):
                total = np.zeros(n)
                for e, ix in zip(tipped, idx):
                    r = e[None, :] - (el[ix] + b)
                    total += np.where(r < 0, np.minimum((r / SIGMA) ** 2, 9), ABOVE * np.minimum((r / SIGMA) ** 2, 9)).mean(1)
                better = total < best
                best[better] = total[better]
                how[better] = (b, ta, tb)
    return best / max(1, len(obs)), how


def margin_of(loss) -> tuple[float, float]:
    """How many times worse the best heading 45° or more from the best fits, and that heading."""
    import numpy as np
    i = int(loss.argmin())
    turn = np.abs((np.arange(len(loss)) - i + len(loss) / 2) % len(loss) - len(loss) / 2) * STEP
    far = np.flatnonzero(turn >= 45)
    j = int(far[loss[far].argmin()])
    return float(loss[j] / max(float(loss[i]), 1e-9)), j * STEP


def halves(obs: list, names: list[str], el) -> float | None:
    """How far apart the headings two halves of the photos give are (a video's frames kept together, the halves as
    even in points as can be); None if they're from fewer than two sources."""
    groups: dict[str, list[int]] = {}
    for k, n in enumerate(names):
        groups.setdefault(n.split("_")[1] if n.startswith("yt_") else n, []).append(k)
    if len(groups) < 2:
        return None
    sides, points = ([], []), [0, 0]
    for g in sorted(groups.values(), key=lambda g: -sum(len(obs[k]) for k in g)):
        s = int(points[1] < points[0])
        sides[s].extend(g)
        points[s] += sum(len(obs[k]) for k in g)
    found = [float(match([obs[k] for k in side], el)[0].argmin() * STEP) for side in sides]
    return abs((found[0] - found[1] + 180) % 360 - 180)


# ------------------------------------------------------------------------------------------------ one mesh
def check(mesh_dir: Path, photos_dir: Path, lat: float, lng: float, metres: float | None, cache: Path,
          log: Callable[[str], None] = print, fetch: Callable[[str], bytes] = _fetch,
          segmenter: "Segmenter | None" = None) -> tuple[dict | None, dict]:
    """Which way a mesh faces from its photos' skyline: {deg, photos, points, margin, ...} or None, and a note of how
    it went ({version, at, why: None if found, else why not, retry: if it's worth trying again later, seconds, ...})."""
    import numpy as np
    t0 = time.time()

    def note(why: str | None, **more) -> dict:
        return {"version": VERSION, "at": time.time(), "why": why, "seconds": round(time.time() - t0, 1), **more}
    if not metres:
        return None, note("its size isn't known", retry=True)
    try:
        el, dist = Elevation(cache / "terrarium", fetch).horizon(lat, lng)
    except Unavailable as e:
        return None, note(f"no elevation data ({e})", retry=True)
    km = round(float(np.median(dist)) / 1000, 1)
    if km < FAR_KM:
        return None, note(f"its horizon is mostly near (half within {km} km): no distant skyline to match", horizon_km=km)
    V = mesh_points(mesh_dir)
    try:
        cams = json.loads((mesh_dir / "cameras.json").read_text())
    except (OSError, ValueError):
        cams = []
    if V is None or not cams:
        return None, note("it has no mesh or placed photos to look at", horizon_km=km)
    todo = [c for c in chosen(cams) if (photos_dir / c["name"]).is_file()]
    first = [todo[round(i * (len(todo) - 1) / max(1, SAMPLE - 1))] for i in range(SAMPLE)] if len(todo) > SAMPLE else list(todo)
    todo = first + [c for c in todo if c not in first]
    seg = segmenter or Segmenter(cache / "models")
    from PIL import Image
    obs, centres, names, looked = [], [], [], 0
    for k, cam in enumerate(todo):
        if k == len(first) and not obs:
            return None, note(f"no distant skyline in the {looked} photos looked at first", horizon_km=km, looked=looked)
        try:
            with Image.open(photos_dir / cam["name"]) as im:
                size = im.size
                if ray_angles(cam, size, (size[0] / 2, 0))[1] < LOWEST_TOP:
                    continue
                im.draft("RGB", (1024, 1024))      # decoded smaller; the cells are scaled back to its full size
                lab = seg.labels(im.convert("RGB"))
        except OSError:
            continue
        looked += 1
        cells = skyline_cells(lab, rock_mask(cam, size, V))
        if len(cells) >= 8:
            obs.append(np.array([ray_angles(cam, size, ((c + 2) * size[0] / 512, r * size[1] / 512)) for c, r in cells]))
            names.append(cam["name"])
            centres.append(-beta._rotation(cam["q"]).T @ np.asarray(cam["t"], float))
    if not obs:
        return None, note(f"no distant skyline in its {looked} photos", horizon_km=km, looked=looked)
    kept = [(o, n) for o, n in zip(far_only(obs, centres, metres), names) if len(o) >= 5]
    points = sum(len(o) for o, _ in kept)
    if len(kept) < MIN_PHOTOS or points < MIN_POINTS:
        return None, note(f"too little distant skyline: {points} points in {len(kept)} photos, of the {looked} looked at",
                          horizon_km=km, looked=looked)
    kobs, knames = [o for o, _ in kept], [n for _, n in kept]
    loss, how = match(kobs, el)
    i = int(loss.argmin())
    deg = i * STEP
    margin, second = margin_of(loss)
    apart = halves(kobs, knames, el)
    found = {"deg": deg, "photos": len(kept), "points": points, "margin": round(margin, 2), "second": second,
             "halves_apart": None if apart is None else round(apart, 1), "offset": float(how[i][0]),
             "tilt": [float(how[i][1]), float(how[i][2])], "horizon_km": km,
             "detail": f"the skyline in {len(kept)} of its photos against the horizon from elevation data (to about 10°)"}
    log(f"skyline: facing {deg:.1f}° in {len(kept)} photos ({points} points), margin {margin:.1f}"
        + (f", halves {apart:.0f}° apart" if apart is not None else ""))
    if margin < MIN_MARGIN or (apart is not None and apart > MOST_APART):
        why = f"not clear: the best ({deg:.0f}°) fits only {margin:.1f} times better than {second:.0f}°" if margin < MIN_MARGIN \
            else f"not clear: two halves of its photos give headings {apart:.0f}° apart"
        return None, note(why, horizon_km=km, looked=looked, nearly=found)
    return found, note(None, horizon_km=km, looked=looked)
