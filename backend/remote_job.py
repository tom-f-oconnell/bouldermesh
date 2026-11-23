#!/usr/bin/env python3
"""One reconstruction on a compute node (compute.py, pullwork.py): a job directory's photos/ and masks/ in, and its
task.json's {mapper} if an area sent one (meshopts.py), its out/ (the mesh, its textures, cameras.json, orientation.json)
and new masks out. Or, with --task, another job on a
mesh/ (its files) and the photos placed in it: finding its beta (beta.py), from videos/ (its videos, as youtube's
cache keeps them), or its size (scale.py), with task.json's {climbs, videos, known}; what that makes goes in out/.
Or filtering a harvest's photos/ for those of other rocks (relevance.py): with more/ (anchors from its boulder's other
harvests), cache.sqlite (what the server's cache knows of them) and task.json's {methods, more, prior, matcher}; out/
gets relevance.json (its report, nothing moved) and cache.sqlite, with what it worked out; with task.json's
{diversity, order} too, diversity.json: the viewpoint check (diversity.assess_unique()) of the photos it kept, in order.
Or fitting a capture's scenery (splat.py fit_capture()): mesh/, photos/ (the capture's photos the mesh placed), masks/,
scenery/ (its frames) and task.json (splat.capture_task()); out/ gets the splat, its small copy, scene.json and the
panorama.

usage: remote_job.py <job dir> [--task beta|scale|filter|splat] [--internet] [--gpu]

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
        "scale": re.compile(r"scale\.json|poses\.json|scale_people\.json|objects\.json"),
        "splat": re.compile(r"scene\.splat|scene-small\.splat|scene\.json|scene-pano\.webp")}


def abtest_task(job: Path, gpu: bool, log) -> int:
    """Mesh the same photos with each COLMAP named in task.json's "order" ("3.9.1", "4.2.1"), one after the other on
    this machine, into out/<version>/ (the mesh, its records and timings) and out/summary.json: for each, how long it
    took, its tools' times by step, photos placed, faces, and its model's check. Meshes nothing for the site."""
    import time
    import timings
    args = json.loads((job / "task.json").read_text())
    summary = {"order": args["order"], "runs": {}}
    for version in args["order"]:
        log(f"@@abtest {version}: starting")
        out, lines = job / "out" / version, []
        t0 = time.time()
        said = lambda line, lines=lines: (lines.append(line), log(line))  # noqa: E731
        run = {"version": version}
        try:
            with pipeline.use_colmap(version), meshopts.using(args.get("mapper")):
                pipeline.reconstruct(job / "photos", job / f"work-{version}", out, said, gpu, internet=True,
                                     tracker=PrintedTracker(pipeline.steps()))
        except Exception as e:  # noqa: BLE001 - the other is still made
            run["failed"] = f"{type(e).__name__}: {e}"
        run["seconds"] = round(time.time() - t0)
        rec = timings.record("\n".join(lines), None, t0, time.time(), "here", None, False)
        run.update(tools_seconds=rec["tools_seconds"], cpu_seconds=rec["cpu_seconds"], placed=rec["placed"], photos_in=rec["photos_in"],
                   faces=timings.face_count(out), steps=[(t["step"], t["seconds"]) for t in rec["tools"]])
        for name in ("check.json", "pipeline.json", "fallbacks.json", "mapper.json"):
            try:
                run[name.split(".")[0]] = json.loads((out / name).read_text())
            except (OSError, ValueError):
                pass
        if out.is_dir():
            (out / "job.log").write_text("\n".join(lines) + "\n")
        summary["runs"][version] = run
        shutil.rmtree(job / f"work-{version}", ignore_errors=True)
    (job / "out").mkdir(parents=True, exist_ok=True)
    (job / "out" / "summary.json").write_text(json.dumps(summary, indent=1))
    print("@@result " + json.dumps({"mesh": "", "abtest": True}), flush=True)     # "mesh" is what makes it a result (compute.follow())
    return 0


def splat_task(job: Path, log) -> int:
    """Fit a capture's scenery (splat.fit_capture()) into out/."""
    import gputurn
    import splat
    tracker = PrintedTracker([("splat", "Fit the scenery", 1)])
    tracker.update("splat", 0.0)
    def said(line: str) -> None:
        log(line)
        if _step(line):
            tracker.update("splat", _step(line))
    try:
        with gputurn.turn("splat", said):
            record = splat.fit_capture(job, job / "out", said)
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}", flush=True)
        return 1
    made = sorted(f.name for f in (job / "out").iterdir() if f.is_file() and MADE["splat"].fullmatch(f.name))
    print("@@result " + json.dumps({"mesh": "", "files": made, "splats": record.get("splats")}), flush=True)
    return 0


def _step(line: str) -> float:
    """How far a splat's fitting is, from its log line ("splat: step 3001 of 15000, ..."); 0 if it doesn't say."""
    m = re.search(r"step (\d+) of (\d+)", line)
    return int(m[1]) / int(m[2]) if m else 0.0


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
    tracker = PrintedTracker([("filter", "Filter off-topic photos", 1)] +
                             ([("diversity", "Check viewpoint diversity", 1)] if args.get("diversity") else []))
    try:
        report = relevance.filter_photos(photos, None, log, methods=args.get("methods"), more_anchors=more,
                                         progress=lambda f=0.0, d=None: tracker.update("filter", f, d), prior=args.get("prior"))
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}", flush=True)
        return 1
    (job / "out").mkdir(exist_ok=True)
    (job / "out" / "relevance.json").write_text(json.dumps(report))
    if args.get("diversity"):
        diversity_task(job, [n for n in args.get("order") or [] if n in set(report["kept"])],
                       lambda f=0.0, d=None: tracker.update("diversity", f, d), log)
    if relevance.CACHE_FILE.is_file():
        shutil.copy(relevance.CACHE_FILE, job / "out" / "cache.sqlite")
    print("@@result " + json.dumps({"mesh": "", "files": sorted(f.name for f in (job / "out").iterdir())}), flush=True)
    return 0


def diversity_task(job: Path, kept: list[str], progress, log) -> None:
    """The viewpoint check of a harvest's photos kept, in the server's order, into out/diversity.json with this
    OpenCV's version, which the server uses only if its own is the same. Nothing written if it fails, or without
    OpenCV: the server then checks them itself."""
    from climbs import diversity
    try:
        import cv2
        report = diversity.assess_unique([job / "photos" / n for n in kept], log, progress=progress)
    except Exception as e:  # noqa: BLE001 - the server checks them instead
        log(f"diversity check failed here, for the server to do: {type(e).__name__}: {e}")
        return
    if (report.get("view_graph") or {}).get("available"):
        (job / "out" / "diversity.json").write_text(json.dumps({"opencv": cv2.__version__, "report": report}))


def task(job: Path, kind: str, log) -> int:
    """Find a mesh's beta or size, as the server would, and put what that made in out/."""
    from climbs import youtube
    youtube.VIDEO_CACHE, youtube.via = job / "videos", "volunteers"    # only the videos sent: never YouTube from here
    mesh, photos = job / "mesh", job / "photos"
    try:
        if kind == "beta":
            import beta
            tracker = PrintedTracker([("beta", "Find its beta", 1)])
            try:
                beta.find(mesh, photos, log, report=lambda text, at: tracker.update("beta", at["frac"], {**at, "text": text}))
            except youtube.Refused as e:    # the videos sent are done: beta.json says which are still to come
                log(f"beta: {e}: the videos sent are done")
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
    ap.add_argument("--task", choices=("beta", "scale", "filter", "splat", "abtest"))
    ap.add_argument("--internet", action="store_true")
    ap.add_argument("--gpu", action="store_true")
    a = ap.parse_args()
    log = lambda line: print(line, flush=True)  # noqa: E731
    if a.task == "filter":
        return filter_task(a.job, log)
    if a.task == "splat":
        return splat_task(a.job, log)
    if a.task == "abtest":
        return abtest_task(a.job, a.gpu, log)
    if a.task:
        return task(a.job, a.task, log)
    try:
        given = json.loads((a.job / "task.json").read_text()) if (a.job / "task.json").exists() else {}
        with meshopts.using(given.get("mapper")):    # an area's settings for the mapper, if it sent any
            kept = a.job / "kept"       # a mesh's kept reconstruction and features: its model, to add photos to
            mesh, demo = pipeline.reconstruct(a.job / "photos", a.job / "work", a.job / "out", log, a.gpu, internet=a.internet,
                                              tracker=PrintedTracker(pipeline.steps()), add_to=kept if kept.is_dir() else None)
    except Exception as e:
        print(f"FAILED: {e}", flush=True)
        return 1
    print("@@result " + json.dumps({"mesh": mesh.name, "demo": demo}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
