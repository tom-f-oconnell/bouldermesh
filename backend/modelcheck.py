"""A reconstruction's check of itself: what a COLMAP model can say about whether it hangs together, from its own files
and its photos' database (design.md, "A model's check of itself").

A model can place every photo, with points a pixel off, and still be wrong: nothing in its counts shows it. What does
show it is the photos' own pairwise geometry, which COLMAP worked out before any model existed: for each pair of
photos it verified, how one camera is turned from the other. A model that hangs together has its cameras turned from
each other the way their pairs say; one that doesn't, hasn't.

    numbers(model, db)    placed, points, track; err_mean, err_p90 (the points' reprojection errors, px: each point's
                          mean over its track, as COLMAP has it, leaving out err_unread: values no error can be, over
                          ERR_MOST_PX or not a number); pairs, pair_med_deg, pair_p90_deg (over the verified
                          pairs of placed photos with MIN_INLIERS and a geometry COLMAP didn't call planar or
                          panoramic: the angle between the turn from one camera to the other in the model and the
                          nearer of the two their fundamental matrix allows through the model's lenses); odd_lenses
                          (focal length outside 0.3-6 of the picture's longer side, or the principal point over a
                          quarter of it from the middle); untied (cameras seeing fewer than UNTIED of its points)
    verdict(numbers)      {ok, failed: ["error", "pairs", "lenses"], why}, by THRESHOLDS
    check(model, db)      the two together
    of_mesh(mesh_dir)     the same for a mesh that kept its reconstruction (reconstruction.py), any time later
    save(out_dir, check), load(mesh_dir)     check.json beside the mesh

It measures a model against its own photos, not against the truth: a model can pass and be wrong, and where the
photos' pairs are themselves poor (few of them, or nearly all planar) it says little. Reads the binary models and
databases of COLMAP 3.9.1 and 4.2.1 alike. Needs numpy for the numbers; the verdict needs nothing.
"""
from __future__ import annotations

import json
import math
import sqlite3
import struct
import tempfile
import time
from pathlib import Path

FILE = "check.json"
VERSION = 1
MAX_IMAGE_ID = 2147483647   # COLMAP's pair_id is this times the smaller image id, plus the larger
MIN_INLIERS = 20            # the pairs the mapper itself goes by (the app's min_num_matches)
UNTIED = 15                 # a camera seeing fewer of the model's points than this isn't tied to it
# A point's error past this isn't a reprojection error but a value COLMAP never set: 4.2.1's models after its global
# mapper's last steps have held a few of 1e150 px and more among points otherwise a pixel or two off (atlas, Nov 15)
ERR_MOST_PX = 1e4
NUM_PARAMS = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12}     # by COLMAP's camera model id

# From the mapper benchmark's models of four boulders, with the 3.9.1 baseline and seven other methods (hal's 29 models
# and atlas's; experiments/mapper/incr/README.md, design.md). First guesses from few boulders, to be revisited.
THRESHOLDS = {
    # every sound model's points were 0.66-1.71 px off in the mean (90th percentile 1.6-2.5); models spoilt by tracks
    # merged at loose tolerances were at 2.45-5.7 (3.1-15)
    "err_mean": 2.0,
    "err_p90": 3.0,
    # cameras were 1.3-3.0 degrees (median) from their pairs in the models that methods agree on, and 6-35 in those
    # they don't: every method's model of the one boulder no two methods agree on (11-16), both baseline runs of a
    # boulder they placed 111 and 181 photos of (10-12), and a model another check had found wrong (26.9, where the
    # eight right ones of that boulder were at 2.0-2.9)
    "pair_med_deg": 5.0,
    "min_pairs": 20,            # fewer pairs than this say nothing
    # lenses that can't be real were 0-6% of a sound model's, and up to 21% of the others'
    "odd_lens_share": 0.2,
    "min_cameras": 10,
}


def read_model(model: Path) -> tuple[dict[str, dict], list[float], float]:
    """A binary COLMAP model's cameras by photo name ({R: world to camera, f, cx, cy, w, h, seen}), its points'
    reprojection errors, and its mean track length. COLMAP 4's extra files (rigs.bin, frames.bin) aren't needed."""
    import numpy as np
    lenses = {}
    with open(model / "cameras.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            cid, mid = struct.unpack("<ii", f.read(8))
            w, h = struct.unpack("<QQ", f.read(16))
            p = struct.unpack(f"<{NUM_PARAMS[mid]}d", f.read(8 * NUM_PARAMS[mid]))
            lenses[cid] = ((p[0], p[1], p[2]) if mid in (0, 2, 3) else ((p[0] + p[1]) / 2, p[2], p[3])) + (w, h)
    cams = {}
    with open(model / "images.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            v = struct.unpack("<I7dI", f.read(64))
            name = b""
            while (c := f.read(1)) != b"\0":
                name += c
            n = struct.unpack("<Q", f.read(8))[0]
            obs = np.frombuffer(f.read(24 * n), np.dtype([("x", "<f8"), ("y", "<f8"), ("p", "<i8")]))
            w, x, y, z = v[1:5]
            R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                          [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                          [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])
            if v[8] not in lenses:
                continue
            fl, cx, cy, wd, ht = lenses[v[8]]
            cams[name.decode()] = {"R": R, "f": fl, "cx": cx, "cy": cy, "w": wd, "h": ht, "seen": int((obs["p"] != -1).sum())}
    errs, lens = [], []
    with open(model / "points3D.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(8 + 24 + 3)
            errs.append(struct.unpack("<d", f.read(8))[0])
            k = struct.unpack("<Q", f.read(8))[0]
            f.read(8 * k)
            lens.append(k)
    return cams, errs, (sum(lens) / len(lens) if lens else 0.0)


def pair_angles(cams: dict[str, dict], db: Path) -> list[float]:
    """For each verified pair of the cameras given, degrees between their relative turn in the model and the nearer of
    the two turns their fundamental matrix allows (through the model's own lenses, distortion left out)."""
    import numpy as np
    W = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1.0]])
    K = {n: np.array([[c["f"], 0, c["cx"]], [0, c["f"], c["cy"]], [0, 0, 1.0]]) for n, c in cams.items()}
    out = []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        name = {i: n for i, n in con.execute("SELECT image_id, name FROM images")}
        for pid, F in con.execute("SELECT pair_id, F FROM two_view_geometries WHERE rows >= ? AND config IN (2, 3)", (MIN_INLIERS,)):
            a, b = (name.get(i) for i in divmod(pid, MAX_IMAGE_ID))
            if a not in cams or b not in cams or not F or len(F) != 72:
                continue
            E = K[b].T @ np.frombuffer(F, np.float64).reshape(3, 3) @ K[a]
            if not np.isfinite(E).all() or not E.any():
                continue
            U, _, Vt = np.linalg.svd(E)
            U, Vt = (U if np.linalg.det(U) > 0 else -U), (Vt if np.linalg.det(Vt) > 0 else -Vt)
            rel = cams[b]["R"] @ cams[a]["R"].T
            out.append(min(float(np.degrees(np.arccos(np.clip((np.trace(rel @ c.T) - 1) / 2, -1, 1)))) for c in (U @ W @ Vt, U @ W.T @ Vt)))
    finally:
        con.close()
    return out


def numbers_of(cams: dict[str, dict], errs, track: float, db: Path) -> dict:
    """The numbers, from cameras, point errors and track length already read (the incremental pipeline has them in
    memory; numbers() reads them from a model's files)."""
    import numpy as np
    errs = np.asarray(errs, float)
    errs = errs[~(errs < 0)]    # COLMAP's -1: not worked out (a NaN is kept, to be counted below)
    sane = np.isfinite(errs) & (errs <= ERR_MOST_PX)
    unread, errs = int((~sane).sum()), errs[sane]
    odd = sum(1 for c in cams.values()
              if not (0.3 * max(c["w"], c["h"]) < c["f"] < 6 * max(c["w"], c["h"]))
              or math.hypot(c["cx"] - c["w"] / 2, c["cy"] - c["h"] / 2) > 0.25 * max(c["w"], c["h"]))
    ang = pair_angles(cams, db)
    return {"placed": len(cams), "points": int(len(errs)), "track": round(float(track), 2),
            "err_mean": round(float(errs.mean()), 3) if len(errs) else None,
            "err_p90": round(float(np.percentile(errs, 90)), 3) if len(errs) else None, "err_unread": unread,
            "pairs": len(ang), "pair_med_deg": round(float(np.median(ang)), 2) if ang else None,
            "pair_p90_deg": round(float(np.percentile(ang, 90)), 2) if ang else None,
            "odd_lenses": int(odd), "untied": sum(1 for c in cams.values() if c.get("seen", UNTIED) < UNTIED)}


def numbers(model: Path, db: Path) -> dict:
    return numbers_of(*read_model(Path(model)), Path(db))


def verdict(n: dict, thresholds: dict = THRESHOLDS) -> dict:
    """Whether a model's numbers pass: {ok, failed (of "error", "pairs", "lenses"), why: the numbers in words, what
    failed first}."""
    t = thresholds
    failed, said = [], []
    mean, p90 = n.get("err_mean"), n.get("err_p90")
    if mean is None:
        failed.append("error")
        said.append((True, "it has no points"))
    else:
        bad = mean > t["err_mean"] or p90 > t["err_p90"]
        if bad:
            failed.append("error")
        said.append((bad, f"its points are {mean:.2f} px off (90th percentile {p90:.2f})"
                     + (f", over the {t['err_mean']:g} ({t['err_p90']:g}) a sound model stays under" if bad else "")))
    pairs, med = n.get("pairs") or 0, n.get("pair_med_deg")
    if pairs >= t["min_pairs"] and med is not None:
        bad = med > t["pair_med_deg"]
        if bad:
            failed.append("pairs")
        said.append((bad, f"its cameras are {med:.1f} degrees (median) from their photos' pairwise geometry over {pairs} pairs"
                     + (f", over {t['pair_med_deg']:g}" if bad else "")))
    else:
        said.append((False, f"only {pairs} pairs to hold its cameras against"))
    placed, odd = n.get("placed") or 0, n.get("odd_lenses") or 0
    if placed >= t["min_cameras"] and odd > t["odd_lens_share"] * placed:
        failed.append("lenses")
        said.append((True, f"{odd} of its {placed} lenses can't be real"))
    elif odd:
        said.append((False, f"{odd} odd lens{'es' if odd != 1 else ''}"))
    return {"ok": not failed, "failed": failed, "why": "; ".join(s for _, s in sorted(said, key=lambda x: not x[0]))}


def check(model: Path, db: Path) -> dict:
    """A model's numbers and verdict: {v, made, **numbers, ok, failed, why}."""
    n = numbers(model, db)
    return {"v": VERSION, "made": time.time(), **n, **verdict(n)}


def line(c: dict) -> str:
    """One line for a log."""
    return (f"model check: {'passed' if c['ok'] else 'FAILED (' + ', '.join(c['failed']) + ')'}: {c['why']} "
            f"[{c['placed']} placed, {c['points']} points, track {c['track']}, {c['untied']} untied]")


def save(out_dir: Path, c: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (FILE + ".partial")
    tmp.write_text(json.dumps(c))
    tmp.replace(out_dir / FILE)
    return out_dir / FILE


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((mesh_dir / FILE).read_text())
    except (OSError, ValueError):
        return None


def of_mesh(mesh_dir: Path, write: bool = False) -> dict | None:
    """The check of a mesh that kept its reconstruction (reconstruction.tar.gz: its model and pairs.db, which keeps
    the verified pairs), made now. None if it kept none. With write, saved as its check.json."""
    import reconstruction
    with tempfile.TemporaryDirectory() as d:
        if reconstruction.unpack(mesh_dir, Path(d)) is None:
            return None
        c = check(Path(d) / "model", Path(d) / "pairs.db")
    if write:
        save(mesh_dir, c)
    return c
