"""What a model can say of itself, from its own files and its photos' database, and whether that passes: the check the
incremental pipeline makes of every model before it keeps it (numpy and sqlite only, so it runs on any model anywhere).

    selfcheck.py <model dir> <database> [...]     one model's numbers and verdict, as JSON (pairs of arguments)
    selfcheck.py table [bench dir]                every model under <bench>/r2 (after the rest step) and <bench>/incr/r

The numbers (numbers()):

    err_mean, err_p90   the points' reprojection errors, as COLMAP has them (each point's mean over its track; COLMAP's
                        "mean reprojection error" is their mean)
    pair_med_deg        over the verified pairs of placed photos (two-view geometries with 20+ inliers that COLMAP didn't
                        call planar or panoramic), the angle between the turn from one camera to the other in the model
                        and the turn their fundamental matrix gives through the model's lenses: the median, and the 90th
                        percentile. A model its photos agree with is at 1-3 degrees
    odd_lenses          lenses that can't be real (the benchmark's consensus.py rule: focal length outside 0.3-6 of the
                        picture's longer side, or the principal point over a quarter of it from the middle)
    untied              cameras seeing fewer than UNTIED of the model's points

The verdict is state.py's model_check(), which has the thresholds and the measurements they come from. It says a model
is at odds with itself, not that one is right: a model can pass and be wrong.
"""
from __future__ import annotations

import json
import math
import sqlite3
import struct
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import state as st  # noqa: E402

MAX_IMAGE_ID = 2147483647
MIN_INLIERS = 20        # the pairs the mapper itself uses (the app's min_num_matches)
UNTIED = 15
NUM_PARAMS = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12}


def pair_angles(cams: dict[str, dict], db: Path) -> list[float]:
    """For each verified pair of the cameras named ({name: {R world to camera, f, cx, cy}}), the angle in degrees
    between their relative turn in the model and the nearer of the two their fundamental matrix allows."""
    W = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1.0]])
    K = {n: np.array([[c["f"], 0, c["cx"]], [0, c["f"], c["cy"]], [0, 0, 1.0]]) for n, c in cams.items()}
    out = []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        name = {i: n for i, n in con.execute("SELECT image_id, name FROM images")}
        for pid, F in con.execute("SELECT pair_id, F FROM two_view_geometries WHERE rows >= ? AND config IN (2, 3)", (MIN_INLIERS,)):
            a, b = (name.get(i) for i in divmod(pid, MAX_IMAGE_ID))
            if a not in cams or b not in cams or not F:
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


def numbers(cams: dict[str, dict], errs, track: float, db: Path) -> dict:
    """A model's numbers: cams {name: {R, f, cx, cy, w, h, seen}}, its points' errors (px), its mean track length."""
    errs = np.asarray(errs, float)
    errs = errs[errs >= 0]
    odd = sum(1 for c in cams.values()
              if not (0.3 * max(c["w"], c["h"]) < c["f"] < 6 * max(c["w"], c["h"]))
              or math.hypot(c["cx"] - c["w"] / 2, c["cy"] - c["h"] / 2) > 0.25 * max(c["w"], c["h"]))
    ang = pair_angles(cams, db)
    return {"placed": len(cams), "points": int(len(errs)), "track": round(float(track), 2),
            "err_mean": round(float(errs.mean()), 3) if len(errs) else None,
            "err_p90": round(float(np.percentile(errs, 90)), 3) if len(errs) else None,
            "pairs": len(ang), "pair_med_deg": round(float(np.median(ang)), 2) if ang else None,
            "pair_p90_deg": round(float(np.percentile(ang, 90)), 2) if ang else None,
            "odd_lenses": int(odd), "untied": sum(1 for c in cams.values() if c.get("seen", UNTIED) < UNTIED)}


def check(cams: dict[str, dict], errs, track: float, db: Path) -> dict:
    """The numbers and the verdict: {**numbers, "ok", "failed", "why"}."""
    n = numbers(cams, errs, track, db)
    return {**n, **st.model_check(n)}


def read_model(model: Path) -> tuple[dict[str, dict], np.ndarray, float]:
    """A binary COLMAP model's cameras (as numbers() wants them), its points' errors and its mean track length."""
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
    return cams, np.array(errs), float(np.mean(lens)) if lens else 0.0


def of_model(model: Path, db: Path) -> dict:
    return check(*read_model(Path(model)), Path(db))


def table(bench: Path) -> None:
    cols = ["boulder", "model", "placed", "points", "track", "err_mean", "err_p90", "pairs", "pair_med_deg", "pair_p90_deg",
            "odd_lenses", "untied", "ok", "failed"]
    print("\t".join(cols))
    for proj in sorted((bench / "projects").iterdir()) if (bench / "projects").exists() else []:
        P = proj.name
        models = []
        for d in sorted((bench / "r2" / P).glob("*/*/rest/rest2")):
            db = d.parents[2] / "database.db"
            models.append((f"{d.parents[2].name}/{d.parents[1].name}", d, db))
        for d in sorted((bench / "incr" / "r" / P).glob("*/model")):
            models.append((f"pipeline:{d.parent.name}", d, d.parent / "database.db"))
        for name, d, db in models:
            if not (d / "images.bin").exists() or not db.exists():
                continue
            try:
                c = of_model(d, db)
            except Exception as e:      # one unreadable model doesn't stop the table
                c = {"failed": [f"{type(e).__name__}: {e}"]}
            print("\t".join(str({"boulder": P, "model": name, **c, "failed": ",".join(c.get("failed") or [])}.get(k, "")) for k in cols), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "table":
        table(Path(sys.argv[2] if len(sys.argv) > 2 else "/data/tom/mapperbench"))
    else:
        for m, d in zip(sys.argv[1::2], sys.argv[2::2]):
            print(json.dumps({"model": m, **of_model(Path(m), Path(d))}))
