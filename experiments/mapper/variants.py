"""More methods, feature sets, steps and reports for bench.py (added 2026-11-14), kept in their own file. bench.py
imports this, takes its METHODS and FEATS entries, and hands it the commands in COMMANDS:

    bench.py sidemodels [folder]                        how much of each mapper run went on models other than the largest
    bench.py together <N> <jobs file> [every] | report  N whole jobs at once, each on cores/N (every: each on every
                                                        core, as unpartitioned workers would): meshes an hour
    bench.py texture <project> <run> <variant> | report   TextureMesh again on a finished post's mesh, one thing changed
    bench.py people <project> [run] [cpu]               the app's people masks, timed: the photos, or a run's undistorted ones
                                                        (the same on the CPU or not in an image with no GPU masks: b5607e9)
    bench.py breakdown                                  every part of a job in one row, people masks to texture

What each new method and feature set changes about which photos are placed is said where it is defined, for the
MESH_RULES decision that shipping any of them would need. Only the standard library is imported here at the top:
bench.py's containers run `variants.py poses` with the trees' bare Python.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
B = None        # bench.py's module: set by it when it imports this one
COMMANDS = ["sidemodels", "together", "texture", "people", "breakdown"]
WRAPPED = {"thin", "hier"}      # METHODS commands whose mapper step is made here (mapper()), not by bench.mapper()

# ---------------------------------------------------------------------------------------------- methods

UNTIED_LEAST = 15       # a camera seeing fewer of its model's points than this isn't placed: bench.py's UNTIED, REST_ARGS' own least
HIER_GRID = [(w, leaf) for w in (2, 3, 4, 6, 8) for leaf in (20, 40, 60, 80, 120)]
THIN = (1500, 2500)


def methods(GLOBAL: list[str]) -> dict:
    """METHODS entries: (COLMAP family, command, extra arguments, environment), as bench.py's own."""
    side = {
        # Fewer side models. COLMAP's incremental mapper starts model after model until it finds no pair to start from
        # (up to 50 models, 200 tries at a pair each), and the app keeps the smaller ones (--Mapper.min_model_size 6,
        # write_models()) for its boulder-or-group decision and for other sides of a rock. All of these change that
        # record: fewer or no smaller models come out. The largest model's own photos are placed as before, EXCEPT
        # that the first model made isn't always the largest (e6de: 6 photos, then 15): with models1 the job then
        # ends with the small one. models3 keeps the first three; trials50 gives up looking for a pair to start the
        # next model from sooner; overlap5 drops a later model once it shares 5 photos with another (default 20).
        "models1": ["--Mapper.max_num_models", "1"],
        "models3": ["--Mapper.max_num_models", "3"],
        "trials50": ["--Mapper.init_num_trials", "50"],
        "overlap5": ["--Mapper.max_model_overlap", "5"],
        "sidecut": ["--Mapper.max_num_models", "3", "--Mapper.init_num_trials", "50", "--Mapper.max_model_overlap", "5"],
    }
    out = {name: ("app", "mapper", args, {}) for name, args in side.items()}
    out.update({f"{name}_421": ("new", "mapper", args, {}) for name, args in side.items()})     # the same options in 4.2.1
    out.update({
        # The global mapper has none of those three: it reconstructs every connected part of the photos at once.
        # multiple_models 0 keeps only the largest part, so no smaller models come out at all (the app's record of
        # them is lost); the largest part's photos are placed as before.
        "global_one": ("new", "global_mapper", GLOBAL + ["--GlobalMapper.multiple_models", "0"], {}),
        # The global mapper's own thinning: tracks are taken longest first until every photo has this many
        # (track_required_tracks_per_view), or until so many in all (keep_max_num_tracks). Fewer tracks to position
        # and adjust; photos seen only in short tracks may lose their place (weakly linked ones first).
        "global_tpv1500": ("new", "global_mapper", GLOBAL + ["--GlobalMapper.track_required_tracks_per_view", "1500"], {}),
        "global_tpv2500": ("new", "global_mapper", GLOBAL + ["--GlobalMapper.track_required_tracks_per_view", "2500"], {}),
        "global_keep50k": ("new", "global_mapper", GLOBAL + ["--GlobalMapper.keep_max_num_tracks", "50000"], {}),
        "global_keep100k": ("new", "global_mapper", GLOBAL + ["--GlobalMapper.keep_max_num_tracks", "100000"], {}),
    })
    # A thinned database for the mapper (thin()): the mapper sees only each photo's N keypoints in most verified
    # matches (the <feat>_thin<N> feature set), then every keypoint of the full database is triangulated from its
    # cameras, held fixed, and everything is adjusted once. Photos the thinned mapper leaves out get their usual
    # second try in the rest step, on the full database. Weakly linked photos may be placed or not differently, and
    # only the largest model comes out (no smaller models for the app's record).
    for n in THIN:
        out[f"thin{n}_base"] = ("app", "thin", [n, "base"], {})
        out[f"thin{n}_global"] = ("new", "thin", [n, "global"], {})
    # COLMAP's hierarchical mapper (hier()): the photos are split into overlapping clusters of at most <leaf>, each
    # mapped by its own worker with the app's Mapper settings, and the models merged. hier and hier421 are 4 workers
    # and leaves of 80; hier_w<workers>_l<leaf> and hier421_w.._l.. the other sizes in HIER_GRID. Clusters that
    # don't merge stay separate models, so the largest model can be smaller than the plain mapper's (fewer photos
    # placed before the rest step); with leaves larger than the boulder's photos it is the plain mapper.
    for family, name in (("app", "hier"), ("new", "hier421")):
        out[name] = (family, "hier", [4, 80], {})
        out.update({f"{name}_w{w}_l{leaf}": (family, "hier", [w, leaf], {}) for w, leaf in HIER_GRID})
    return out


GPU_METHODS = {"global_one", "global_tpv1500", "global_tpv2500", "global_keep50k", "global_keep100k",
               *(f"thin{n}_global" for n in THIN)}
# As bench.py's global_tied: the global mapper leaves cameras in its models that see none of the model's points, and
# those are its wrong ones (atlas, 14 Nov). For every method here that runs it, a camera seeing fewer than 15 is taken
# out before the rest step, which then places it as any photo left out.
UNTIED = {m: UNTIED_LEAST for m in GPU_METHODS}

# ---------------------------------------------------------------------------------------------- feature sets

# kind: (what dbtools does, its argument). A derived set is <feature set>_<kind><number>, made from that set's
# database by derive(), and anything can be run on it with --feat.
#   lens       One lens per video: a video's frames of one size share one camera (focal length, distortion, centre)
#              where the app gives each frame its own. Fewer unknowns, and frames can't each drift to their own focal
#              length; a video that zooms is then modelled wrongly, and its frames may be placed worse or not at all.
#              In hal's five databases no video has frames of two sizes, and every frame starts from the same guess
#              (1.2 x the long side, no prior). The baseline's own models say when sharing is right: where they are
#              good, a video's frames agree to a few percent (Drifter, 31de), and where they aren't the frames of one
#              video run from 0.09 to 4.6 x the long side (9f3b). The incremental mapper treats a shared camera
#              differently, though: once one frame is placed, the others are placed with the shared focal length
#              held, not estimated (incremental_mapper.cc, "Camera already refined"), so a first guess far off the
#              truth (e6de's main video: 0.35 against the 1.2 guessed) costs it time and photos. The global mapper
#              has no such step.
#   dedup<P>   Without a video's frames that repeat a kept one: P% or more of the pair's verified matches explained
#              by a shift, turn and zoom (backend/climbs/diversity.py's measure of a pair with no parallax, which it
#              puts at 90% and only discounts). The dropped frames are in no later step: fewer photos placed by
#              exactly those frames, each a repeat of one that stays.
#   move<P>    The same, by the other rule: the verified matches to a kept frame moved less than P% of the diagonal.
#   thin<N>    At most N keypoints a photo (see the thin methods). Used as it is (--feat), the rest step is thinned
#              too, so photos added there need their 15 inliers among fewer keypoints: fewer may be placed.
KINDS = {"lens": ("lens", None), "dedup90": ("dedup", ("sim", 0.9)), "dedup75": ("dedup", ("sim", 0.75)),
         "move2": ("dedup", ("move", 0.02)), **{f"thin{n}": ("thin", n) for n in THIN}}
DERIVED_FROM = ("sift391", "siftgpu391")      # 3.9.1 databases: changed before 4.2.1 migrates a run's copy


def feats(FEATS: dict) -> dict:
    """FEATS entries for the derived sets: each as the set it is made from (the same COLMAP reads it)."""
    return {f"{base}_{kind}": FEATS[base] for base in DERIVED_FROM for kind in KINDS}


def derived(feat: str) -> tuple[str, str] | None:
    """(the set it is made from, kind) of a derived feature set's name."""
    base, _, kind = feat.rpartition("_")
    return (base, kind) if base in DERIVED_FROM and kind in KINDS else None


def derive(P: str, feat: str, build: str) -> Path:
    """A derived feature set's folder, its database made (once) from the set it derives from. What changed, and the
    seconds it took, are in derive.json there."""
    import dbtools
    base, kind = derived(feat)
    d = B.feat_dir(P, feat)
    if (d / "derive.json").exists() and (d / "database.db").exists():
        return d
    src = B.feat_db(P, base, build)
    if not src.exists():
        B.features(P, base, "dev", build)
    d.mkdir(parents=True, exist_ok=True)
    what, arg = KINDS[kind]
    start = time.time()
    tmp = d / "database.tmp.db"
    result = (dbtools.lens(src, tmp) if what == "lens" else dbtools.thin(src, tmp, arg) if what == "thin"
              else dbtools.dedup(src, tmp, *arg))
    problems = dbtools.check(tmp)
    if problems:
        raise RuntimeError(f"{feat}: the changed database is unsound: {problems[:3]}")
    tmp.rename(d / "database.db")
    result.update(seconds=round(time.time() - start, 1), made_from=str(src), when=time.strftime("%F %T"))
    (d / "derive.json").write_text(json.dumps(result, indent=1))
    for name in ("extract", "match"):       # as bench.features() marks a database it didn't have to make
        (d / f"{name}.json").write_text(json.dumps({"step": name, "rc": 0, "derived": str(src)}))
    print(f"{time.strftime('%T')} {d.relative_to(B.R)} derived in {result['seconds']} s: " + ", ".join(
        f"{k} {result['before'][k]} -> {result['after'][k]}" for k in ("cameras", "images", "keypoints", "verified_pairs")
        if result["before"][k] != result["after"][k]), flush=True)
    return d


# ---------------------------------------------------------------------------------------------- wrapped mappers

def _fresh(d: Path) -> None:
    """A run's folder, empty, as bench.mapper() starts one (an interrupted run's root-owned leftovers put aside)."""
    shutil.rmtree(d, ignore_errors=True)
    if d.exists():
        d.rename(d.with_name(f"{d.name}.stale{int(time.time())}"))
    (d / "sparse").mkdir(parents=True)


def mapper(P: str, method: str, seed: int, feat: str, cond: str, build: str, rep: int) -> str:
    """bench.mapper() for the methods in WRAPPED. Returns the run's name."""
    return (thin if B.METHODS[method][1] == "thin" else hier)(P, method, seed, feat, cond, build, rep)


def hier(P: str, method: str, seed: int, feat: str, cond: str, build: str, rep: int) -> str:
    """One run of COLMAP's hierarchical mapper: clusters of at most <leaf> photos sharing a quarter of that (at least
    10) with their neighbours, <workers> of them mapped at once. 3.9.1 writes the merged model straight into the
    output folder and 4.2.1 into numbered ones: either ends up as sparse/<n>."""
    family, _, (workers, leaf), env = B.METHODS[method]
    tools = "app" if family == "app" else build
    run = B.run_name(method, seed, cond, build, rep)
    d = B.feat_dir(P, feat) / run
    if (d / "map.json").exists():
        return run
    if not B.feat_db(P, feat, build).exists():
        B.features(P, feat, "dev", build)
    _fresh(d)
    shutil.copyfile(B.feat_db(P, feat, build), d / "database.db")
    sizes = [("num_workers", workers), ("leaf_max_num_images", leaf), ("image_overlap", max(10, leaf // 4))]
    if family == "app":
        args = ["--random_seed", seed, *(x for k, v in sizes for x in (f"--{k}", v))]
    else:
        args = ["--log_target", "stderr", "--default_random_seed", seed, "--Mapper.random_seed", seed,
                *(x for k, v in sizes for x in (f"--HierarchicalMapper.{k}", v))]
    script = (B.sh(["colmap", "hierarchical_mapper", "--database_path", "/r/database.db", "--image_path", "/photos",
                    "--output_path", "/r/sparse", *args, *B.INC, *B.threads(cond, "--Mapper.num_threads")])
              + "\nif [ -f /r/sparse/images.bin ]; then mkdir -p /r/sparse/0 && mv /r/sparse/*.bin /r/sparse/0/; fi")
    B.step(d, "map", tools, script, [f"{B.BENCH}/projects/{P}/photos:/photos:ro", f"{d}:/r"], cond, env, timeout=14 * 3600)
    (d / "database.db").unlink(missing_ok=True)
    (d / "models.json").write_text(json.dumps(B.models_of(d / "sparse")))
    return run


def thin(P: str, method: str, seed: int, feat: str, cond: str, build: str, rep: int) -> str:
    """A mapper on a thinned database, then the full one brought back: the method's mapper runs on <feat>_thin<N>
    (its own run, under that feature set); its largest model's cameras, without points, go to point_triangulator on
    the full database (cameras fixed), then one bundle_adjuster (on the GPU for 4.2.1). Cameras the mapper placed
    that see fewer than UNTIED_LEAST of its points are left out of that (BENCH_UNTIED in the step's log says how
    many). The run's map.json is the three together: seconds for thinning, the mapper and the full step, under "parts"."""
    family, _, (n, inner), _ = B.METHODS[method]
    tools, new = ("app", False) if family == "app" else (build, True)
    run = B.run_name(method, seed, cond, build, rep)
    d = B.feat_dir(P, feat) / run
    if (d / "map.json").exists():
        return run
    tfeat = f"{feat}_thin{n}"
    if tfeat not in B.FEATS:
        raise RuntimeError(f"no thinned set of {feat}: only of {DERIVED_FROM}")
    irun = B.mapper(P, inner, seed, tfeat, cond, build, rep)
    idir = B.feat_dir(P, tfeat) / irun
    made = json.loads((idir / "map.json").read_text())
    models = json.loads((idir / "models.json").read_text())
    parts = {"thin": json.loads((B.feat_dir(P, tfeat) / "derive.json").read_text())["seconds"], "map": made["wall"]}
    rc, cpu = made["rc"], made["cpu_s"]
    _fresh(d)
    if models:
        best = max(models, key=lambda m: len(models[m]))
        shutil.copyfile(B.feat_db(P, feat, build), d / "full.db")        # a copy: 4.2.1 migrates what it opens
        log = ["--log_target", "stderr"] if new else []
        script = "\n".join([
            "mkdir -p /r/txt /r/poses /r/tri /r/sparse/0",
            B.sh(["colmap", "model_converter", *log, "--input_path", "/in", "--output_path", "/r/txt", "--output_type", "TXT"]),
            # cameras that see next to none of the mapper's points (the global mapper leaves such in, wrongly placed)
            # give the triangulation nothing true: left out here, for the rest step to place
            f"python3 /bench/stats.py untied /r/txt {UNTIED_LEAST} > /r/untied.txt; SRC=/r/txt",
            "if [ -s /r/untied.txt ]; then mkdir -p /r/tied /r/txt2; "
            + B.sh(["colmap", "image_deleter", *log, "--input_path", "/r/txt", "--output_path", "/r/tied", "--image_names_path",
                    "/r/untied.txt"]) + "; "
            + B.sh(["colmap", "model_converter", *log, "--input_path", "/r/tied", "--output_path", "/r/txt2", "--output_type", "TXT"])
            + "; SRC=/r/txt2; fi",
            "echo BENCH_UNTIED $(wc -l < /r/untied.txt)",
            "python3 /bench/variants.py poses $SRC /r/poses",
            "echo BENCH_MARK triangulate $(date +%s.%N)",
            B.sh(["colmap", "point_triangulator", *log, "--database_path", "/r/full.db", "--image_path", "/photos",
                  "--input_path", "/r/poses", "--output_path", "/r/tri", *B.INC, *B.threads(cond, "--Mapper.num_threads")]),
            "echo BENCH_MARK adjust $(date +%s.%N)",
            B.sh(["colmap", "bundle_adjuster", *log, "--input_path", "/r/tri", "--output_path", "/r/sparse/0",
                  *(["--BundleAdjustmentCeres.use_gpu", "1"] if new else [])]),
            "echo BENCH_MARK done $(date +%s.%N)"])
        full = B.step(d, "full", tools, script, [f"{B.BENCH}/projects/{P}/photos:/photos:ro", f"{d}:/r",
                                                 f"{idir}/sparse/{best}:/in:ro"], cond, timeout=4 * 3600, gpu=new)
        (d / "full.db").unlink(missing_ok=True)
        parts["full"], rc, cpu = full["wall"], rc or full["rc"], cpu + full["cpu_s"]
    (d / "models.json").write_text(json.dumps(B.models_of(d / "sparse")))
    (d / "map.json").write_text(json.dumps({
        "step": "map", "tools": made["tools"], "runner": made.get("runner", ""), "cond": cond, "rc": rc,
        "wall": round(sum(parts.values()), 1), "cpu_s": round(cpu, 1), "started": made["started"], "parts": parts,
        "mapper_run": str(idir.relative_to(B.R)), "mapper_models": {k: len(v) for k, v in models.items()},
        "host": made.get("host", {}), "verify": made.get("verify", {})}, indent=1))
    print(f"{time.strftime('%T')} {d.relative_to(B.R)} map: {sum(parts.values()):.0f} s in all ("
          + ", ".join(f"{k} {v:.0f}" for k, v in parts.items()) + f"), rc {rc}", flush=True)
    return run


def poses(src: Path, dst: Path) -> None:
    """dst: the text model src with its cameras where they are and no points: each image's line of observations
    emptied, points3D.txt empty, every other file (cameras, and 4.2.1's rigs and frames) as it is. What
    point_triangulator needs to triangulate another database's keypoints from those cameras."""
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.name not in ("images.txt", "points3D.txt"):
            shutil.copyfile(f, dst / f.name)
    lines = (src / "images.txt").read_text().split("\n")
    head = [l for l in lines if l.startswith("#")]
    body = [l for l in lines if not l.startswith("#")]
    if body and body[-1] == "" and len(body) % 2:        # the file's last newline, not an image's empty second line
        body.pop()
    (dst / "images.txt").write_text("\n".join(head + [x for image in body[0::2] for x in (image, "")]) + "\n")
    (dst / "points3D.txt").write_text("# no points: to be triangulated\n")


# ---------------------------------------------------------------------------------------------- side models report

STAMP = re.compile(r"^[IWEF](\d{8}) (\d\d):(\d\d):(\d\d\.\d+)")
REGISTERED = re.compile(r"Registering image #\d+ \((?:num_reg_frames=)?(\d+)\)")


def attempts(text: str) -> tuple[float | None, float | None, list[dict]]:
    """A mapper log as (its first time, its last, each model it started): a model starts at "Finding good initial
    image pair" and has a start, an end (the next one's start, or the log's) and the most photos it reached. Times
    are the log's own stamps, in seconds; lines without one (most of 3.9.1's) take the last line's before them."""
    first = now = None
    out = []
    for line in text.split("\n"):
        if m := STAMP.match(line):
            day = datetime.strptime(m.group(1), "%Y%m%d").toordinal() * 86400
            now = day + int(m.group(2)) * 3600 + int(m.group(3)) * 60 + float(m.group(4))
            first = now if first is None else first
        if "Finding good initial image pair" in line:
            if out:
                out[-1]["end"] = now
            out.append({"start": now, "end": now, "size": 0})
        elif out and (m := REGISTERED.search(line)):
            out[-1]["size"] = max(out[-1]["size"], int(m.group(1)))
        elif out and ("Registering initial image pair" in line or "Initializing with image pair" in line):
            out[-1]["size"] = max(out[-1]["size"], 2)
    if out:
        out[-1]["end"] = now
    return first, now, out


def sidemodels(folder: Path | None = None) -> None:
    """For every incremental mapper log under the results (or a folder of them): the seconds before the largest
    model was started, in it, and after it was finished, and the share of the run that wasn't the largest model.
    "largest" and "second" are the most photos the largest and the next largest model reached while being made (a
    second of dozens of photos is another part of the rock, not a stray: the app keeps it). Costs nothing: it
    reads map.log files that exist. The global mapper has no such models and is left out."""
    cols = ["boulder", "feat", "run", "wall_s", "models_started", "largest", "second", "before_s", "largest_s", "after_s", "side_share"]
    print("\t".join(cols))
    root = folder or B.R
    for log in sorted(root.glob("**/map.log")):
        if ".stale" in str(log):
            continue
        first, last, models = attempts(log.read_text(errors="replace"))
        models = [m for m in models if m["start"] is not None]
        if not models or first is None or last <= first:
            continue
        big = max(models, key=lambda m: m["size"])
        rec = log.with_suffix(".json")
        wall = json.loads(rec.read_text()).get("wall", "") if rec.exists() else ""
        before, inside, after = big["start"] - first, big["end"] - big["start"], last - big["end"]
        parts = log.relative_to(root).parts
        print("\t".join(str(x) for x in [*(["", "", ""] + list(parts[:-1]))[-3:], round(wall, 1) if wall != "" else "",
                                         len(models), big["size"], sorted(m["size"] for m in models)[-2] if len(models) > 1 else 0,
                                         round(before, 1), round(inside, 1), round(after, 1),
                                         round((before + after) / (last - first), 3)]))


# ---------------------------------------------------------------------------------------------- jobs side by side

STAGGER = 5             # seconds between one job's start and the next's in a batch (two containers starting on one tree
#                         in the same second can fail to start: bench.py's START_RACE, which step() also tries again)
NO_START = "Docker couldn't start the container"
RETRY_WAIT = 15         # seconds before a job that ended that way is run again (twice at most; its made steps are kept)
SMALL = 10              # a model of fewer photos than this that then fails a post step had nothing to make a mesh of
# what an app job's log says when one of its GPU steps went to the CPU for want of GPU memory, or failed on the GPU
GPU_FELL_BACK = re.compile(r"only \d+ MB of GPU memory free|GPU run failed|out of memory", re.I)

def job_parts(line: str, cond: str) -> tuple[list[str], str, str, str]:
    """(a job line's words with its condition replaced, its project, feature set and build). An app line (app
    <project> [--image ..]) has no features of its own: its feature set and build are bench.py's defaults, unused."""
    words = shlex.split(line)
    for flag in ("--cond",):
        while flag in words:
            i = words.index(flag)
            del words[i:i + 2]
    get = lambda flag, default: words[words.index(flag) + 1] if flag in words else default      # noqa: E731
    return words + ["--cond", cond], words[1], get("--feat", "sift391"), get("--build", "plain")


def together(n: int, jobs_file: Path, every_core: bool = False) -> None:
    """Runs a file's jobs (bench.py lines: all ..., usually with --post) N at a time, each held to cores/N cores and
    as many threads ("c<cores/N>"), in a results folder of its own (<BENCH>/together/<file>-n<N>, as BENCH_R), so
    that nothing is skipped as already made. GPU steps still take gpu.lock one at a time. Features come from the
    main results (linked, made first if missing): they aren't part of what is timed.

    A file of the app's own jobs (app <project> --image ..., bench.app()) runs them whole, photos to textured mesh,
    the same way but with no gpu.lock (BENCH_NO_GPU_LOCK): their GPU steps share the card as two of the app's workers
    on one machine would. Each job's photos and work are deleted as it ends (BENCH_APP_CLEAN), its mesh and records
    kept. Each job's CPU-seconds, its own and the card's peak GPU memory, and any of its GPU steps that went to the
    CPU for want of memory or failed on the GPU (GPU_FELL_BACK) are kept with it. every_core: each job on every core
    (no --cpus; the cap's cores where there is one), as workers that don't share out the cores would run them, in
    <file>-n<N>-every.

    Saves and prints: the seconds from the first job's start to the last one's end, the jobs that ended with a mesh
    (a textured one where the job asked for the post steps, else a model), meshes an hour, and each job's own
    steps. A job that made no mesh says why (job_result()): a boulder whose model comes out too small at this
    core count isn't the batch failing, and jobs_per_hour counts every job that ran to its end. Jobs start STAGGER
    seconds apart, and one whose container couldn't be started is run again (up to twice), keeping its made steps.
    A batch already made is only printed again: move its folder away to make it again.
    """
    jobs = [l for l in jobs_file.read_text().split("\n") if l.strip() and not l.startswith("#")]
    kinds = {shlex.split(l)[0] for l in jobs}
    assert kinds in ({"all"}, {"app"}), f"a batch is benchmark jobs (all ...) or the app's own (app ...), not {sorted(kinds)}"
    app_jobs = kinds == {"app"}
    root = B.BENCH / "together" / (f"{jobs_file.stem}-n{n}" + ("-every" if every_core else ""))
    if (root / "together.json").exists():
        together_report([root])
        return
    if root.exists():       # an interrupted batch: its steps would be skipped and the batch's time mean nothing
        root.rename(root.with_name(f"{root.name}.stale{int(time.time())}"))
    cores = B.cap() or os.cpu_count()
    per = cores if every_core else max(1, cores // n)
    cond = (f"c{cores}" if B.cap() else "prod") if every_core else f"c{per}"
    ram = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1)) / 2 ** 20
    mem = min(int(ram * 0.8 / n), int(re.sub(r"\D", "", B.MEM) or 64))
    commands = []
    for line in jobs:
        words, P, feat, build = job_parts(line, cond)
        if app_jobs:
            commands.append(words)
            continue
        assert words[2] in B.METHODS, f"together runs whole jobs (all <project> <method> <seed>): {line}"
        db = B.feat_db(P, feat, build)
        if not db.exists():
            B.features(P, feat, "dev", build)
        dst = root / db.parent.relative_to(B.R)
        dst.mkdir(parents=True, exist_ok=True)
        for f in db.parent.iterdir():
            if f.is_file() and f.suffix in (".db", ".json") and not (dst / f.name).exists():
                (dst / f.name).symlink_to(f)
        commands.append(words)
    root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, BENCH_R=str(root), MEM=f"{mem}g", **({"BENCH_NO_GPU_LOCK": "1", "BENCH_APP_CLEAN": "1"} if app_jobs else {}))
    print(f"{time.strftime('%T')} together: {len(jobs)} {'app ' if app_jobs else ''}jobs, {n} at a time, "
          f"{'every one' if every_core else per} of {cores} cores and {mem} GB each, in {root}", flush=True)

    gate, last = threading.Lock(), [0.0]

    def one(i: int) -> dict:
        with gate:              # no two jobs' first containers in the same second
            time.sleep(max(0.0, last[0] + STAGGER - time.time()))
            last[0] = time.time()
        start, log, tries = time.time(), root / f"job{i}.log", 0
        while True:
            tries += 1
            with open(log, "a") as f:
                f.write(f"# try {tries}: {B.sh(commands[i])}\n")
                f.flush()
                rc = subprocess.run([sys.executable, str(HERE / "bench.py"), *commands[i]], stdout=f, stderr=subprocess.STDOUT,
                                    env=env).returncode
            text = log.read_text(errors="replace").rsplit("# try ", 1)[-1]
            if rc == 0 or tries > 2 or NO_START not in text:
                break
            print(f"{time.strftime('%T')} together: job {i + 1}'s container couldn't be started; again in {RETRY_WAIT} s", flush=True)
            time.sleep(RETRY_WAIT)
        print(f"{time.strftime('%T')} together: job {i + 1} of {len(jobs)} done in {time.time() - start:.0f} s (exit {rc}): "
              f"{B.sh(commands[i])}", flush=True)
        return {"job": B.sh(commands[i]), "start": start, "end": time.time(), "exit": rc, "tries": tries,
                "no_start": rc != 0 and NO_START in text, "last_line": ([l for l in text.split("\n") if l.strip()] or [""])[-1][-200:]}

    with ThreadPoolExecutor(n) as pool:
        done = list(pool.map(one, range(len(jobs))))
    wall = max(j["end"] for j in done) - min(j["start"] for j in done)
    for j, words in zip(done, commands):
        j.update(job_result(root, words, j))
    meshes, ran = sum(j["mesh"] for j in done), sum(j["exit"] == 0 for j in done)
    peaks = [j["card_mib_max"] for j in done if j.get("card_mib_max") is not None]
    (root / "together.json").write_text(json.dumps({
        "jobs_file": str(jobs_file), "kind": "app" if app_jobs else "all", "every_core": every_core,
        "at_a_time": n, "cores_each": per, "cores": cores, "memory_gb_each": mem,
        "wall": round(wall, 1), "meshes": meshes, "meshes_per_hour": round(meshes / wall * 3600, 2) if wall else 0,
        "jobs_ran": ran, "jobs_per_hour": round(ran / wall * 3600, 2) if wall else 0,
        "cpu_s": round(sum(j.get("cpu_s") or 0 for j in done), 1), "card_mib_max": max(peaks) if peaks else None,
        "gpu_fallbacks": sum(len(j.get("gpu_fallbacks", [])) for j in done),
        "when": time.strftime("%F %T"), "jobs": done}, indent=1))
    together_report([root])


def job_result(root: Path, words: list[str], ended: dict | None = None) -> dict:
    """What a finished job left in a batch's folder: its steps' seconds, the seconds its GPU steps waited for the
    lock, the photos its model places after the rest step, whether it ended with what it was asked for (a textured
    mesh if it had post steps, else a model), and if not, why (ended: how its process ended, from together())."""
    if words[0] == "app":
        return app_result(root, words, ended)
    steps, waited, failed = {}, 0.0, {}
    P, method, seed = words[1], words[2], words[3]
    wants_mesh = "--post" in words
    textured, placed = False, None
    for rec_file in sorted((root / P).glob("*/*/**/*.json")):
        if not re.match(rf"{re.escape(method)}-s{seed}\b", rec_file.relative_to(root / P).parts[1]):
            continue
        rec = json.loads(rec_file.read_text())
        if rec_file.name == "placed.json":
            placed = len(rec)
        if not isinstance(rec, dict) or "wall" not in rec or rec["step"] == "full":      # full: within a thin run's map
            continue
        steps[rec["step"]] = round(steps.get(rec["step"], 0) + rec["wall"], 1)
        waited += rec.get("lock_wait", 0) or 0
        if rec["rc"] != 0:
            failed[rec["step"]] = rec["rc"]
        textured |= rec["step"] == "texture" and rec["rc"] == 0
    mesh = bool(textured if wants_mesh else steps.get("map") and "map" not in failed)
    why = ""
    if not mesh:
        ended = ended or {}
        first = next(iter(failed), None)
        if ended.get("no_start"):
            why = f"its container couldn't be started ({ended.get('tries', 1)} tries)"
        elif "map" not in steps:
            why = f"the mapper never ran: {ended.get('last_line', '')}".rstrip(": ")
        elif first in ("dense", "mesh", "texture") and placed is not None and placed < SMALL:
            why = f"its model has only {placed} photos at this core count: nothing to {first} (rc {failed[first]})"
        elif first:
            why = f"{first} failed (rc {failed[first]})" + (f", on a model of {placed} photos" if placed is not None else "")
        elif placed == 0:
            why = "no photos placed"
        else:
            why = f"stopped before texture: {ended.get('last_line', '')}".rstrip(": ")
    return {"steps": steps, "steps_s": round(sum(steps.values()), 1), "lock_wait_s": round(waited, 1), "failed": list(failed),
            "placed": placed, "mesh": mesh, "why_no_mesh": why}


def app_result(root: Path, words: list[str], ended: dict | None = None) -> dict:
    """job_result() for an app job (bench.app()): the pipeline's own steps' seconds (its steps.json), the whole job's
    seconds and CPU-seconds, the photos placed and whether it made a mesh with faces (metrics.json), its own and the
    card's peak GPU memory while it ran, and the lines of its log where a GPU step went to the CPU or failed there."""
    get = lambda flag, default: words[words.index(flag) + 1] if flag in words else default      # noqa: E731
    mapper = get("--app-mapper", "")
    run = B.app_name(get("--image", B.APP), get("--cond", "dev"), int(get("--rep", "1"))) + (f"+{mapper}" if mapper else "")
    d = root / words[1] / "app" / run
    read = lambda name: json.loads((d / name).read_text()) if (d / name).exists() else {}     # noqa: E731
    rec, metrics = read("app.json"), read("metrics.json")
    steps = {s["key"]: s["seconds"] for s in read("steps.json").get("steps", []) if s.get("seconds") is not None}
    log = (d / "app.log").read_text(errors="replace") if (d / "app.log").exists() else ""
    fell_back = [l.strip()[:200] for l in log.split("\n") if GPU_FELL_BACK.search(l)]
    host = rec.get("host", {})
    mesh = rec.get("rc") == 0 and (metrics.get("faces") or 0) > 0
    why = ""
    if not mesh:
        ended = ended or {}
        last = ([l for l in log.split("\n") if l.strip()] or [ended.get("last_line", "")])[-1][-200:]
        if ended.get("no_start"):
            why = f"its container couldn't be started ({ended.get('tries', 1)} tries)"
        elif not rec:
            why = f"it never ran: {ended.get('last_line', '')}".rstrip(": ")
        elif rec.get("rc") != 0:
            why = f"the job failed (rc {rec.get('rc')}): {last}"
        else:
            why = f"no mesh made ({metrics.get('placed', 0)} photos placed): {last}"
    return {"steps": steps, "steps_s": rec.get("wall", 0.0), "cpu_s": rec.get("cpu_s"), "lock_wait_s": 0.0,
            "failed": [] if mesh or not rec else ["app"], "placed": metrics.get("placed"), "faces": metrics.get("faces"),
            "mesh": mesh, "why_no_mesh": why, "gpu_mib_max": host.get("own_mib_max"), "card_mib_max": host.get("card_mib_max"),
            "gpu_fallbacks": fell_back}


def together_report(roots: list[Path] | None = None) -> None:
    """Every batch (or those given): how many jobs at a time, the wall time, meshes an hour and jobs an hour (every job
    that ran to its end, mesh or not), then each job with, where it made no mesh, why."""
    roots = roots or sorted(p.parent for p in (B.BENCH / "together").glob("*/together.json"))
    print("batch\tat_a_time\tcores_each\tjobs\tmeshes\twall_s\tmeshes_per_hour\tjobs_ran\tjobs_per_hour\tjobs_steps_s\tlock_wait_s"
          "\tcpu_s\tcard_mib_max\tgpu_fallbacks")
    saved = [(r, json.loads((r / "together.json").read_text())) for r in roots]
    for r, t in saved:
        ran = t.get("jobs_ran", sum(j.get("exit") == 0 for j in t["jobs"]))
        print("\t".join(str(x) for x in [r.name, t["at_a_time"], t["cores_each"], len(t["jobs"]), t["meshes"], t["wall"],
                                         t["meshes_per_hour"], ran, t.get("jobs_per_hour", round(ran / t["wall"] * 3600, 2) if t["wall"] else 0),
                                         round(sum(j["steps_s"] for j in t["jobs"]), 1),
                                         round(sum(j["lock_wait_s"] for j in t["jobs"]), 1),
                                         t.get("cpu_s", ""), t.get("card_mib_max", ""), t.get("gpu_fallbacks", "")]))
    print("\nbatch\tjob\ttook_s\ttries\tplaced\tmesh\twhy_no_mesh\tlock_wait_s\tsteps\tcpu_s\tgpu_mib_max\tcard_mib_max\tgpu_fallbacks")
    for r, t in saved:
        for j in t["jobs"]:
            why = j.get("why_no_mesh") or ("" if j["mesh"] else "failed: " + ",".join(j.get("failed", [])))
            print("\t".join(str(x) for x in [r.name, j["job"], round(j["end"] - j["start"], 1), j.get("tries", 1),
                                             "" if j.get("placed") is None else j["placed"], "yes" if j["mesh"] else "no", why,
                                             j["lock_wait_s"], " ".join(f"{k}={v}" for k, v in j["steps"].items()),
                                             j.get("cpu_s", ""), j.get("gpu_mib_max", ""), j.get("card_mib_max", ""),
                                             " | ".join(j.get("gpu_fallbacks", []))]))


# ---------------------------------------------------------------------------------------------- texture experiments

# variant: (TextureMesh arguments changed from the app's, whether only OpenMVS 2.4 has them). Every option name is in
# both 2.3's --help and 2.4's source except --views-file, which is 2.4's. None changes which photos are placed: they
# change the texture's detail, seams and size, and (views) which photos colour the mesh.
TEXTURE = {
    "same": ([], False),                                        # the app's own settings again: the reference
    "res1": (["--resolution-level", "1"], False),               # photos at half size for texturing (0, full, is the app's)
    "res2": (["--resolution-level", "2"], False),               # at a quarter
    "dec50": (["--decimate", "0.5"], False),                    # the mesh simplified to half its faces first
    "dec25": (["--decimate", "0.25"], False),
    "noglobal": (["--global-seam-leveling", "0"], False),       # no colour evening across the whole mesh
    "nolocal": (["--local-seam-leveling", "0"], False),         # no blending at each seam
    "noseams": (["--global-seam-leveling", "0", "--local-seam-leveling", "0"], False),
    "size2048": (["--max-texture-size", "2048"], False),        # the app's is 4096
    "size8192": (["--max-texture-size", "8192"], False),
    "views2": (["--views-file", "VIEWS"], True),                # every second photo colours the mesh (a cap on views)
    "views3": (["--views-file", "VIEWS"], True),                # every third
    # OpenMVS's own way to choose views for groups of coplanar triangles at once (2.4's help: "3 - good value"). In 2.4's
    # source; not checked against 2.3's --help, so use it with --tools of a 2.4 tree
    "vface3": (["--virtual-face-images", "3"], False),
}


def texture(P: str, run: str, variant: str, feat: str, tools: str, cond: str, mvs: str = "") -> None:
    """TextureMesh again on a run's finished post steps (its scene and mesh, untouched), with one thing changed:
    the step tex_<variant> beside the post's own, its GLB kept in tex_<variant>/. Then, untimed, what came out
    (texstats.py, in the app's image): the GLB's size, its textures' pixels and how much of them is used, and a
    sharpness number, in tex_<variant>/texstats.json."""
    args, only_new = TEXTURE[variant]
    new = tools != "app"
    if only_new and not new:
        raise RuntimeError(f"OpenMVS 2.3 has no {args[0]}")
    d = B.feat_dir(P, feat) / run
    o = d / (f"post-{tools}" + (f"+{mvs}" if mvs else "") + (f"@{cond}" if cond != "dev" else ""))      # mvs: bench.py's post --mvs
    if not (o / "scene_dense.mvs").exists() or not (o / "scene_dense_mesh.ply").exists():
        raise RuntimeError(f"{o.relative_to(B.R)} has no dense scene and mesh: run its post steps first")
    name = f"tex_{variant}"
    size = args[args.index("--max-texture-size") + 1] if "--max-texture-size" in args else "4096"
    more = [a for i, a in enumerate(args) if a != "--max-texture-size" and (i == 0 or args[i - 1] != "--max-texture-size")]
    more = [f"{name}/views.txt" if a == "VIEWS" else a for a in more]
    script = f"cd /o\nmkdir -p {name}\n"
    if variant.startswith("views"):
        script += (f"ls dense/images | grep -vE '\\.mask\\.png$' | awk 'NR % {int(variant[5:])} == 1' > {name}/views.txt\n"
                   f"echo BENCH_VIEWS $(wc -l < {name}/views.txt) of $(ls dense/images | grep -cvE '\\.mask\\.png$')\n")
    script += B.sh([f"{B.MVS_NEW if new else B.MVS_APP}/TextureMesh", "scene_dense.mvs", "-m", "scene_dense_mesh.ply", "-o",
                    f"{name}/scene_dense_mesh_texture.mvs", "-w", "/o", "--export-type", "glb", "--empty-color", str(0x808080),
                    "--max-texture-size", size, *more, *B.threads(cond, "--max-threads")])
    mounts = [f"{B.BENCH}/projects/{P}/photos:/photos:ro", f"{d}/rest/rest2:/model:ro", f"{o}:/o"]
    rec = B.step(o, name, tools, script, mounts, cond, timeout=4 * 3600, gpu=new)
    if rec["rc"] == 0:
        B.step(o, f"{name}_stats", "app", f"python3 /bench/texstats.py /o/{name}", mounts, "dev")


def texture_report() -> None:
    """Every texture experiment: its time beside what came out. sharpness is the spread of each used texel against
    its neighbours (texstats.py): compare it between variants of one mesh, with used_mpx (how many texels there are)."""
    cols = ["boulder", "feat", "run", "post", "variant", "tools", "rc", "wall_s", "cpu_s", "glb_mb", "textures", "mpx", "used_mpx",
            "sharpness", "faces"]
    print("\t".join(cols))
    for rec_file in sorted(B.R.glob("*/*/*/post-*/tex_*.json")):
        if rec_file.stem.endswith("_stats"):
            continue
        rec = json.loads(rec_file.read_text())
        stats_file = rec_file.with_suffix("") / "texstats.json"
        s = json.loads(stats_file.read_text()) if stats_file.exists() else {}
        P, feat, run, post = rec_file.relative_to(B.R).parts[:4]
        print("\t".join(str(x) for x in [P, feat, run, post, rec_file.stem[4:], rec["tools"], rec["rc"], round(rec["wall"], 1),
                                         rec["cpu_s"], s.get("glb_mb", ""), s.get("textures", ""), s.get("mpx", ""),
                                         s.get("used_mpx", ""), s.get("sharpness", ""), s.get("faces", "")]))


# ---------------------------------------------------------------------------------------------- people masks

def people(P: str, run: str | None, device: str, feat: str, tools: str, cond: str) -> None:
    """The app's people masks as a timed step, with the app image's own code (people_step.py): for a boulder's
    photos (the step "people" in <project>/people-<device>[@cond]/), or for a run's undistorted photos (the app's
    second pass: "people2" in the run's post folder, its masks kept apart in people2-<device>/). device "gpu" lets
    the app choose as it does (the GPU if it has room), "cpu" keeps it off (PEOPLE_GPU=0). The image in use decides
    what that means: the live one (b5607e9) has no GPU path for masks, so both are its CPU pass and take the same
    time (atlas, 14 Nov); the GPU pass needs an image built from e0898a3 or later, named with APP=<image>. Each
    step's log says where it ran (BENCH_PEOPLE device=). The model's loading (and, once, its 42 MB download) is in
    the step's time and also marked on its own (BENCH_MARK loaded)."""
    assert device in ("gpu", "cpu"), device
    env = {"DATA_DIR": "/data", "PEOPLE_GPU": "1" if device == "gpu" else "0"}
    cache = B.BENCH / "cache" / "models"
    cache.mkdir(parents=True, exist_ok=True)
    if run is None:
        d = B.R / P / (f"people-{device}" + (f"@{cond}" if cond != "dev" else ""))
        d.mkdir(parents=True, exist_ok=True)
        mounts = [f"{B.BENCH}/projects/{P}/photos:/photos:ro", f"{d}:/w", f"{cache}:/data/cache/models"]
        B.step(d, "people", "app", "python3 /bench/people_step.py /photos /w/masks '{name}.png'", mounts, cond, env,
               gpu=device == "gpu")
    else:
        o = B.feat_dir(P, feat) / run / (f"post-{tools}" + (f"@{cond}" if cond != "dev" else ""))
        if not (o / "dense" / "images").is_dir():
            raise RuntimeError(f"{o.relative_to(B.R)} has no undistorted photos: run its post steps first")
        mounts = [f"{o}:/o", f"{cache}:/data/cache/models"]
        B.step(o, f"people2_{device}", "app", f"python3 /bench/people_step.py /o/dense/images /o/people2-{device} '{{stem}}.mask.png'",
               mounts, cond, env, gpu=device == "gpu")


# ---------------------------------------------------------------------------------------------- the whole job

def breakdown() -> None:
    """Each run with post steps as one row: every part of a job in seconds, from people masks to texture, and their
    sum. Features and matching are the timed ones under the run's own condition where they exist (feat_timing says
    which were used), else the untimed ones every run starts from ("dev"; blank if adopted with no time). People
    masks are the boulder's people-gpu (else people-cpu) step, and people2 the post folder's, where they were run."""
    steps = ["people", "extract", "match", "map", "rest", "people2", "undistort", "import", "dense", "mesh", "texture"]
    print("\t".join(["boulder", "photos", "feat", "run", "post", "feat_timing", *steps, "total_s", "missing"]))

    def wall(f: Path):
        if not f.exists():
            return None
        rec = json.loads(f.read_text())
        return rec["wall"] if rec.get("rc", 1) == 0 and "wall" in rec else None

    for post in sorted(B.R.glob("*/*/*/post-*")):
        P, feat, run, post_name = post.relative_to(B.R).parts
        if ".stale" in run or not (post / "texture.json").exists():
            continue
        _, build, cond = B.run_parts(run)
        suffix = f"@{cond}" if cond != "dev" else ""
        timed = [B.feat_dir(P, feat, cond, build), B.feat_dir(P, feat, cond, "plain")] if cond != "dev" else []
        fdir = next((d for d in timed if (d / "match.json").exists()), None)
        feat_timing = cond if fdir else "dev"
        fdir = fdir or B.feat_db(P, feat, build).parent
        pdir = next((B.R / P / f"people-{dev}{suffix}" for dev in ("gpu", "cpu")
                     if (B.R / P / f"people-{dev}{suffix}" / "people.json").exists()), post)
        row = {"people": wall(pdir / "people.json"), "extract": wall(fdir / "extract.json"), "match": wall(fdir / "match.json"),
               "map": wall(post.parent / "map.json"), "rest": wall(post.parent / "rest" / "rest.json"),
               "people2": next((w for w in (wall(post / f"people2_{dev}.json") for dev in ("gpu", "cpu")) if w is not None), None),
               **{s: wall(post / f"{s}.json") for s in steps[6:]}}
        photos = len(list((B.BENCH / "projects" / P / "photos").iterdir())) if (B.BENCH / "projects" / P / "photos").is_dir() else ""
        print("\t".join(str(x) for x in [P, photos, feat, run, post_name, feat_timing,
                                         *("" if row[s] is None else round(row[s], 1) for s in steps),
                                         round(sum(v for v in row.values() if v is not None), 1),
                                         ",".join(s for s in steps if row[s] is None)]))


# ---------------------------------------------------------------------------------------------- consensus

def consensus(pairs: list[tuple[str, str, dict]]) -> dict[str, dict]:
    """Each model against all the others of its boulder, from the pairs' agreement (a, b, stats.agreement's result):
    the median over the other models of the pair's median angle and of its median position error. A model far from
    every other shows by its own number, whichever model it is (the baseline's own second run included). For
    bench.py agree's consensus columns; consensus.py is the fuller judge (by viewing rays, not rotations)."""
    by = {}
    for a, b, ag in pairs:
        if ag and "deg_median" in ag:
            for m in (a, b):
                by.setdefault(m, []).append(ag)
    med = lambda v: round(sorted(v)[len(v) // 2] if len(v) % 2 else sum(sorted(v)[len(v) // 2 - 1:len(v) // 2 + 1]) / 2, 4)    # noqa: E731
    return {m: {"others": len(v), "deg": med([x["deg_median"] for x in v]), "pos": med([x["pos_median"] for x in v]),
                "deg_best": round(min(x["deg_median"] for x in v), 4)} for m, v in by.items()}


def command(a) -> None:
    """One of COMMANDS, from bench.py's parsed arguments."""
    if a.command == "sidemodels":
        sidemodels(Path(a.args[0]) if a.args else None)
    elif a.command == "together":
        if a.args[0] == "report":
            together_report()
        else:
            jobs = Path(a.args[1])
            together(int(a.args[0]), jobs if jobs.is_absolute() or jobs.exists() else HERE / jobs, "every" in a.args[2:])
    elif a.command == "texture":
        if a.args[0] == "report":
            texture_report()
        else:
            texture(a.args[0], a.args[1], a.args[2], a.feat, a.tools, a.cond, a.mvs)
    elif a.command == "people":
        rest = [x for x in a.args[1:] if x not in ("cpu", "gpu")]
        people(a.args[0], rest[0] if rest else None, "cpu" if "cpu" in a.args[1:] else "gpu", a.feat, a.tools, a.cond)
    elif a.command == "breakdown":
        breakdown()


if __name__ == "__main__":
    if sys.argv[1] == "poses":
        poses(Path(sys.argv[2]), Path(sys.argv[3]))
