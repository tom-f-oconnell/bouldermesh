#!/usr/bin/env python3
"""One reconstruction on a compute node (compute.py, pullwork.py): a job directory's photos/ and masks/ in, and its
task.json's {mapper} if an area sent one (meshopts.py), its out/ (the mesh, its textures, cameras.json, orientation.json)
and new masks out. Or, with --task, another job on a
mesh/ (its files) and the photos placed in it: finding its beta (beta.py), from videos/ (its videos, as youtube's
cache keeps them), or its size (scale.py), with task.json's {climbs, videos, known}; what that makes goes in out/.
Or filtering a harvest's photos/ for those of other rocks (relevance.py): with more/ (anchors from its boulder's other
harvests), cache.sqlite (what the server's cache knows of them) and task.json's {methods, more, prior, matcher}; out/
gets relevance.json (its report, nothing moved) and cache.sqlite, with what it worked out.

usage: remote_job.py <job dir> [--task beta|scale|filter] [--internet] [--gpu]

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


def filter_task(job: Path, log) -> int:
    """Filter a harvest's photos as the server would (relevance.filter_photos()), reporting only: the server moves those
    rejected. Its cache is the one sent, so that pairs known there aren't matched again, and what's new goes back."""
    from climbs import relevance
    args = json.loads((job / "task.json").read_text())
    relevance.CACHE_FILE = job / "cache.sqlite"
    if args.get("matcher") in relevance.MATCHERS:
        relevance.MATCHER = args["matcher"]
    photos = sorted(p for p in (job / "photos").iterdir() if p.is_file())
    more = [job / m for m in args.get("more") or [] if (job / m).is_file()]
    tracker = PrintedTracker([("filter", "Filter off-topic photos", 1)])
    try:
        report = relevance.filter_photos(photos, None, log, methods=args.get("methods"), more_anchors=more,
                                         progress=lambda f=0.0, d=None: tracker.update("filter", f, d), prior=args.get("prior"))
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}", flush=True)
        return 1
    (job / "out").mkdir(exist_ok=True)
    (job / "out" / "relevance.json").write_text(json.dumps(report))
    if relevance.CACHE_FILE.is_file():
        shutil.copy(relevance.CACHE_FILE, job / "out" / "cache.sqlite")
    print("@@result " + json.dumps({"mesh": "", "files": sorted(f.name for f in (job / "out").iterdir())}), flush=True)
    return 0


def task(job: Path, kind: str, log) -> int:
    """Find a mesh's beta or size, as the server would, and put what that made in out/."""
    from climbs import youtube
    youtube.VIDEO_CACHE, youtube.via = job / "videos", "volunteers"    # only the videos sent: never YouTube from here
    mesh, photos = job / "mesh", job / "photos"
    try:
        if kind == "beta":
            import beta
            tracker = PrintedTracker([("beta", "Find its beta", 1)])
            beta.find(mesh, photos, log, report=lambda text, at: tracker.update("beta", at["frac"], {**at, "text": text}))
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
    ap.add_argument("--task", choices=("beta", "scale", "filter"))
    ap.add_argument("--internet", action="store_true")
    ap.add_argument("--gpu", action="store_true")
    a = ap.parse_args()
    log = lambda line: print(line, flush=True)  # noqa: E731
    if a.task == "filter":
        return filter_task(a.job, log)
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
