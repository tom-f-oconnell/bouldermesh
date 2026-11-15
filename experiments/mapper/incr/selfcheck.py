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

All of it is backend/modelcheck.py's, which the app's own pipeline runs too: the thresholds and the measurements they come from are there. It says a model
is at odds with itself, not that one is right: a model can pass and be wrong.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for _b in (HERE.parents[2] / "backend", Path("/backend")):     # the repo's, or where boulder.py mounts it for the steps
    if (_b / "modelcheck.py").exists():
        sys.path.insert(0, str(_b))
        break
import modelcheck  # noqa: E402     the one implementation: backend/modelcheck.py (what's measured, the thresholds, why)

MAX_IMAGE_ID, UNTIED = modelcheck.MAX_IMAGE_ID, modelcheck.UNTIED
pair_angles, read_model = modelcheck.pair_angles, modelcheck.read_model
numbers = modelcheck.numbers_of


def check(cams: dict[str, dict], errs, track: float, db: Path) -> dict:
    """The numbers and the verdict, from a model in memory: {**numbers, "ok", "failed", "why"}."""
    n = numbers(cams, errs, track, db)
    return {**n, **modelcheck.verdict(n)}


def of_model(model: Path, db: Path) -> dict:
    return modelcheck.check(Path(model), Path(db))


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
