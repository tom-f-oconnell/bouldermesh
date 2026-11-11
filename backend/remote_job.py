#!/usr/bin/env python3
"""One reconstruction on a compute node (compute.py, pullwork.py): a job directory's photos/ and masks/ in, and its
task.json's {mapper} if an area sent one (meshopts.py), its out/ (the mesh, its textures, cameras.json, orientation.json)
and new masks out. Or, with --task, another job on a
mesh/ (its files) and the photos placed in it: finding its beta (beta.py), from videos/ (its videos, as youtube's
cache keeps them), or its size (scale.py), with task.json's {climbs, videos, known}; what that makes goes in out/.

usage: remote_job.py <job dir> [--task beta|scale] [--internet] [--gpu]

Prints the pipeline's log, with its progress as "@@progress <step> <fraction> <detail as JSON>" lines
and the result as "@@result <JSON>", which the server reads back.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meshopts  # noqa: E402
import pipeline  # noqa: E402
from progress import Tracker  # noqa: E402


class PrintedTracker(Tracker):
    """A tracker that prints its progress for the server to follow."""

    def update(self, key: str, frac: float = 0.0, detail: str | None = None) -> None:
        super().update(key, frac, detail)
        print(f"@@progress {key} {frac:.4f} {json.dumps(detail) if detail is not None else ''}".rstrip(), flush=True)


# what each task makes in its mesh's folder, which goes back
MADE = {"beta": re.compile(r"beta\.json|beta_poses\.json|beta_[0-9a-f]{8}\.jpg"),
        "scale": re.compile(r"scale\.json|poses\.json|scale_people\.json|objects\.json")}


def task(job: Path, kind: str, log) -> int:
    """Find a mesh's beta or size, as the server would, and put what that made in out/."""
    from climbs import youtube
    youtube.VIDEO_CACHE, youtube.via = job / "videos", "volunteers"    # only the videos sent: never YouTube from here
    mesh, photos = job / "mesh", job / "photos"
    try:
        if kind == "beta":
            import beta
            beta.find(mesh, photos, log)
        else:
            import scale
            args = json.loads((job / "task.json").read_text())
            scale.estimate(mesh, photos, job / "masks" if (job / "masks").is_dir() else None, args.get("climbs") or [],
                           args.get("videos") or {}, log, known=args.get("known"))
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}", flush=True)
        return 1
    (job / "out").mkdir(exist_ok=True)
    made = sorted(f.name for f in mesh.iterdir() if f.is_file() and MADE[kind].fullmatch(f.name))
    for name in made:
        shutil.copy(mesh / name, job / "out" / name)
    print("@@result " + json.dumps({"mesh": "", "files": made}), flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", type=Path)
    ap.add_argument("--task", choices=("beta", "scale"))
    ap.add_argument("--internet", action="store_true")
    ap.add_argument("--gpu", action="store_true")
    a = ap.parse_args()
    log = lambda line: print(line, flush=True)  # noqa: E731
    if a.task:
        return task(a.job, a.task, log)
    try:
        given = json.loads((a.job / "task.json").read_text()) if (a.job / "task.json").exists() else {}
        with meshopts.using(given.get("mapper")):    # an area's settings for the mapper, if it sent any
            mesh, demo = pipeline.reconstruct(a.job / "photos", a.job / "work", a.job / "out", log, a.gpu, internet=a.internet,
                                              tracker=PrintedTracker(pipeline.steps()))
    except Exception as e:
        print(f"FAILED: {e}", flush=True)
        return 1
    print("@@result " + json.dumps({"mesh": mesh.name, "demo": demo}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
