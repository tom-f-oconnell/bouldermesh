"""Where each climb on a boulder is seen from, in a mesh made from the photos of all of them.

A boulder's mesh pools every one of its climbs' harvests. The photos that belong to one climb in
particular are those from its own page and its own videos (relevance.is_trusted(): web searches are
of the boulder as a whole). Where those were placed, they show which side of the rock the climb is
on: the view is taken from the middle one of them, looking at where they all look.

  views.json   {created, climbs: {climb id: {eye, target, photos, of, names}}, unplaced: {climb id: n}}

eye and target are points in the mesh's own coordinates, as are its cameras (cameras.json).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

FILE = "views.json"
RAYS = ((0.5, 0.5), (0.35, 0.35), (0.65, 0.35), (0.35, 0.65), (0.65, 0.65))   # where in each photo to look, as shares of it
NEAREST, FURTHEST = 0.6, 2.5     # how far from what it looks at a view may be, in the boulder's size

Log = Callable[[str], None]


def own_photos(project_dir: Path) -> set[str]:
    """A climb's harvest's photos that are its own: its page's and its videos' frames, as kept, but not those a
    harvest from before boulders had their own meshes pooled from another climb's (harvest.pool())."""
    from climbs.relevance import is_trusted
    photos = project_dir / "photos"
    try:
        files = json.loads((project_dir / "sources.json").read_text()).get("files") or []
    except (OSError, ValueError):
        files = []
    pooled = {f["file"] for f in files if f.get("pooled_from")}
    return {p.name for p in photos.iterdir() if is_trusted(p) and p.name not in pooled} if photos.is_dir() else set()


def _pose(cam: dict):
    import numpy as np
    from beta import _rotation
    R, t = _rotation(cam["q"]), np.asarray(cam["t"], float)
    return -R.T @ t, R.T @ np.array([0.0, 0.0, 1.0])


def _looked_at(cams: list[dict], mesh, fallback) -> "object":
    """Where a climb's photos look: the middle of where rays through each of them meet the mesh, or without
    any such, the point nearest to all their axes (drawn a little towards fallback, so that one is enough)."""
    import numpy as np
    from beta import camera_ray
    hits = []
    for cam in cams:
        lens = cam.get("lens") or {}
        size = (lens.get("width") or 1000, lens.get("height") or 750)
        for fx, fy in RAYS if mesh is not None else ():
            origin, direction = camera_ray(cam, size, (fx * size[0], fy * size[1]))
            p = mesh.hit(origin, direction)
            if p is not None:
                hits.append(p)
    if hits:
        return np.median(np.array(hits), axis=0)
    A, b = 0.1 * len(cams) * np.eye(3), 0.1 * len(cams) * np.asarray(fallback, float)
    for cam in cams:
        c, d = _pose(cam)
        P = np.eye(3) - np.outer(d, d)
        A += P
        b += P @ c
    return np.linalg.solve(A, b)


def compute(mesh_dir: Path, climbs: dict[str, set[str]], log: Log) -> dict:
    """Work out and save each climb's view of a boulder's mesh.

    Args:
        climbs: {climb id: the names of its own photos}, as own_photos() gives them.

    Returns:
        What's saved in views.json.
    """
    import numpy as np
    cams = {c["name"]: c for c in json.loads((mesh_dir / "cameras.json").read_text())}
    try:
        from beta import Mesh
        mesh = Mesh.load(mesh_dir)
        middle, size = np.median(mesh.c, axis=0), mesh.size
    except Exception as e:     # a mesh that can't be read: its cameras still say roughly where things are
        log(f"views: couldn't read the mesh ({e}); going by the cameras alone")
        mesh = None
        centres = np.array([_pose(c)[0] for c in cams.values()])
        middle, size = np.median(centres, axis=0), float(np.ptp(centres, axis=0).max() or 1.0) / 3
    out = {"created": time.time(), "climbs": {}, "unplaced": {}}
    for cid, names in climbs.items():
        placed = [cams[n] for n in sorted(names) if n in cams]
        if not placed:
            if names:
                out["unplaced"][cid] = len(names)
            continue
        centres = np.array([_pose(c)[0] for c in placed])
        eye = centres[np.argmin(np.linalg.norm(centres - np.median(centres, axis=0), axis=1))]
        target = _looked_at(placed, mesh, middle)
        away = eye - target
        dist = float(np.linalg.norm(away))
        if dist < 1e-9:
            away, dist = eye - middle, float(np.linalg.norm(eye - middle)) or 1.0
        eye = target + away / dist * min(max(dist, NEAREST * size), FURTHEST * size)
        out["climbs"][cid] = {"eye": [round(float(x), 5) for x in eye], "target": [round(float(x), 5) for x in target],
                              "photos": len(placed), "of": len(names), "names": [c["name"] for c in placed]}
    tmp = mesh_dir / (FILE + ".partial")
    tmp.write_text(json.dumps(out))
    tmp.replace(mesh_dir / FILE)
    log(f"views: {len(out['climbs'])} climb(s) placed by their own photos" +
        (f", {len(out['unplaced'])} with none of theirs placed" if out["unplaced"] else ""))
    return out


POOR_LEAST, POOR_SHARE = 3, 0.25     # a climb with fewer of its own photos placed than both of these is poorly placed


def poorly_placed(views: dict) -> dict[str, tuple[int, int]]:
    """The climbs a pooled mesh hardly shows, by their own photos (a side its other climbs' photos don't reach, or a
    rock that didn't join the rest): {climb id: (placed, of its own)}, for those with fewer placed than POOR_LEAST or
    than POOR_SHARE of their own. Not climbs without own photos, of which nothing can be said."""
    out = {cid: (0, n) for cid, n in (views.get("unplaced") or {}).items() if n}
    for cid, v in (views.get("climbs") or {}).items():
        if v.get("of") and v["photos"] < max(POOR_LEAST, POOR_SHARE * v["of"]):
            out[cid] = (v["photos"], v["of"])
    return out


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((mesh_dir / FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
