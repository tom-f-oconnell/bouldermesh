"""How one mesh's coordinates sit in another's: the frame a capture's mesh is tied to the mesh it adds to by.

A mesh made from a rock's photos has its own coordinates: where its reconstruction happened to put the origin, which
way it was levelled, and units that are only metres once its size is found. Two meshes of the same rock that share
photos can be tied exactly, because each says where it put those photos' cameras: tie() finds the scale, turn and
shift taking one's coordinates to the other's. With that, whatever is known of one in its coordinates (holds, views, a
plan's targets) can be carried to the other, the two laid over each other, and the angle between their ups read off.

The record, as FILE in the later mesh's folder (app/FORMAT.md; the server's tie_capture() and the incremental
pipeline in experiments/mapper/incr both write it, so that the server treats their meshes alike):

    v          1
    of         the mesh this one is tied to (the one a capture added to)
    how        "cameras": this mesh was made on its own, and tied afterwards by the cameras they share
               "refit": the same, by the incremental pipeline's first build over every photo
               "add": made in the other's frame, by adding photos to its reconstruction: the identity, but for drift
               "add+relevel": the same, then turned level by the capture's own readings: a rotation, scale 1
               "texture": the other's own shape, textured again (from a capture's photos first): the identity
    status     "tied", or "untied" with why (too few shared cameras): the rest is then absent
    scale, rot (3 rows), shift     x_this = scale * rot @ x_of + shift
    shared, of_shared              the cameras that agreed, of those the two have in common
    rms        how far those cameras' centres are from where the tie puts them, in this mesh's units (rms_m: metres,
               if its size is known)
    tilt_deg   the angle between the other's up (+Y) and this one's: how far off level one is by the other's reckoning
    rot_deg    the whole turn
    capture_share   of its surface, the share textured from photos taken with an app of ours, if whoever made it says
    made, note
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

FILE = "frame.json"
VERSION = 1
LEAST = 4               # cameras two meshes have to share to be tied
HOW = ("cameras", "refit", "add", "add+relevel", "texture")


def _rotation(q):
    """World to camera, from a quaternion w, x, y, z (as cameras.json has it)."""
    import numpy as np
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def tie(of_cams: list[dict], cams: list[dict], least: int = LEAST) -> dict:
    """The scale, turn and shift taking one mesh's coordinates to another's, from the cameras both placed.

    The turn comes from the cameras' own rotations (each shared camera says it outright: where the two meshes have the
    same photo looking, they differ by the turn between them), so it's found even where the cameras stand in a line,
    which their positions alone couldn't say. Scale and shift come from their positions. Cameras one mesh has
    somewhere the tie doesn't put them (a photo placed wrongly in either) are left out and it's found again.

    Args:
        of_cams: the cameras.json of the mesh tied to (name, q, t: world to camera).
        cams: this mesh's.

    Returns:
        {status: "tied", scale, rot, shift, shared, of_shared, rms, tilt_deg, rot_deg}, or {status: "untied", why,
        shared: 0, of_shared}.
    """
    import numpy as np
    def read(cs):
        out = {}
        for c in cs:
            try:
                R = _rotation(c["q"])
                out[c["name"]] = (R, -R.T @ np.asarray(c["t"], float))
            except (KeyError, TypeError, ValueError):
                continue
        return out
    old, new = read(of_cams), read(cams)
    names = sorted(set(old) & set(new))
    if len(names) < least:
        return {"status": "untied", "why": f"they share {len(names)} camera{'' if len(names) == 1 else 's'}, and {least} are needed",
                "shared": 0, "of_shared": len(names)}
    X, Y = np.array([old[n][1] for n in names]), np.array([new[n][1] for n in names])
    turns = np.array([new[n][0].T @ old[n][0] for n in names])      # each camera's word on the turn from the one mesh to the other
    keep = np.ones(len(names), bool)
    for _ in range(5):
        U, _, Vt = np.linalg.svd(turns[keep].sum(axis=0))       # the rotation nearest their mean
        R = U @ np.diag([1.0, 1.0, float(np.sign(np.linalg.det(U @ Vt)))]) @ Vt
        mx, my = X[keep].mean(axis=0), Y[keep].mean(axis=0)
        A, B = (X[keep] - mx) @ R.T, Y[keep] - my
        spread = float((A * A).sum())
        s = float((A * B).sum() / spread) if spread > 1e-18 else 1.0
        t = my - s * R @ mx
        off = np.linalg.norm(Y - (s * X @ R.T + t), axis=1)
        again = off <= max(3 * 1.4826 * float(np.median(off[keep])), 1e-9 * (1 + float(np.abs(Y).max())))
        if (again == keep).all() or again.sum() < least:
            break
        keep = again
    if s <= 0:
        return {"status": "untied", "why": "the cameras they share don't fit one onto the other", "shared": 0, "of_shared": len(names)}
    up = R @ np.array([0.0, 1.0, 0.0])
    return {"status": "tied", "scale": round(s, 7), "rot": [[round(float(x), 8) for x in row] for row in R], "shift": [round(float(x), 6) for x in t],
            "shared": int(keep.sum()), "of_shared": len(names), "rms": round(float(np.sqrt(np.mean(off[keep] ** 2))), 6),
            "tilt_deg": round(math.degrees(math.acos(max(-1.0, min(1.0, float(up[1]))))), 2),
            "rot_deg": round(math.degrees(math.acos(max(-1.0, min(1.0, (float(np.trace(R)) - 1) / 2)))), 2)}


def apply(frame: dict, point) -> list[float]:
    """A point of the mesh tied to, in this mesh's coordinates."""
    import numpy as np
    return [float(x) for x in frame["scale"] * np.asarray(frame["rot"], float) @ np.asarray(point, float) + np.asarray(frame["shift"], float)]


def back(frame: dict, point) -> list[float]:
    """A point of this mesh, in the coordinates of the mesh tied to."""
    import numpy as np
    return [float(x) for x in np.asarray(frame["rot"], float).T @ (np.asarray(point, float) - np.asarray(frame["shift"], float)) / frame["scale"]]


def save(mesh_dir: Path, of: str, how: str, found: dict, **more) -> dict:
    """Keep a mesh's tie to another (tie()'s answer, or one known some other way) as its FILE. Returns the record."""
    if how not in HOW:
        raise ValueError(f"how a mesh is tied is one of {', '.join(HOW)}")
    rec = {"v": VERSION, "of": of, "how": how, "made": time.time(), **found, **more}
    tmp = mesh_dir / (FILE + ".partial")
    tmp.write_text(json.dumps(rec))
    tmp.replace(mesh_dir / FILE)
    return rec


def load(mesh_dir: Path) -> dict | None:
    try:
        rec = json.loads((mesh_dir / FILE).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None
