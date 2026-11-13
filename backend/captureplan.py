"""What a phone photographing a boulder needs to know of the mesh there already is of it (app/): a light copy of the mesh
to draw over the camera's picture, how well each part of it was photographed, and what's worth photographing: its holds
closest, then the rock around them, then the rest; and where the mesh simply ends.

  capture/guide.bin    the light mesh, each face with its patch, how finely it was seen and from how many directions
  capture/plan.json    its frame, the cameras its photos were placed at, and the targets

How finely: a face's ground sampling distance, how much rock one pixel covered in the best photo of it, in the mesh's
units a pixel (the app knows the scale better than the server, once it has lined the mesh up with the world). Targets
are in the mesh's own coordinates too, and what each needs (NEED) is in millimetres a pixel and directions.

guide.bin, little-endian: "BMG1", then uint32 vertices, faces, patches; float32 x, y, z for each vertex; uint32 a, b, c
for each face; uint16 patch for each face (65535: none); float32 units a pixel for each face (0: not seen); uint8
directions seen from for each face.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import time
from pathlib import Path

VERSION = 3             # goes up when a plan or guide made before would be read wrongly, or should be made again
DIR, PLAN_FILE, GUIDE_FILE = "capture", "plan.json", "guide.bin"
GUIDE_FACES = 12000     # at most, in the light mesh
PATCH_CELLS = 10        # a patch: a box this share of the mesh's size, and the way its faces face
VIEW_DEG = 15.0         # directions further apart than this count as different
FACING_DEG = 75.0       # a face seen more edge-on than this isn't seen
DEPTH_GRID = 96         # cells across a photo, for what hides what
VIEWS_KEPT = 8          # directions counted for a face, at most
GUESS_ACROSS_M = 8.0    # a mesh whose size wasn't found is taken as this far across (as the viewer's Nearby does)
HOLD_RADIUS_M = 0.15    # a hold's target: this far round it
HOLD_SAME_M = 0.08      # holds nearer each other than this are one
HOLD_DETAIL_M = 0.075   # the part of the rock that is a hold, when it's wanted in detail: this far round it, unless it says (beta.HOLD_R_M)
NEAR_HOLD_M = 0.75      # rock this near a hold matters as much as the climb does
NEAR_CLIMB_M = 1.5      # and this near where a climb's own photos look
# ladder: on the way in to a hold, a photo at each step this many times coarser, up to how its photos see it now: close
# photos at half a millimetre a pixel match web photos at 9 through those in between, not directly
NEED = {"hold": {"gsd_mm": 0.5, "views": 4, "ladder": 2.0}, "patch": {"gsd_mm": 1.5, "views": 3}, "edge": {"views": 3}, "view": {"views": 1},
        # the holds of the set of beta a capture is for: as fine as the phone's lens goes (from as close as it focuses, taken as
        # close_m, rather than a number of millimetres) and from more directions, further apart, for a hold's every face:
        # enough to make a replica of it from
        # enough to make a replica of it from. Its whole region (a hold's r_m) is in each close picture, so the phone is
        # as near as its lens focuses or as far as that takes; sides_deg: besides straight on, a view from its left, right,
        # above and below this far round, wherever a phone can get to; ties: this many steps of its ladder aren't only
        # asked for on the way in but needed, as they tie the close photos to the rest
        "detail": {"close_m": 0.15, "views": 6, "view_deg": 20.0, "ladder": 2.0, "sides_deg": 40.0, "ties": 3},
        # the rock that's on no climb, when the whole of it is wanted: one view, nearly square on, fine enough to texture it with
        "texture": {"gsd_mm": 2.0, "views": 1, "facing_deg": 35.0}}
# views: where a few of its photos were taken, to photograph it from again first, which ties a capture's photos to them
BRIDGE = {"within_m": 1.0, "deg": 20.0}     # how near one, and how nearly looking as it did, counts as the same view
BRIDGE_MOST = 6
BRIDGE_GAIN = 0.05          # a view that shows less than this share more of what its photos saw isn't asked for
BRIDGE_HEIGHT_M = (0.4, 2.3)    # above the ground: where a person can hold a phone
BRIDGE_FAR_M = 10.0
UP = (0.0, 1.0, 0.0)    # in a levelled mesh
# what each role of hold is drawn in, as the site draws it (beta.COLORS, which is blue-green-red)
ROLE_COLORS = {"start": "#19d23c", "hand": "#2f6bff", "finish": "#ff2a2a", "foot": "#f53dff"}


def _rotation(q):
    import numpy as np
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def decimate(V, F, max_faces: int = GUIDE_FACES):
    """A mesh with at most max_faces faces: its vertices merged by the cells of a grid, coarser until it's few enough."""
    import numpy as np
    V, F = np.asarray(V, float), np.asarray(F, np.int64)
    if len(F) <= max_faces:
        used, inv = np.unique(F, return_inverse=True)
        return V[used], inv.reshape(F.shape)
    lo = np.percentile(V[np.unique(F)], 0.5, axis=0)
    hi = np.percentile(V[np.unique(F)], 99.5, axis=0)
    size = float((hi - lo).max()) or 1.0
    k = 160.0
    while True:
        n = int(k) + 4
        keys = np.clip(np.floor((V - lo) / (size / k)).astype(np.int64) + 1, 0, n - 1)
        flat = (keys[:, 0] * n + keys[:, 1]) * n + keys[:, 2]
        uniq, inv = np.unique(flat, return_inverse=True)
        f = inv[F]
        f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]
        _, first = np.unique(np.sort(f, axis=1), axis=0, return_index=True)
        f = f[np.sort(first)]
        if len(f) <= max_faces or k <= 6:
            break
        k *= 0.85
    sums = np.zeros((len(uniq), 3))
    np.add.at(sums, inv, V)
    v = sums / np.bincount(inv, minlength=len(uniq))[:, None]
    used, back = np.unique(f, return_inverse=True)
    return v[used], back.reshape(f.shape)


def geometry(V, F):
    """Each face's centre, unit normal and area."""
    import numpy as np
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    cross = np.cross(b - a, c - a)
    size = np.linalg.norm(cross, axis=1)
    return (a + b + c) / 3, cross / np.maximum(size, 1e-12)[:, None], size / 2


def _sees(C, N, cam: dict):
    """What one photo saw of the faces: which (a mask), how finely each (units a pixel), the way to the camera from each,
    and where the camera was; or None if its camera can't be read or it saw none. A face is seen by a photo that has it
    in frame, facing it, with nothing nearer in the way (by a coarse picture of how far each part of the photo is, from
    the faces' centres)."""
    import numpy as np
    lens = cam.get("lens") or {}
    try:
        f, w, h = float(lens["params"][0]), float(lens["width"]), float(lens["height"])
        pinhole = lens.get("model") in (1, 4, 5, 6)     # fx, fy, cx, cy first
        cx, cy = (lens["params"][2], lens["params"][3]) if pinhole else (lens["params"][1], lens["params"][2])
        R, t = _rotation(cam["q"]), np.asarray(cam["t"], float)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if f <= 0 or w <= 0 or h <= 0:
        return None
    o = -R.T @ t
    P = (C - o) @ R.T
    z = P[:, 2]
    front = z > 1e-9
    zs = np.where(front, z, 1.0)
    u, v = f * P[:, 0] / zs + cx, f * P[:, 1] / zs + cy
    inside = front & (u >= 0) & (u < w) & (v >= 0) & (v < h)
    if not inside.any():
        return None
    away = C - o
    d = np.linalg.norm(away, axis=1)
    toward = -away / np.maximum(d, 1e-12)[:, None]
    cosang = np.einsum("ij,ij->i", N, toward)
    gh = max(1, int(round(DEPTH_GRID * h / w)))
    cell = (np.clip((v / h * gh).astype(np.int64), 0, gh - 1) * DEPTH_GRID + np.clip((u / w * DEPTH_GRID).astype(np.int64), 0, DEPTH_GRID - 1))
    cell = np.where(inside, cell, 0)
    nearest = np.full(DEPTH_GRID * gh, np.inf)
    np.minimum.at(nearest, cell[inside], d[inside])
    # one cell of the photo is this wide at that distance: a face a cell's width behind the nearest is beside it
    seen = inside & (cosang > math.cos(math.radians(FACING_DEG))) & (d <= nearest[cell] * (1.05 + 2.0 * w / DEPTH_GRID / f))
    if not seen.any():
        return None
    return seen, d / f / np.maximum(cosang, 0.2), toward, o, R.T @ np.array([0.0, 0.0, 1.0]), d


def coverage(C, N, cams: list[dict]):
    """How each face was photographed: the finest it was seen at, in units a pixel (inf: never), and from how many
    different directions (_sees() has what counts as seen)."""
    import numpy as np
    n = len(C)
    best = np.full(n, np.inf)
    dirs = np.zeros((n, VIEWS_KEPT, 3))
    count = np.zeros(n, np.int64)
    cos_view = math.cos(math.radians(VIEW_DEG))
    for cam in cams:
        saw = _sees(C, N, cam)
        if saw is None:
            continue
        seen, gsd, toward = saw[:3]
        best[seen] = np.minimum(best[seen], gsd[seen])
        idx = np.flatnonzero(seen)
        dots = np.einsum("ikj,ij->ik", dirs[idx], toward[idx])
        filled = np.arange(VIEWS_KEPT)[None, :] < count[idx][:, None]
        new = ~((dots > cos_view) & filled).any(axis=1) & (count[idx] < VIEWS_KEPT)
        add = idx[new]
        dirs[add, count[add]] = toward[add]
        count[add] += 1
    return best, count


def viewpoints(C, N, area, cams: list[dict], ground: float, mpu: float, rank: dict[str, float] | None = None,
               guessed: bool = False) -> list[dict]:
    """Where a few of its photos were taken, to photograph it from again: those that between them show the most of what
    any photo saw, each from somewhere a person can stand and hold a phone (BRIDGE_HEIGHT_M above the ground, within
    BRIDGE_FAR_M of what it shows). Chosen one at a time, each the one showing most not yet shown, until BRIDGE_MOST
    or one adds less than BRIDGE_GAIN of it all.

    Args:
        ground: the ground's height in the mesh; mpu: metres a unit.
        guessed: its size isn't known, so heights and distances in metres aren't: a view is then one from no higher
            than most of its photos were taken from (people stand to take photos), however far.
        rank: how well tied in each photo is, by name, if known (cameras.json's "seen": how many of its features became
            points of the model): of two views showing much the same, the better tied is asked for.

    Returns:
        [{name, c (where the camera was), d (the way it looked), share (of what any photo saw, that it adds)}]
    """
    import numpy as np
    saw = [(cam.get("name"), _sees(C, N, cam)) for cam in cams]
    saw = [(name, s) for name, s in saw if s is not None]
    if not saw:
        return []
    most = float(np.percentile([float(s[3][1]) for _, s in saw], 75))
    usable = []
    for name, (seen, _, _, o, look, d) in saw:
        up = (float(o[1]) - ground) * mpu
        if (float(o[1]) > most + 1e-9) if guessed else (not BRIDGE_HEIGHT_M[0] <= up <= BRIDGE_HEIGHT_M[1] or float(np.median(d[seen])) * mpu > BRIDGE_FAR_M):
            continue
        usable.append((name, o, look, seen))
    if not usable:
        return []
    every = np.zeros(len(C), bool)
    for *_, seen in usable:
        every |= seen
    total = float(area[every].sum()) or 1.0
    scores = [float((rank or {}).get(name) or 0.0) for name, *_ in usable]
    usual = float(np.median([x for x in scores if x > 0])) if any(x > 0 for x in scores) else 0.0
    # twice as well tied counts as showing 1.4 times as much: enough to choose between views, not to drown what they show
    weight = [math.sqrt(x / usual) if usual and x > 0 else 1.0 for x in scores]
    shown, out = np.zeros(len(C), bool), []
    while len(out) < BRIDGE_MOST:
        share = [float(area[seen & ~shown].sum()) / total for *_, seen in usable]
        worth = [sh * wt if sh >= BRIDGE_GAIN else -1.0 for sh, wt in zip(share, weight)]
        i = int(np.argmax(worth))
        if worth[i] < 0:
            break
        name, o, look, seen = usable[i]
        out.append({"name": name, "c": [round(float(x), 5) for x in o], "d": [round(float(x), 4) for x in look], "share": round(share[i], 3)})
        shown |= seen
    return out


def patches(C, N, area, lo, size):
    """Faces grouped into patches, by where they are (a box of the mesh's size over PATCH_CELLS) and which of six ways
    they mostly face. Returns each face's group."""
    import numpy as np
    box = np.clip(np.floor((C - lo) / (size / PATCH_CELLS)).astype(np.int64), 0, PATCH_CELLS * 4)
    axis = np.argmax(np.abs(N), axis=1)
    way = axis * 2 + (N[np.arange(len(N)), axis] < 0)
    key = ((box[:, 0] * 64 + box[:, 1]) * 64 + box[:, 2]) * 6 + way
    _, of = np.unique(key, return_inverse=True)
    return of


def _weighted_median(values, weights) -> float:
    import numpy as np
    order = np.argsort(values)
    cum = np.cumsum(np.asarray(weights)[order])
    return float(np.asarray(values)[order][int(np.searchsorted(cum, cum[-1] / 2))])


def boundary(F):
    """The edges only one face has: [(vertex, vertex, face)]."""
    import numpy as np
    e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    face = np.tile(np.arange(len(F)), 3)
    s = np.sort(e, axis=1)
    key = s[:, 0] * (int(F.max()) + 1) + s[:, 1]
    _, first, counts = np.unique(key, return_index=True, return_counts=True)
    once = first[counts == 1]
    return e[once], face[once]


def build(V, F, cams: list[dict], holds: list[dict], climbs: list[dict], metres_per_unit: float | None,
          rank: dict[str, float] | None = None, sets: list[dict] | None = None) -> tuple[dict, bytes]:
    """A mesh's plan and guide.

    Args:
        V, F: its vertices and faces, levelled (+Y up), stray pieces left out.
        cams: its cameras.json.
        holds: [{p, role, from ("video" or "marked"), climbs: [climb ids], sets: [ids of the sets of beta it's in],
            ids: [its own id in those sets], r_m: how far round it reaches, in metres, if it says}], in
            its coordinates.
        climbs: [{id, name, grade, view: {eye, target} or None}], those it's of.
        metres_per_unit: its scale, if it was found.
        rank: how well tied in each of its photos is, by name, if known (viewpoints()).
        sets: its sets of beta made by hand, [{id, name, climb}], for the plan to list with the targets of each.

    Returns:
        The plan (without what only the server knows: its name, place and heading), and guide.bin's bytes.
    """
    import numpy as np
    V, F = decimate(V, F)
    C, N, area = geometry(V, F)
    lo, hi = np.percentile(V, 5, axis=0), np.percentile(V, 95, axis=0)
    size = float((hi - lo).max()) or 1.0
    ground = float(np.percentile(V[:, 1], 2))
    height = float(np.percentile(V[:, 1], 98)) - ground or size
    guessed = not metres_per_unit
    mpu = float(metres_per_unit) if metres_per_unit else GUESS_ACROSS_M / size
    best, views = coverage(C, N, cams)
    of = patches(C, N, area, V.min(axis=0), size)
    up = np.array(UP)

    merged: list[dict] = []     # a hold found in two videos, or found and marked: once
    for h in holds:
        p = np.asarray(h["p"], float)
        same = next((x for x in merged if float(np.linalg.norm(np.asarray(x["p"], float) - p)) * mpu < HOLD_SAME_M), None)
        if same:
            same["climbs"] = sorted(set(same.get("climbs") or []) | set(h.get("climbs") or []))
            same["sets"] = sorted(set(same.get("sets") or []) | set(h.get("sets") or []))
            same["ids"] = same["ids"] + [i for i in h.get("ids") or [] if i not in same["ids"]]
            same["r_m"] = max(same.get("r_m") or 0.0, h.get("r_m") or 0.0) or None
        else:
            merged.append({**h, "climbs": list(h.get("climbs") or []), "sets": list(h.get("sets") or []), "ids": list(h.get("ids") or [])})
    holds = merged
    hold_p = np.array([h["p"] for h in holds], float).reshape(-1, 3)
    climb_p = [(c["id"], np.asarray(c["view"]["target"], float)) for c in climbs if (c.get("view") or {}).get("target")]

    def near(point):    # how much a spot matters (2: on a climb; 1: other rock), and the climbs it's on
        on = set()
        if len(hold_p):
            d = np.linalg.norm(hold_p - point, axis=1) * mpu
            for i in np.flatnonzero(d < NEAR_HOLD_M):
                on.update(holds[i].get("climbs") or [])
            close = bool((d < NEAR_HOLD_M).any())
        else:
            close = False
        for cid, target in climb_p:
            if float(np.linalg.norm(target - point)) * mpu < NEAR_CLIMB_M:
                on.add(cid)
                close = True
        return close, sorted(on)

    def have(faces):
        seen = np.isfinite(best[faces])
        if not seen.any():
            return None, 0
        return _weighted_median(best[faces][seen], area[faces][seen]), int(np.median(views[faces]))

    targets, patch_of = [], np.full(len(F), 65535, np.int64)
    known = bool(len(hold_p) or climb_p)
    least = (size / PATCH_CELLS) ** 2 * 0.05
    for p in range(int(of.max()) + 1 if len(of) else 0):
        faces = np.flatnonzero(of == p)
        total = float(area[faces].sum())
        if total < least:
            continue
        middle = np.average(C[faces], axis=0, weights=area[faces])
        at = C[faces[np.argmin(np.linalg.norm(C[faces] - middle, axis=1))]]
        normal = np.average(N[faces], axis=0, weights=area[faces])
        normal = normal / (np.linalg.norm(normal) or 1.0)
        flat = float(normal @ up) > 0.85 and at[1] < ground + 0.12 * height
        close, on = near(at)
        gsd, seen = have(faces)
        # with no holds or climbs' views to go by, what its photos saw is what matters, and the rest is the back
        prio = 0 if flat else 2 if (close if known else seen > 0) else 1
        patch_of[faces] = len(targets)
        targets.append({"id": f"p{len(targets)}", "kind": "patch", "p": [round(float(x), 5) for x in at],
                        "n": [round(float(x), 4) for x in normal], "r": round(math.sqrt(total / math.pi), 5), "prio": prio,
                        "have_gsd": None if gsd is None else round(gsd, 7), "have_views": seen, "climbs": on})
    n_patches = len(targets)

    radius = HOLD_RADIUS_M / mpu
    for i, h in enumerate(holds):
        p = np.asarray(h["p"], float)
        d = np.linalg.norm(C - p, axis=1)
        faces = np.flatnonzero(d < radius)
        if not len(faces):
            faces = np.array([int(np.argmin(d))])
        gsd, seen = have(faces)
        normal = np.average(N[faces], axis=0, weights=area[faces] + 1e-12)
        normal = normal / (np.linalg.norm(normal) or 1.0)
        targets.append({"id": f"h{i}", "kind": "hold", "role": h.get("role"), "from": h.get("from"),
                        "p": [round(float(x), 5) for x in p], "n": [round(float(x), 4) for x in normal], "r": round(radius, 5),
                        "prio": 3, "have_gsd": None if gsd is None else round(gsd, 7), "have_views": seen,
                        "climbs": sorted(h.get("climbs") or []), "sets": sorted(h.get("sets") or []),
                        "holds": h["ids"], "detail_m": round(float(h.get("r_m") or HOLD_DETAIL_M), 4)})

    edges, edge_face = boundary(F) if len(F) else (np.zeros((0, 2), np.int64), np.zeros(0, np.int64))
    if len(edges):
        mid = (V[edges[:, 0]] + V[edges[:, 1]]) / 2
        length = np.linalg.norm(V[edges[:, 0]] - V[edges[:, 1]], axis=1)
        out = mid - C[edge_face]
        out = out / np.maximum(np.linalg.norm(out, axis=1), 1e-12)[:, None]
        cell = size / 8
        box = np.floor((mid - V.min(axis=0)) / cell).astype(np.int64)
        _, group = np.unique((box[:, 0] * 64 + box[:, 1]) * 64 + box[:, 2], return_inverse=True)
        n_edge = 0
        for g in range(int(group.max()) + 1):
            m = np.flatnonzero(group == g)
            if float(length[m].sum()) < 0.5 * cell:
                continue
            at = np.average(mid[m], axis=0, weights=length[m])
            beyond = np.average(out[m], axis=0, weights=length[m])
            beyond = beyond / (np.linalg.norm(beyond) or 1.0)
            if float(beyond @ up) < -0.4 or at[1] < ground + 0.08 * height:     # where the rock meets the ground
                continue
            normal = np.average(N[edge_face[m]], axis=0, weights=length[m])
            normal = normal / (np.linalg.norm(normal) or 1.0)
            close, on = near(at)
            targets.append({"id": f"e{n_edge}", "kind": "edge", "p": [round(float(x), 5) for x in at],
                            "n": [round(float(x), 4) for x in normal], "out": [round(float(x), 4) for x in beyond],
                            "r": round(cell / 2, 5), "prio": 2 if close or not known else 1, "have_gsd": None, "have_views": 0, "climbs": on})
            n_edge += 1

    for i, v in enumerate(viewpoints(C, N, area, cams, ground, mpu, rank, guessed)):
        targets.append({"id": f"v{i}", "kind": "view", "p": v["c"], "n": v["d"], "r": round(BRIDGE["within_m"] / mpu, 5), "prio": 2,
                        "from": v["name"], "have_gsd": None, "have_views": 0, "climbs": []})

    shown = []
    for cam in cams:
        try:
            R, t = _rotation(cam["q"]), np.asarray(cam["t"], float)
        except (KeyError, TypeError, ValueError):
            continue
        shown.append({"c": [round(float(x), 4) for x in -R.T @ t], "d": [round(float(x), 3) for x in R.T @ np.array([0.0, 0.0, 1.0])]})

    guide = b"BMG1" + struct.pack("<III", len(V), len(F), n_patches) + V.astype("<f4").tobytes() + F.astype("<u4").tobytes() \
        + patch_of.astype("<u2").tobytes() + np.where(np.isfinite(best), best, 0.0).astype("<f4").tobytes() \
        + np.minimum(views, 255).astype("u1").tobytes()
    plan = {"v": VERSION, "made": time.time(),
            "frame": {"up": list(UP), "ground_y": round(ground, 5), "size": round(size, 5), "height": round(height, 5),
                      "metres_per_unit": round(mpu, 6), "scale_guessed": guessed},
            "guide": {"vertices": len(V), "faces": len(F), "patches": n_patches, "sha1": hashlib.sha1(guide).hexdigest()[:16]},
            "need": NEED, "bridge": BRIDGE, "view_deg": VIEW_DEG, "facing_deg": FACING_DEG,
            "sets": [{"id": one["id"], "name": one.get("name"), "climb": one.get("climb"),
                      "targets": [t["id"] for t in targets if t["kind"] == "hold" and one["id"] in t["sets"]]} for one in sets or []],
            "roles": ROLE_COLORS,
            "cameras": shown, "targets": targets,
            "climbs": [{"id": c["id"], "name": c.get("name"), "grade": c.get("grade")} for c in climbs]}
    return plan, guide


def key(version: str, holds_stamp: float, scale) -> str:
    """What a mesh's plan was made from: what the viewer is sent for it, its holds, and its scale."""
    return hashlib.sha1(f"{VERSION}|{version}|{holds_stamp}|{scale}".encode()).hexdigest()[:12]


def load(mesh_dir: Path, want: str | None = None) -> dict | None:
    """A mesh's plan as kept, or None if there's none, or it was made from other than want (a key())."""
    try:
        plan = json.loads((mesh_dir / DIR / PLAN_FILE).read_text())
    except (OSError, ValueError):
        return None
    if plan.get("v") != VERSION or (want is not None and plan.get("key") != want) or not (mesh_dir / DIR / GUIDE_FILE).exists():
        return None
    return plan


def make(mesh_dir: Path, holds: list[dict], climbs: list[dict], metres_per_unit: float | None, made_from: str,
         sets: list[dict] | None = None) -> dict:
    """Make and keep a mesh's plan and guide, from its mesh (its stray pieces left out) and cameras.

    Raises:
        ValueError: it has no mesh that can be read.
    """
    import beta
    import scale
    mesh = beta.Mesh.load(mesh_dir)
    kept = scale.kept_faces(mesh_dir, len(mesh.F))
    F = mesh.F if kept is None else mesh.F[kept]
    try:
        cams = json.loads((mesh_dir / "cameras.json").read_text())
    except (OSError, ValueError):
        cams = []
    # how well tied in each photo is, where the mesh's cameras say (those kept since orientation.read_cameras() counted it)
    rank = {c["name"]: c["seen"] for c in cams if isinstance(c, dict) and isinstance(c.get("seen"), (int, float)) and c.get("name")}
    plan, guide = build(mesh.V, F, cams, holds, climbs, metres_per_unit, rank or None, sets)
    plan["key"] = made_from
    out = mesh_dir / DIR
    out.mkdir(exist_ok=True)
    (out / (GUIDE_FILE + ".part")).write_bytes(guide)
    (out / (GUIDE_FILE + ".part")).replace(out / GUIDE_FILE)
    (out / (PLAN_FILE + ".part")).write_text(json.dumps(plan))
    (out / (PLAN_FILE + ".part")).replace(out / PLAN_FILE)
    return plan
