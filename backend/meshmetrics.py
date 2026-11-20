"""Numbers about a mesh to sort and filter the Meshes tab by: how many faces it has, how many photos were placed and
how many went in, and how varied the places they were taken from are.

Placed photos alone overrate a mesh made from many frames of one video, taken from nearly the same spot. So each placed
photo also counts as one viewpoint shared with those taken within VIEW_DEG of the same direction from the rock: the
"distinct viewpoints" are the sum of 1/(how many share each one's), so a cluster of near-duplicates counts about once,
however many frames it has. "Around" is how much of a full circle round the rock, seen from above, its photos cover.
Both use cameras.json (where its photos were placed, orientation.py) and orientation.json's down. Kept beside the mesh
in metrics.json, worked out again when those change.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

FILE = "metrics.json"
VERSION = 2
VIEW_DEG = 15.0         # photos taken within this of the same direction from the rock count as one viewpoint
SOURCES = ("cameras.json", "cleanup.json", "orientation.json", "timings.json", "photo_sources.json")


def _rotation(q: list[float]) -> list[list[float]]:
    w, x, y, z = q
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _unit(v):
    n = math.sqrt(_dot(v, v)) or 1.0
    return [x / n for x in v]


def cameras(cams: list[dict]) -> list[tuple[list[float], list[float]]]:
    """Each placed photo's centre and the direction it looks, in the model's coordinates, from COLMAP's (q, t)
    (world to camera): the centre is -R^T t, and it looks along R^T z."""
    out = []
    for c in cams:
        try:
            r = _rotation(c["q"])
            t = c["t"]
            centre = [-sum(r[j][i] * t[j] for j in range(3)) for i in range(3)]
            out.append((centre, _unit([r[2][i] for i in range(3)])))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out


def _solve3(a: list[list[float]], b: list[float]) -> list[float] | None:
    """a x = b for a 3x3 a, or None if it's (nearly) singular."""
    det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
           + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    if abs(det) < 1e-9:
        return None
    out = []
    for k in range(3):
        m = [row[:] for row in a]
        for i in range(3):
            m[i][k] = b[i]
        out.append((m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                    + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])) / det)
    return out


def rock_centre(cams: list[tuple[list[float], list[float]]]) -> list[float] | None:
    """The point nearest every photo's line of sight (least squares), which is where they all look: the rock."""
    a = [[0.0] * 3 for _ in range(3)]
    b = [0.0] * 3
    for c, f in cams:
        for i in range(3):
            for j in range(3):
                m = (1.0 if i == j else 0.0) - f[i] * f[j]
                a[i][j] += m
                b[i] += m * c[j]
    return _solve3(a, b)


def distinct_viewpoints(dirs: list[list[float]], deg: float = VIEW_DEG) -> float:
    """Each direction counts 1/(how many lie within deg of it, itself too): about one per cluster of near-duplicates."""
    cos = math.cos(math.radians(deg))
    return sum(1.0 / sum(1 for e in dirs if _dot(d, e) >= cos) for d in dirs)


def around(dirs: list[list[float]], up: list[float]) -> float:
    """How many degrees of a full circle round the rock, seen from above, the directions cover: 360 less the widest
    gap between them."""
    up = _unit(up)
    ref = _unit(_sub([1.0, 0.0, 0.0], [up[0] * up[0], up[0] * up[1], up[0] * up[2]]) if abs(up[0]) < 0.9 else
                _sub([0.0, 0.0, 1.0], [up[2] * up[0], up[2] * up[1], up[2] * up[2]]))
    other = [up[1] * ref[2] - up[2] * ref[1], up[2] * ref[0] - up[0] * ref[2], up[0] * ref[1] - up[1] * ref[0]]
    angles = sorted(math.degrees(math.atan2(_dot(d, other), _dot(d, ref))) % 360 for d in dirs
                    if abs(_dot(d, up)) < 0.98)     # straight above or below says nothing of which side
    if len(angles) < 2:
        return 0.0
    gaps = [b - a for a, b in zip(angles, angles[1:])] + [angles[0] + 360 - angles[-1]]
    return round(360 - max(gaps), 1)


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def compute(mesh_dir: Path) -> dict:
    """A mesh's numbers: faces, placed (photos), photos_in, views (distinct viewpoints), around (degrees); None for
    any it hasn't what's needed for."""
    cleanup = _read(mesh_dir / "cleanup.json") or {}
    orient = _read(mesh_dir / "orientation.json") or {}
    timings = _read(mesh_dir / "timings.json") or {}
    cams = cameras(_read(mesh_dir / "cameras.json") or [])
    given = _read(mesh_dir / "photo_sources.json") or {}     # an older mesh's: every photo it was given, if not which placed
    out = {"faces": cleanup.get("faces"), "placed": len(cams) or orient.get("placed") or timings.get("placed"),
           "photos_in": timings.get("photos_in") or (len(given.get("photos") or []) if given.get("basis") == "given" else None) or None,
           "views": None, "around": None}
    centre = rock_centre(cams) if len(cams) >= 2 else None
    if centre:
        dirs = [_unit(_sub(c, centre)) for c, _ in cams]
        down = ((orient.get("iphone") or {}).get("down") if orient.get("method") == "iphone" else None) \
            or (orient.get("estimate") or {}).get("down") or [0.0, -1.0, 0.0]
        out["views"] = round(distinct_viewpoints(dirs), 1)
        out["around"] = around(dirs, [-x for x in down])
    return out


FLAT_BELOW = 0.025      # a mesh thinner than this share of its width is a photo on a card, not a rock (thickness())
# a mesh whose points stray less than this share of its width from a gently curved surface is a photo on a bent card
# (sheet_spread()): on Nov 19, 2026, the 12 meshes hidden as flat cards and Knuckleball Boulder (a slightly curved one,
# 0.044 thick by thickness()) were all under 0.0085, and Lud Foe and Trailside Tango, photos of a boulder with the sky
# and trees round it, 0.008-0.0092; the flattest real rock face looked at was 0.02
SHEET_BELOW = 0.01
SHAPE_FILE, SHAPE_VERSION = "shape.json", 1     # shape()'s, kept beside the mesh


def _points(glb: Path):
    """A mesh's vertex positions (at most ~50k of them, evenly), as an array; None if it can't be read."""
    import numpy as np

    import meshclean
    try:
        j, bufs = meshclean._read_glb(str(glb))
        pts, _, _ = meshclean._geometry(j, bufs)
    except Exception:  # noqa: BLE001 - no measure, so no verdict
        return None
    pts = np.asarray(pts, float)
    return pts[::max(1, len(pts) // 50000)] if len(pts) >= 3 else None


def thickness(glb: Path) -> float | None:
    """How thick a mesh is for its width: the spread of its vertices along their least direction over that along their
    most (the singular values of their positions about their mean). A rock, even one face of it, has relief, over
    0.04 in what was looked at on Nov 19, 2026; a placing whose cameras disagree (Swiss Cheese Boulder: one photo on a
    card), or a photo of a painting or a print, makes a flat sheet, under 0.025. None if it can't be read."""
    import numpy as np
    pts = _points(glb)
    if pts is None:
        return None
    s = np.linalg.svd(pts - pts.mean(0), compute_uv=False)
    return float(s[2] / s[0]) if s[0] > 0 else None


def sheet_spread(glb: Path) -> float | None:
    """How far a mesh strays from a gently curved sheet, for its width: a quadratic surface fitted to its vertices over
    their two widest directions, and the 90th percentile of their distances from it over the 95th of their reach along
    the widest. thickness() measures against a flat plane, so a photo on a slightly bent card (Knuckleball Boulder)
    passes it; this doesn't (SHEET_BELOW). None if it can't be read."""
    import numpy as np
    pts = _points(glb)
    if pts is None or len(pts) < 50:
        return None
    c = pts - pts.mean(0)
    _, _, vt = np.linalg.svd(c, full_matrices=False)
    x, y, d = c @ vt[0], c @ vt[1], c @ vt[2]
    reach = float(np.percentile(np.abs(x), 95))
    if reach <= 0:
        return None
    a = np.c_[x * x, y * y, x * y, x, y, np.ones_like(x)]
    coef, *_ = np.linalg.lstsq(a, d, rcond=None)
    return float(np.percentile(np.abs(d - a @ coef), 90) / reach)


def sky_share(mesh_dir: Path) -> float | None:
    """The share of a mesh's texture, where it's painted, that's sky blue: photos on cards often keep the sky round the
    rock, which a real rock's mesh rarely shows. A hint only: many real rocks have some. None without a texture."""
    import numpy as np
    from PIL import Image
    tex = sorted(mesh_dir.glob("scene_dense_mesh_texture*.png")) or sorted(mesh_dir.glob("*texture*.jpg"))
    if not tex:
        return None
    try:
        with Image.open(tex[0]) as im:
            im = im.convert("RGB")
            im.thumbnail((512, 512))
            px = np.asarray(im).astype(int).reshape(-1, 3)
    except Exception:  # noqa: BLE001
        return None
    px = px[px.sum(1) > 30]     # what's painted, not the atlas's empty black
    if len(px) < 100:
        return None
    r, g, b = px.T
    return round(float(((b > r + 25) & (b > g + 5) & (b > 110)).mean()), 4)


def shape(mesh_dir: Path, compute: bool = True) -> dict | None:
    """A mesh's shape measures, for telling rock from what isn't (the labelling page, and a classifier to come):
    {flat: thickness(), sheet: sheet_spread(), sky: sky_share()}, kept in SHAPE_FILE while its mesh.glb is unchanged.
    With compute False, only what's kept (None if nothing is, or it's stale)."""
    glb = mesh_dir / "mesh.glb"
    if not glb.exists():
        return None
    stamp = [SHAPE_VERSION, glb.stat().st_mtime]
    kept = _read(mesh_dir / SHAPE_FILE)
    if kept and kept.get("stamp") == stamp:
        return kept["shape"]
    if not compute:
        return None
    flat, sheet = thickness(glb), sheet_spread(glb)
    out = {"flat": round(flat, 4) if flat is not None else None, "sheet": round(sheet, 4) if sheet is not None else None,
           "sky": sky_share(mesh_dir)}
    try:
        (mesh_dir / SHAPE_FILE).write_text(json.dumps({"stamp": stamp, "shape": out}))
    except OSError:
        pass
    return out


def load(mesh_dir: Path) -> dict:
    """compute()'s, from metrics.json if it's as new as what it's worked out from; otherwise worked out again and
    kept there (if it can be)."""
    stamp = [VERSION] + [(mesh_dir / f).stat().st_mtime if (mesh_dir / f).exists() else None for f in SOURCES]
    kept = _read(mesh_dir / FILE)
    if kept and kept.get("stamp") == stamp:
        return kept["metrics"]
    metrics = compute(mesh_dir)
    try:
        (mesh_dir / FILE).write_text(json.dumps({"stamp": stamp, "metrics": metrics}))
    except OSError:
        pass
    return metrics
