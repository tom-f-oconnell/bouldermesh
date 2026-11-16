"""Photogrammetry pipeline: photos -> mesh.

Real path:  COLMAP (sparse SfM) -> OpenMVS (dense + mesh), via subprocess. The cameras are placed by COLMAP 4's global
            mapper where the image has it (_global_mapper(), MAPPER), else by COLMAP 3.9.1's own incremental mapper;
            where that can't place the photos, VGGT places them and COLMAP refines that (vggt_place.py). The photos
            the mapper left out are then tried again, one at a time, against the model it made (_place_rest()).
Fallback:   if either tool is missing, builds a labelled DEMO relief mesh from
            the first photo so the rest of the app can be exercised.

Every step logs to a callback so the UI can show progress.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Callable

import diagnostics
import meshopts
import occlusion
import orientation
import reconstruction
import vggt_place
from climbs.common import Cancelled, jobctl
from climbs.relevance import MIN_INLIERS
from diagnostics import gpu_free_mb
from progress import Tracker

Log = Callable[[str], None]

MAPPER_ATTEMPTS = 3
# placing the photos the mapper left out against its finished model: fewer of a photo's matches need to fit a camera
# position than the mapper's 30 and 25% (its defaults), as the model is then complete; a wrong one is rare, as each must
# still fit 15 points of the model it's seen to, within 12 px, and bundle adjustment then refines it with the rest
REST_ARGS = ["--Mapper.abs_pose_min_num_inliers", "15", "--Mapper.abs_pose_min_inlier_ratio", "0.15"]
GPU_MIN_FREE_MB = 4000  # matching 113 photos peaked at about 3 GB
# COLMAP's minimum of 15 raw matches, on the GPU. Its matcher finds about 4% fewer, which would drop
# borderline pairs before guided matching can rescue them; design.md has the measurements.
GPU_MATCH_ARGS = ["--TwoViewGeometry.min_num_inliers", "12"]
# GPU_SIFT (on unless GPU_SIFT=0): features for photos from the internet on the GPU. COLMAP has no GPU version of the
# affine shape and domain-size pooling those use, so the GPU's are plain SIFT, which changes which photos are placed:
# turned on Nov 15 without raising MESH_RULES (Tom), with the CPU's SIFT as its fallback (PLAIN_SIFT_LEAST below).
# With no GPU to be had, features are the CPU's as before.
GPU_SIFT = os.environ.get("GPU_SIFT", "1") == "1"
AFFINE_SIFT = ["--SiftExtraction.estimate_affine_shape", "1", "--SiftExtraction.domain_size_pooling", "1"]
MAX_TEXTURE = 4096      # larger atlases get split: an 8192 x 8192 one takes 270 MB in a browser, which can refuse it
MIN_FACES = 1000        # a mesh with fewer isn't one: harvests' empty meshes had 0-574, real ones 3,870 and up (design.md)
MIN_REGISTERED = 6      # photos COLMAP must place together for a mesh to be made from them
COLMAP = shutil.which("colmap")
# Which mapper places the cameras. "global": COLMAP 4.2.1's global mapper (the image's colmap4, Dockerfile), on a copy of
# the database, its models converted for 3.9.1, which does everything else; today's incremental mapper is the fallback
# (_global_mapper()). "incremental": COLMAP 3.9.1's mapper, as every mesh was placed before. In the mapper benchmark
# (design.md, "The global mapper") the global mapper placed the cameras 6-14 times faster with as many photos placed,
# once the cameras it leaves seeing next to none of its points are taken out (UNTIED) for the rest step to place.
MAPPER = os.environ.get("MAPPER", "global")
COLMAP4 = os.environ.get("COLMAP4_BIN") or shutil.which("colmap4")
UNTIED = 15             # a camera the global mapper leaves seeing fewer of its model's points than this is taken out
# The global mapper solves on the GPU (global positioning, and bundle adjustments of 50 photos or more: COLMAP's own
# threshold) when its COLMAP 4 has CUDA and this much GPU memory is free, as in every benchmark run behind the switch.
# It peaked at about 430 MiB on 31de (82 photos, the app's own run) and 580 MiB on Drifter (234, the benchmark's).
GLOBAL_GPU_MIN_FREE_MB = 1000
SOLVE = re.compile(r"BENCH_(BA|GP) images=(\d+)\b.*?device=(\w+)")     # each solve, as the image's patched COLMAP 4 logs it
MAPPER_FILE = "mapper.json"     # in work/, then beside the mesh: which mapper placed its cameras, and why (_global_mapper())
# OpenMVS binaries are usually installed under /usr/local/bin/OpenMVS
_MVS_DIRS = [os.environ.get("OPENMVS_BIN", ""), "/usr/local/bin/OpenMVS", "/usr/local/bin", "/usr/bin"]
# OpenMVS built with CUDA, where the image has one (Dockerfile, MVS=24): used for depth maps when a GPU has room. Its
# tools load the NVIDIA driver's library, so they don't start without a GPU: every other step uses the CPU build.
_MVS_CUDA_DIR = os.environ.get("OPENMVS_CUDA_BIN", "/usr/local/bin/OpenMVS-cuda")
GPU_MVS_MIN_FREE_MB = 2000  # its depth maps took 0.4-0.8 GB at resolution level 2 (experiments/mapper, 33-234 photos)


def _mvs(name: str) -> str | None:
    for d in _MVS_DIRS:
        if d and Path(d, name).exists():
            return str(Path(d, name))
    return shutil.which(name)


def _mvs_cuda(name: str) -> str | None:
    return str(Path(_MVS_CUDA_DIR, name)) if _MVS_CUDA_DIR and Path(_MVS_CUDA_DIR, name).exists() else None


def tools_available() -> dict:
    return {
        "colmap": COLMAP,
        "InterfaceCOLMAP": _mvs("InterfaceCOLMAP"),
        "DensifyPointCloud": _mvs("DensifyPointCloud"),
        "ReconstructMesh": _mvs("ReconstructMesh"),
        "TextureMesh": _mvs("TextureMesh"),
    }


def real_pipeline_ready() -> bool:
    t = tools_available()
    return all(t[k] for k in ("colmap", "InterfaceCOLMAP", "DensifyPointCloud", "ReconstructMesh"))


@functools.cache
def colmap_has_cuda() -> bool:
    if not COLMAP:
        return False
    try:
        return "with CUDA" in subprocess.run([COLMAP, "-h"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False


@functools.cache
def colmap_build(colmap: str | None = None) -> str | None:
    """Which COLMAP this is (or the one named, e.g. COLMAP4), as the first lines of its help say (version, commit,
    CUDA), for what a mesh keeps."""
    colmap = colmap or COLMAP
    if not colmap:
        return None
    try:
        lines = subprocess.run([colmap, "-h"], capture_output=True, text=True, timeout=30).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    return " ".join(l.strip() for l in lines[:3] if l.strip()) or None


def gpu_choice(allowed: bool = True) -> tuple[bool, str]:
    """Whether a COLMAP step should use the GPU right now, and why or why not."""
    if not allowed:
        return False, "USE_GPU=0, or these settings need the CPU"
    if not colmap_has_cuda():
        return False, "this COLMAP was built without CUDA"
    free = gpu_free_mb()
    if free is None:
        return False, "no GPU visible"
    if free < GPU_MIN_FREE_MB:
        return False, f"only {free} MB of GPU memory free, and it needs {GPU_MIN_FREE_MB}"
    return True, f"{free} MB of GPU memory free"


# OpenMVS 2.4 changed defaults that 2.3 had: it fuses depth maps another way (--fusion-filter 2, "dense-fuse", where 2.3
# has none, which is 1), wants fewer views to agree (2 against 3), filters no depth maps (0 against 7), and meshes
# points 1.5 px apart (against 2.5). Together they gave a fifth of the points and two thirds of the faces of 2.3's,
# and dense-fuse segfaulted 1 time in 15 (experiments/mapper, Nov 14). With 2.3's own values, 2.4 made 2.3's cloud
# (4.73M points on Drifter against 4.95M) and a mesh of 1.28M faces, in 530 s after the mapper against 2.3's 1,131
# on the CPU (depth maps on the GPU). So 2.4 is given 2.3's values, and the meshes look as they did, sooner. Except one:
# one geometric-consistency pass over the depth maps where both make two (Tom's yes, Nov 15): in the mapper benchmark
# ("as23g1") the dense step went from 161 to 101 s on Drifter and 66 to 52 s on 31de, with the surface as close to
# two passes' as two runs of two passes are to each other, and the points within 2%.
GEOMETRIC_ITERS = 1
MVS24_DENSIFY = ["--fusion-filter", "1", "--number-views-fuse", "3", "--postprocess-dmaps", "7",
                 "--geometric-iters", str(GEOMETRIC_ITERS)]
MVS24_MESH = ["--min-point-distance", "2.5"]


def texture_args(work: Path, images: Path, mask_args: list[str], log=None) -> list[str]:
    """TextureMesh's arguments after its input, output and work folder, as every mesh is textured (here and by
    textureagain.py)."""
    return ["--export-type", "glb", "--empty-color", str(0x808080), "--max-texture-size", str(MAX_TEXTURE), *mask_args,
            *texture_views(work, images, log)]


@functools.cache
def mvs_version() -> tuple[int, int] | None:
    """OpenMVS's version (major, minor), from DensifyPointCloud's own banner, or None if it can't be told."""
    tool = _mvs("DensifyPointCloud")
    if not tool:
        return None
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as td:     # it writes a log file where it runs
            out = subprocess.run([tool, "--help"], capture_output=True, text=True, timeout=60, cwd=td)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"OpenMVS \S* ?v(\d+)\.(\d+)", out.stdout + out.stderr)
    return (int(m[1]), int(m[2])) if m else None


def texture_views(work: Path, images: Path, log=None) -> list[str]:
    """TextureMesh's arguments for texturing from every still and every meshopts' texture_every-th frame of each video
    (VIDEO_FRAME's names; in their order, the first kept), as a list of them in work/texture-views.txt: none where every
    photo is used, no frame would be left out, there are too few to thin, or OpenMVS is before 2.4, which has no
    --views-file. Only frames are thinned: a video's next frame mostly shows what the last did, where a still is a
    viewpoint of its own, and leaving stills out left faces coloured from fewer, further photos (Stem Bem Boulder,
    Nov 16: blotches of another photo's light where every second photo was used)."""
    every = int(meshopts.mapper().get("texture_every") or 1)
    names = sorted(f.name for f in images.iterdir() if f.is_file() and not f.name.endswith(".mask.png")) if images.is_dir() else []
    if every < 2 or len(names) < TEXTURE_VIEWS_LEAST or not _mvs24(["x"]):
        return []
    videos: dict[str, list[str]] = {}
    for n in names:
        if VIDEO_FRAME.match(n):
            videos.setdefault(n.rsplit("_", 1)[0], []).append(n)
    left = {n for frames in videos.values() for n in frames if n not in frames[::every]}
    if not left:
        return []
    kept = [n for n in names if n not in left]
    (work / "texture-views.txt").write_text("\n".join(kept) + "\n")
    if log:
        frames = sum(map(len, videos.values()))
        log(f"texture: from {len(kept)} of {len(names)} photos (every still, and {frames - len(left)} of {frames} video frames: "
            f"every {'second' if every == 2 else f'{every}th'} of each video)")
    return ["--views-file", "texture-views.txt"]


VIDEO_FRAME = re.compile(r"(yt|vid)_[0-9a-f]+_\d+\.")   # a video's frames, as the harvest names them (yt_<tag>_NNN.jpg, vid_ for uploads)


TEXTURE_VIEWS_LEAST = 10    # photos left to texture from, at least, for only some to be used


def _mvs24(args: list[str]) -> list[str]:
    """These arguments where OpenMVS is 2.4 or later, to keep 2.3's defaults; none for 2.3, which rejects --fusion-filter."""
    v = mvs_version()
    return list(args) if v and v >= (2, 4) else []


def mvs_gpu_choice(allowed: bool = True) -> tuple[bool, str]:
    """Whether OpenMVS's depth maps should use the GPU right now (given a CUDA build of it), and why or why not."""
    if not allowed:
        return False, "USE_GPU=0"
    free = gpu_free_mb()
    if free is None:
        return False, "no GPU visible"
    if free < GPU_MVS_MIN_FREE_MB:
        return False, f"only {free} MB of GPU memory free, and it needs {GPU_MVS_MIN_FREE_MB}"
    return True, f"{free} MB of GPU memory free"


def _run(cmd: list[str], cwd: Path, log: Log, watch: Callable[[str], None] | None = None) -> None:
    log("$ " + " ".join(cmd))
    proc = jobctl.popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    assert proc.stdout
    running = diagnostics.ToolWatch(proc.pid)
    sender = None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(line)
                sender = diagnostics.read_sender(line) or sender
                if watch:
                    watch(line)
        code = proc.wait()
    finally:
        running.stop()
    jobctl.check()
    log(running.summary())
    if code != 0:
        failure = diagnostics.ToolFailed(cmd[0], code, sender, running.oom_kills)
        log(f"{failure}")
        for line in running.report(code, cwd):
            log(line)
        raise failure


FALLBACKS_FILE = "fallbacks.json"   # in work/, then beside the mesh: each time a step fell back from the GPU to the CPU


def _fell_back(work: Path, step: str, why: str, log: Log, **more) -> None:
    """Records that a step fell back to the CPU (or to the CPU's way of doing it), in work/FALLBACKS_FILE, which goes
    beside the mesh for the server to log (server.py, note_fallbacks()), and says so in the job's log as a line
    starting "FALLBACK:", which a machine's worker script also logs as its own."""
    f = work / FALLBACKS_FILE
    try:
        had = json.loads(f.read_text()) if f.exists() else []
    except ValueError:
        had = []
    f.write_text(json.dumps(had + [{"step": step, "why": why[:500], "t": round(time.time()), **more}], indent=1))
    log(f"FALLBACK: {step}: {why}")


def _gpu_or_cpu(cmd: list[str], flag: str, allowed: bool, cwd: Path, log: Log, watch: Callable[[str], None] | None = None,
                gpu_args: list[str] | None = None) -> str:
    """Run a COLMAP step on the GPU if it can be, otherwise on the CPU.

    The GPU is shared, and COLMAP aborts outright if it runs out of memory, which can happen at any
    point. Going again on the CPU only does what's left, as COLMAP skips work already in its database.

    Args:
        flag: the option that turns the GPU on or off.
        allowed: False to stay on the CPU, for settings the GPU can't do or USE_GPU=0.
        gpu_args: further options for a run on the GPU.

    Returns:
        What it ran on: "gpu", "cpu", or "gpu+cpu" if the GPU failed partway.
    """
    gpu, why = gpu_choice(allowed)
    log(f"{cmd[1]}: using the {'GPU' if gpu else 'CPU'} ({why})")
    if gpu:
        try:
            _run(cmd + (gpu_args or []) + [flag, "1"], cwd, log, watch)
            return "gpu"
        except RuntimeError as e:
            log(f"{cmd[1]}: GPU run failed ({e}), carrying on with the CPU")
            _fell_back(cwd, cmd[1], f"the GPU run failed ({e}): carried on with the CPU", log)
    _run(cmd + [flag, "0"], cwd, log, watch)
    return "gpu+cpu" if gpu else "cpu"


def steps() -> list[tuple[str, str, float]]:
    """The progress steps of reconstruct(), weighted by typical CPU share of the run."""
    if not real_pipeline_ready():
        return [("demo", "Build demo mesh", 1)]
    people = occlusion.available()
    out = ([("people", "Find people in photos", 1)] if people else []) + [
        ("features", "Find features", 3), ("matching", "Match photos", 15), ("mapper", "Place cameras", 40),
        ("rest", "Place the rest", 2), ("level", "Level the model", 1), ("undistort", "Undistort photos", 1)]
    out += ([("people-undistorted", "Mask people for dense steps", 1)] if people else []) + [
        ("import", "Import scene", 1), ("dense", "Dense reconstruction", 25), ("mesh", "Build mesh", 8)]
    return out + ([("texture", "Texture mesh", 6)] if _mvs("TextureMesh") else [])


def _phases(tracker: Tracker, key: str, *phases: tuple[str, float, float]) -> Callable[[str], None]:
    """A log watcher: each phase is (regex, fraction of the step at its start, and at its end).

    The regex's groups give progress within the phase: "12 (37.5%" style with one group
    (a percentage), "[3/72]" style with two (a count), or none for a phase that just ended.
    """
    compiled = [(re.compile(rx), lo, hi) for rx, lo, hi in phases]

    def watch(line: str) -> None:
        for rx, lo, hi in compiled:
            m = rx.search(line)
            if m:
                g = m.groups()
                within = int(g[0]) / max(1, int(g[1])) if len(g) == 2 else float(g[0]) / 100 if g else 1.0
                tracker.update(key, lo + (hi - lo) * within)
                return
    return watch


def _matching(tracker: Tracker) -> Callable[[str], None]:
    rx = re.compile(r"Matching block \[(\d+)/(\d+), (\d+)/(\d+)\]")

    def watch(line: str) -> None:
        m = rx.search(line)
        if m:
            a, b, c, d = (int(x) for x in m.groups())
            tracker.update("matching", ((a - 1) * d + c - 1) / max(1, b * d), f"block {(a - 1) * d + c} of {b * d}")
    return watch


def _mapper(tracker: Tracker, n_images: int) -> Callable[[str], None]:
    rx = re.compile(r"Registering image #\d+ \((\d+)\)")

    def watch(line: str) -> None:
        m = rx.search(line)
        if m:
            k = int(m[1])
            tracker.update("mapper", k / max(1, n_images), f"{k} of {n_images} photos in this model")
    return watch


def _global(tracker: Tracker) -> Callable[[str], None]:
    """The global mapper's phases, in each connected part of the photos it reconstructs: the bar moves at the start of
    each, by about its share of the time in the benchmark's runs."""
    part = re.compile(r"Reconstructing component (\d+) / (\d+)")
    phases = [("rotation averaging", 0.05, "turning the cameras"), ("track establishment", 0.15, "linking the matches"),
              ("global positioning", 0.25, "placing the cameras"), ("iterative bundle adjustment", 0.5, "refining"),
              ("iterative retriangulation", 0.8, "refining the points")]
    state = {"k": 1, "n": 1}

    def watch(line: str) -> None:
        if m := part.search(line):
            state.update(k=int(m[1]), n=int(m[2]))
            return
        for text, at, what in phases:
            if f"Running {text}" in line:
                k, n = state["k"], state["n"]
                tracker.update("mapper", (k - 1 + at) / n, what + (f" (part {k} of {n})" if n > 1 else ""))
                return
    return watch


def _geometric(tracker: Tracker, passes: int = 2) -> Callable[[str], None]:
    """DensifyPointCloud's depth phases; the geometric-consistency one runs `passes` times over (OpenMVS's own 2, or
    GEOMETRIC_ITERS where it's given)."""
    rx = re.compile(r"(Geometric-consistent estimated|Estimated|Fused|Dense fused) depth-maps \d+ \(([\d.]+)%")   # "Dense fused": OpenMVS 2.4
    state = {"passes": 0, "last": 0.0}

    def watch(line: str) -> None:
        m = rx.search(line)
        if not m:
            return
        pct = float(m[2]) / 100
        if m[1] == "Estimated":
            tracker.update("dense", 0.45 * pct, "estimating depth maps")
        elif m[1] in ("Fused", "Dense fused"):
            tracker.update("dense", 0.85 + 0.15 * pct, "fusing depth maps")
        else:
            if pct < state["last"]:
                state["passes"] += 1
            state["last"] = pct
            tracker.update("dense", 0.45 + 0.40 * min(1.0, (state["passes"] + pct) / passes), "checking depth consistency")
    return watch


# --------------------------------------------------------------------------
# Real pipeline
# --------------------------------------------------------------------------
def _largest_model(sparse: Path) -> tuple[Path | None, int]:
    """COLMAP may output several disconnected models (sparse/0, sparse/1, ...).
    Pick the one with most registered images; count via images.bin header or images.txt."""
    best, best_n = None, 0
    for m in sorted(sparse.iterdir()) if sparse.exists() else []:
        n = 0
        if (m / "images.bin").exists():
            with open(m / "images.bin", "rb") as f:
                n = struct.unpack("<Q", f.read(8))[0]
        elif (m / "images.txt").exists():
            n = sum(1 for l in open(m / "images.txt") if l.strip() and not l.startswith("#")) // 2
        if n > best_n:
            best, best_n = m, n
    return best, best_n


# Beside a mesh: each model COLMAP made of its photos, largest first, and the photos each placed; with the database, which
# of them the mesh is made from (main), and each photo that one doesn't place with the placed photo it shares most verified
# matches with (left: {photo: {with, inliers}}, inliers 0 if it shares none).
#   models.json   {models: [{name, photos}], main, left}
MODELS_FILE = "models.json"
EXTRACT_FILE = "extract.json"    # in work/: the feature extraction's options and device, for what the mesh keeps (reconstruction.py)
MODEL_LEAST = 4                 # models placing fewer photos than this aren't worth recording
TIE_LEAST = MIN_INLIERS         # verified matches two photos share for them to be tied: as the off-topic filter takes a match
WATERMARK = 7                   # COLMAP's two-view configuration for matches that are an overlay in the same place in both
                                # (a logo or caption on a video's frames), not of the scene
PAIR_BASE = 2147483647          # COLMAP's pair id: PAIR_BASE * the lower image id + the higher


def check_model(model: Path, db: Path, out_dir: Path, log: Log) -> dict | None:
    """The model's check of itself (modelcheck.py), of the levelled model the mesh is made from, kept beside the mesh
    as check.json and said in the log. It decides nothing about the mesh and never fails one: whatever goes wrong is
    logged and nothing is written."""
    try:
        import modelcheck
        c = modelcheck.check(model, db)
        modelcheck.save(out_dir, c)
        log(modelcheck.line(c))
        return c
    except Exception as e:  # noqa: BLE001: a check must not cost a mesh
        log(f"model check: not made ({type(e).__name__}: {e})")
        return None


def best_ties(db: Path, placed: set[str]) -> dict[str, dict]:
    """Each photo in a COLMAP database that isn't among those placed, with the placed photo it shares most verified
    matches with (its two-view geometry's inliers, unless they're a watermark): {photo: {"with": that photo or None,
    "inliers": n}}."""
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = dict(con.execute("SELECT image_id, name FROM images"))
        best = {n: {"with": None, "inliers": 0} for n in names.values() if n not in placed}
        for pair, n, config in con.execute("SELECT pair_id, rows, config FROM two_view_geometries WHERE rows > 0"):
            if config == WATERMARK:
                continue
            a, b = names.get(pair // PAIR_BASE), names.get(pair % PAIR_BASE)
            for x, y in ((a, b), (b, a)):
                if x in best and y in placed and n > best[x]["inliers"]:
                    best[x] = {"with": y, "inliers": n}
        return best
    finally:
        con.close()


def write_models(sparse: Path, dest: Path, db: Path | None = None, main: Path | None = None) -> dict:
    """Record every model the mapper made (not only the largest, which the mesh is made from), as {name, photos},
    largest first, in dest (the work folder, whence it goes beside the mesh): photos that land in a model together
    overlap, which says which climbs' photos can be pooled, and which made a separate piece. With the database and the
    model the mesh is made from (main), also how each photo that model leaves out is tied to those it places
    (best_ties()): what says whether another model is of the boulder's surroundings or of somewhere else, and whether a
    video's frame shows the climb's rock. Returns what's recorded."""
    models = []
    for m in sorted(sparse.iterdir()) if sparse.exists() else []:
        if not (m / "images.bin").exists():
            continue
        try:
            names = sorted(c["name"] for c in orientation.read_cameras(m))
        except (OSError, struct.error, UnicodeDecodeError):
            continue
        if len(names) >= MODEL_LEAST or m == main:
            models.append({"name": m.name, "photos": names})
    models.sort(key=lambda x: -len(x["photos"]))
    record: dict = {"models": models}
    placed = next((set(x["photos"]) for x in models if main is not None and x["name"] == main.name), None)
    if placed is not None and db is not None and db.exists():
        try:
            record.update(main=main.name, left=best_ties(db, placed))
        except Exception:   # only costs telling the other models and video frames apart
            pass
    dest.mkdir(parents=True, exist_ok=True)
    (dest / MODELS_FILE).write_text(json.dumps(record))
    return record


def load_models(mesh_dir: Path) -> dict | None:
    """A mesh's models.json (write_models()), or None if it has none."""
    try:
        return json.loads((mesh_dir / MODELS_FILE).read_text())
    except (OSError, ValueError):
        return None


def _upright(model: Path, images: Path, work: Path, log: Log, tracker: Tracker) -> Path:
    """COLMAP models have no meaningful orientation, and tend to come out upside down in the
    viewer. Level the model from its cameras (most photos are taken upright), which puts gravity
    along +Y, then flip it so +Y is up as glTF expects. Returns the model to use from here on."""
    log("== COLMAP: level the model")
    tracker.update("level")
    aligned = work / "aligned"
    aligned.mkdir(exist_ok=True)
    flip = work / "flip.txt"
    flip.write_text("1 0 1 0 0 0 0 0\n")  # scale, quaternion w x y z (180 degrees about X), translation
    try:
        _run([COLMAP, "model_orientation_aligner", "--image_path", str(images), "--input_path", str(model),
              "--output_path", str(aligned), "--method", "IMAGE-ORIENTATION"], work, log)
        _run([COLMAP, "model_transformer", "--input_path", str(aligned), "--output_path", str(aligned),
              "--transform_path", str(flip)], work, log)
        return aligned
    except RuntimeError as e:  # only costs the orientation
        log(f"levelling failed ({e}); keeping COLMAP's orientation")
        return model


def _place_with_vggt(db: Path, images: Path, sparse: Path, work: Path, log: Log, tracker: Tracker, registered: int) -> None:
    """When the mapper placed too few photos: VGGT's placements, refined by COLMAP, as another model in sparse/ (vggt),
    for the largest to be chosen from as before. Failing only costs the time."""
    why = vggt_place.unavailable()
    if why:
        log(f"VGGT could try placing them instead, but not here: {why}")
        return
    log(f"== VGGT: placing the photos, as COLMAP's mapper placed only {registered}")
    tracker.update("mapper", 0, "placing them with VGGT")
    try:
        n = vggt_place.place(COLMAP, db, images, sparse / "vggt", work / "vggt", log, lambda cmd: _run(cmd, work, log))
        log(f"VGGT and COLMAP placed {n} photos")
    except Cancelled:
        raise
    except Exception as e:
        log(f"placing them with VGGT failed: {e}")


def _place_rest(db: Path, model: Path, work: Path, log: Log, tracker: Tracker, placed: int, n_images: int) -> tuple[Path, int]:
    """The photos the mapper left out, tried one at a time against the model it finished (COLMAP's image_registrator,
    with REST_ARGS), then everything refined together (bundle adjustment). The mapper grows its model from one pair
    of photos, and gives up on a photo that doesn't fit what it has at the time; against the whole model, more do.
    The model it makes takes the mapper's place, which is kept in work/. Returns the model to use and how many photos
    it places: the mapper's, if none more could be placed or it failed, which only costs the time."""
    log(f"== COLMAP: placing the rest ({n_images - placed} photos the mapper left out) against its model")
    tracker.update("rest")
    tmp = work / "rest"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        _run([COLMAP, "image_registrator", "--database_path", str(db), "--input_path", str(model), "--output_path", str(tmp),
              *REST_ARGS], work, log)
        n = vggt_place.count(tmp, "images")
        if n <= placed:
            log("none of them could be placed")
            return model, placed
        _run([COLMAP, "bundle_adjuster", "--input_path", str(tmp), "--output_path", str(tmp)], work, log)
    except RuntimeError as e:
        log(f"placing the rest failed ({e}); keeping the mapper's model")
        return model, placed
    kept = work / f"mapper_{model.name}"
    shutil.rmtree(kept, ignore_errors=True)
    model.rename(kept)
    tmp.rename(model)
    log(f"placed {n - placed} more of them")
    return model, n


def global_args(min_registered: int, internet: bool, gpu: bool = False) -> list[str]:
    """The global mapper's options: the app's own for the incremental mapper, in its spelling (models down to what a
    mesh can be made from; for photos from the internet each lens's principal point refined and pairs of 20 matches
    or more), its solves on the GPU or the CPU, from seed 0."""
    args = ["--log_target", "stderr", "--default_random_seed", "0", "--GlobalMapper.random_seed", "0",
            "--GlobalMapper.min_model_size", str(min_registered)]
    if internet:
        args += ["--GlobalMapper.ba_refine_principal_point", "1", "--GlobalMapper.min_num_matches", "20"]
    on = "1" if gpu else "0"
    return args + ["--GlobalMapper.gp_use_gpu", on, "--GlobalMapper.ba_ceres_use_gpu", on]


@functools.cache
def colmap4_has_cuda() -> bool:
    """Whether the image's COLMAP 4 was built with CUDA (the Dockerfile's colmap4-cuda stage)."""
    build = colmap_build(COLMAP4) if COLMAP4 else None
    return bool(build and "with CUDA" in build)


def global_gpu_choice(allowed: bool = True) -> tuple[bool, str]:
    """Whether the global mapper should solve on the GPU right now, and why or why not."""
    if not allowed:
        return False, "USE_GPU=0"
    if not colmap4_has_cuda():
        return False, "this COLMAP 4 was built without CUDA"
    free = gpu_free_mb()
    if free is None:
        return False, "no GPU visible"
    if free < GLOBAL_GPU_MIN_FREE_MB:
        return False, f"only {free} MB of GPU memory free, and it needs {GLOBAL_GPU_MIN_FREE_MB}"
    return True, f"{free} MB of GPU memory free"


def _solves(lines: list[str]) -> dict:
    """What the global mapper's solves ran on, from its log: {cuda: n, cpu: n, cpu_large: n}, cpu_large counting
    those of 50 photos or more, which COLMAP puts on the GPU when it's asked to."""
    out = {"cuda": 0, "cpu": 0, "cpu_large": 0}
    for line in lines:
        if m := SOLVE.search(line):
            device = "cuda" if m[3] == "cuda" else "cpu"
            out[device] += 1
            out["cpu_large"] += device == "cpu" and int(m[2]) >= 50
    return out


def _set_aside(sparse: Path, work: Path, prefix: str) -> None:
    """What the global mapper left in sparse/, moved out of the incremental mapper's way (kept in work/ to look at)."""
    if sparse.exists() and any(sparse.iterdir()):
        sparse.rename(work / f"{prefix}_sparse{len(list(work.glob(f'{prefix}_sparse*')))}")
    sparse.mkdir(exist_ok=True)


def _global_mapper(db: Path, images: Path, sparse: Path, work: Path, log: Log, tracker: Tracker, n_images: int,
                   min_registered: int, internet: bool, use_gpu: bool = False) -> tuple[dict | None, str]:
    """The cameras placed by COLMAP 4.2.1's global mapper, where MAPPER is "global" and the image has it (COLMAP4).

    On a copy of the database (COLMAP 4 changes a 3.9.1 database into its own layout as it opens it), into
    work/sparse_global; each model it makes is converted to text by COLMAP 4 and back to binary by 3.9.1, into sparse/
    as 3.9.1's mapper would have left it, so that every later step sees what it always has. In its largest model, the
    cameras that see fewer than UNTIED of its points are then taken out: the global mapper keeps cameras whose every
    observation it filtered out, and those were its wrong ones (12-20 degrees off the others' in the benchmark). The
    rest step then tries them like any other photo the mapper left out. It solves on the GPU where it can
    (global_gpu_choice()), and if that fails, starts again on the CPU. Its log says what each solve ran on, and a
    large one left on the CPU when the GPU was chosen is said loudly: that's a build or a COLMAP that ignores it.

    Returns:
        What it did, for mapper.json ({mapper: "global", colmap4, args, device, solves, untied, placed, seconds}), or None and why
        not, with sparse/ left empty for the incremental mapper: MAPPER isn't "global", COLMAP 4 isn't here, the global
        mapper failed, its largest model places fewer than min_registered photos, or its models couldn't be converted.
    """
    if MAPPER != "global":
        return None, f"MAPPER is {MAPPER!r}"
    if not COLMAP4:
        return None, "this image has no COLMAP 4 (colmap4) for the global mapper"
    log("== COLMAP 4: sparse reconstruction (global mapper)")
    tracker.update("mapper", 0, "the global mapper")
    out, copy = work / "sparse_global", work / "global.db"
    done = work / "global.json"

    def attempt(gpu: bool) -> tuple[list[str], list[str]]:
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir()
        for f in work.glob(copy.name + "*"):
            f.unlink()
        shutil.copyfile(db, copy)
        args, lines, progress = global_args(min_registered, internet, gpu), [], _global(tracker)

        def watch(line: str) -> None:
            progress(line)
            if "BENCH_" in line:
                lines.append(line)
        _run([COLMAP4, "global_mapper", "--database_path", str(copy), "--image_path", str(images), "--output_path", str(out),
              *args], work, log, watch)
        return args, lines

    if not _finished(work, "global"):
        start = time.time()
        gpu, why = global_gpu_choice(use_gpu)
        log(f"global_mapper: solving on the {'GPU' if gpu else 'CPU'} ({why})")
        device = "gpu" if gpu else "cpu"
        try:
            try:
                args, lines = attempt(gpu)
            except RuntimeError as e:
                if not gpu:
                    raise
                log(f"global_mapper: GPU run failed ({e}), starting it again on the CPU")
                _fell_back(work, "global_mapper", f"the GPU run failed ({e}): started again on the CPU", log)
                device = "gpu+cpu"
                args, lines = attempt(False)
        except (RuntimeError, OSError) as e:
            return None, f"the global mapper failed ({e})"
        solves = _solves(lines)
        log(f"global_mapper: {solves['cuda']} solve(s) on the GPU and {solves['cpu']} on the CPU")
        if device == "gpu" and solves["cpu_large"]:
            log(f"WARNING: global_mapper: the GPU was chosen, but {solves['cpu_large']} solve(s) of 50 photos or more ran on "
                f"the CPU: is this COLMAP 4's Ceres built with CUDA and cuDSS?")
        done.write_text(json.dumps({"seconds": round(time.time() - start, 1), "args": args, "device": device,
                                    "device_why": why, "solves": solves}))
        _finish(work, "global")
    ran = json.loads(done.read_text()) if done.exists() else {}
    seconds, args = ran.get("seconds"), ran.get("args") or global_args(min_registered, internet)
    models = sorted((m for m in out.iterdir() if (m / "images.bin").exists()), key=lambda m: m.name) if out.exists() else []
    if not models:
        return None, "the global mapper made no model"
    try:
        for m in models:
            text = work / "global_text" / m.name
            shutil.rmtree(text, ignore_errors=True)
            text.mkdir(parents=True)
            _run([COLMAP4, "model_converter", "--log_target", "stderr", "--input_path", str(m), "--output_path", str(text),
                  "--output_type", "TXT"], work, log)
            (sparse / m.name).mkdir()
            _run([COLMAP, "model_converter", "--input_path", str(text), "--output_path", str(sparse / m.name),
                  "--output_type", "BIN"], work, log)
    except (RuntimeError, OSError) as e:
        _set_aside(sparse, work, "global")
        return None, f"its models couldn't be converted for COLMAP 3.9.1 ({e})"
    model, placed = _largest_model(sparse)
    untied = []
    if model is not None:
        untied = sorted(c["name"] for c in orientation.read_cameras(model) if c["seen"] < UNTIED)
    if untied and placed - len(untied) < min_registered:
        _set_aside(sparse, work, "global")
        return None, (f"its largest model places only {placed - len(untied)} of {n_images} photos that see {UNTIED} or more "
                      f"of its points (need {min_registered})")
    if untied:
        names, tied = work / "untied.txt", work / "tied"
        names.write_text("".join(f"{n}\n" for n in untied))
        shutil.rmtree(tied, ignore_errors=True)
        tied.mkdir()
        try:
            _run([COLMAP, "image_deleter", "--input_path", str(model), "--output_path", str(tied), "--image_names_path", str(names)],
                 work, log)
        except RuntimeError as e:
            _set_aside(sparse, work, "global")
            return None, f"its cameras seeing next to none of its points couldn't be taken out ({e})"
        kept = work / f"global_untied_{model.name}"
        shutil.rmtree(kept, ignore_errors=True)
        model.rename(kept)
        tied.rename(model)
        log(f"global mapper: took out {len(untied)} of its {placed} cameras, which see fewer than {UNTIED} of its points, "
            f"for the rest step to try again: {', '.join(untied[:12])}{' ...' if len(untied) > 12 else ''}")
        model, placed = _largest_model(sparse)
    if placed < min_registered:
        _set_aside(sparse, work, "global")
        return None, f"its largest model places only {placed} of {n_images} photos (need {min_registered})"
    log(f"global mapper: {placed} of {n_images} photos placed in its largest model, of {len(models)} model(s)")
    return {"mapper": "global", "why": "MAPPER is 'global'", "colmap4": colmap_build(COLMAP4), "args": args,
            "device": ran.get("device"), "device_why": ran.get("device_why"), "solves": ran.get("solves"),
            "untied": untied, "untied_least": UNTIED, "models": len(models), "placed": placed, "seconds": seconds}, ""


def _finished(work: Path, step: str) -> bool:
    return (work / "finished" / step).exists()


def _finish(work: Path, step: str) -> None:
    """Record that a step is complete, so that a job interrupted later doesn't do it again."""
    (work / "finished").mkdir(exist_ok=True)
    (work / "finished" / step).touch()


def _level_from_readings(model: Path, images: Path, work: Path, log: Log) -> dict | None:
    """Check a levelled model against the iPhone gravity readings of its placed photos, and if there
    are any it can believe, level it from them instead. Only costs that if it fails.

    Returns:
        The report for the mesh, or None if it couldn't be worked out.
    """
    if model.name != "aligned" or not orientation.available():
        return None
    try:
        report = orientation.check(model, images)
        phone = report["iphone"]
        if phone["status"] == "used":
            q = orientation.level_quaternion(phone["down"])
            transform = work / "gravity.txt"
            transform.write_text("1 " + " ".join(f"{x:.12f}" for x in q) + " 0 0 0\n")
            _run([COLMAP, "model_transformer", "--input_path", str(model), "--output_path", str(model),
                  "--transform_path", str(transform)], work, log)
            orientation.after_levelling(report, q)
            log(f"down: from the iPhone gravity readings of {len(phone['readings'])} placed photo(s); "
                f"the estimate from how photos were framed was {phone['off_estimate_deg']} degrees away")
        elif phone["status"] == "rejected":
            log(f"down: estimated from how photos were framed; the iPhone readings are {phone['off_estimate_deg']} degrees away, too far to believe")
        else:
            log(f"down: estimated from how photos were framed; none of the {report['placed']} placed photos has an iPhone reading")
        return report
    except Exception as e:
        log(f"checking which way is down failed ({e}); keeping the estimate from how photos were framed")
        return None


def _incremental_mapper(db: Path, images: Path, sparse: Path, work: Path, log: Log, tracker: Tracker, n_images: int,
                        min_registered: int, internet: bool, why: str) -> dict:
    """The cameras placed by COLMAP 3.9.1's own incremental mapper, with its seeds and retries, and VGGT where it places
    too few: how every mesh was made before the global mapper. Returns what it did, for mapper.json."""
    log("== COLMAP: sparse reconstruction (mapper)")
    start = time.time()
    # models after the first as small as a mesh can be made from (COLMAP's own least is 10; the first is kept whatever
    # its size): what the largest leaves out may be another side or rock (write_models())
    cmd = [COLMAP, "mapper", "--database_path", str(db), "--image_path", str(images), "--output_path", str(sparse),
           "--Mapper.min_model_size", str(min_registered)]
    if internet:
        cmd += ["--Mapper.ba_refine_principal_point", "1", "--Mapper.min_num_matches", "20"]
    # COLMAP 3.9's mapper can abort on one failing model ("NumRegImages() >= 2"; 2 of 25 runs when
    # tried), or throw away a whole model it had grown, leaving too little (design.md), or place many fewer
    # photos of the same set from one run to the next (meshopts.py). It works from the database, so another seed
    # doesn't redo the matching, and the models an earlier attempt finished stay on disk as candidates.
    opts = meshopts.mapper()
    below, more = float(opts["retry_below"] or 0), int(opts["more"] or 0)
    attempt = failed = fewer = 0
    while True:
        try:
            _run(cmd + ["--random_seed", str(attempt)], work, log, _mapper(tracker, n_images))
            ran = True
        except RuntimeError as e:
            ran, problem = False, str(e)
        best = _largest_model(sparse)[1]
        if best >= min_registered and (ran or fewer):     # enough, from this seed or one before tried for more
            if not (ran and below > 0 and best < below * n_images and fewer < more):
                break
            fewer += 1
            log(f"mapper placed {best} of {n_images} photos, under {below:.0%} of them: trying another seed ({fewer} of "
                f"{more}), keeping the model that places most")
        else:
            failed += 1
            log(f"mapper failed ({problem if not ran else f'only {best} of {n_images} photos placed'}), attempt {failed} of "
                f"{MAPPER_ATTEMPTS}")
            if failed >= MAPPER_ATTEMPTS:
                break
        for m in list(sparse.iterdir()):
            if not m.name.startswith("aborted"):
                m.rename(sparse / f"aborted{attempt}-{m.name}")
        attempt += 1
    registered = _largest_model(sparse)[1]
    if registered < min_registered:
        _place_with_vggt(db, images, sparse, work, log, tracker, registered)
    return {"mapper": "incremental", "why": why, "colmap": colmap_build(), "seeds": attempt + 1,
            "vggt": (sparse / "vggt").is_dir(), "seconds": round(time.time() - start, 1)}


# GPU_SIFT's plain SIFT is checked once its model is placed: if the model fails its own check (modelcheck.py) or places
# under PLAIN_SIFT_LEAST of the photos that share verified matches with another, the photos are placed again from the
# CPU's SIFT with affine shapes and domain-size pooling (Tom, Nov 15, on atlas's measurements: GPU SIFT's surfaces were
# within two runs' of each other on both boulders and it placed 17 more on Drifter, but its cameras differ from the
# CPU's by more than two runs' do, and on 9f3b it placed 133 against 141). What the GPU's run made is kept aside in
# work/PLAIN_SIFT_ASIDE, and why in work/PLAIN_SIFT_FILE and mapper.json ("plain_sift_rejected").
PLAIN_SIFT_LEAST = 0.7
PLAIN_SIFT_FILE = "plain-sift.json"
PLAIN_SIFT_ASIDE = "plain-sift"
# what placing the photos leaves in work/, set aside to place them again, and the steps that made it
PLACING_FILES = ("database.db*", "sparse", "sparse_global", "global.db*", "global.json", "global_text", "untied.txt", "tied",
                 "vggt", "rest", MAPPER_FILE, EXTRACT_FILE, MODELS_FILE)
PLACING_STEPS = ("features", "matching", "global", "mapper", "rest")


def matched_photos(db: Path, least: int = 20) -> int:
    """How many photos in a COLMAP database share at least `least` verified matches with another (modelcheck's
    MIN_INLIERS, the pairs the mapper goes by): those a model could place."""
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ids = set()
        for pid, in con.execute("SELECT pair_id FROM two_view_geometries WHERE rows >= ?", (least,)):
            ids.update(divmod(pid, 2147483647))
        return len(ids)
    finally:
        con.close()


def placing_summary(model: Path | None, registered: int, db: Path, log: Log) -> dict:
    """What a placing came to, to judge it and to compare two: {placed, matched (photos with verified matches, or
    None), ok (its model's own check, or None if it couldn't be made), why (the check's words)}."""
    out = {"placed": registered if model is not None else 0, "matched": None, "ok": None, "why": None}
    try:
        out["matched"] = matched_photos(db)
    except Exception as e:  # noqa: BLE001: can't tell, so not on this ground
        log(f"GPU SIFT: couldn't count the photos with matches ({e})")
    if model is None:
        out["ok"], out["why"] = False, "it made no model"
        return out
    try:
        import modelcheck
        c = modelcheck.check(model, db)
        out["ok"], out["why"] = bool(c.get("ok")), c.get("why")
    except Exception as e:  # noqa: BLE001
        log(f"GPU SIFT: the model couldn't be checked ({type(e).__name__}: {e})")
    return out


def _plain_sift_short(summary: dict) -> str | None:
    """Why GPU SIFT's placing (placing_summary()) falls short, for the CPU's to be tried instead, or None if it doesn't."""
    placed, matched = summary["placed"], summary["matched"]
    if summary["ok"] is False and placed == 0:
        return summary["why"]
    if matched and placed < PLAIN_SIFT_LEAST * matched:
        return f"its model places {placed} of the {matched} photos with matches, under {PLAIN_SIFT_LEAST:.0%}"
    if summary["ok"] is False:
        return f"its model fails its own check ({summary['why']})"
    return None


def _said(summary: dict) -> str:
    return (f"placed {summary['placed']}" + (f" of {summary['matched']} with matches" if summary["matched"] else "") +
            {True: ", check passed", False: ", check failed", None: ", not checked"}[summary["ok"]])


def plain_sift_better(gpu: dict, cpu: dict) -> bool:
    """Whether the CPU's placing did better than the GPU's it replaced: its check passed where the GPU's failed, or it
    placed more photos and its check didn't fail."""
    if cpu["ok"] and gpu["ok"] is False:
        return True
    return cpu["placed"] > gpu["placed"] and cpu["ok"] is not False


def _set_aside_placing(work: Path) -> None:
    """Moves what placing the photos made into work/PLAIN_SIFT_ASIDE, and forgets those steps were done."""
    aside = work / PLAIN_SIFT_ASIDE
    shutil.rmtree(aside, ignore_errors=True)
    aside.mkdir()
    for pattern in PLACING_FILES:
        for f in work.glob(pattern):
            f.rename(aside / f.name)
    for step in PLACING_STEPS:
        (work / "finished" / step).unlink(missing_ok=True)
    (work / "sparse").mkdir()


def reconstruct_colmap_openmvs(images: Path, work: Path, out_dir: Path, log: Log, use_gpu: bool,
                               internet: bool = False, min_registered: int = MIN_REGISTERED, tracker: Tracker | None = None) -> Path:
    """internet=True tunes COLMAP for unordered photos from many cameras (per-image intrinsics,
    affine-shape SIFT, domain-size pooling, guided matching) - slower but far more tolerant.

    Steps that finish are recorded in work/, so called again on the same work/ after an interruption,
    this carries on from there. Within a step, matching keeps its pairs and densifying its depth maps;
    the others start that step again. To start from scratch, delete work/.
    """
    tracker = tracker or Tracker([])
    work.mkdir(parents=True, exist_ok=True)
    db = work / "database.db"
    sparse = work / "sparse"
    dense = work / "dense"
    sparse.mkdir(exist_ok=True)
    n_images = len([p for p in images.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")])

    # Kept next to the photos rather than in work/, which is deleted, as where the people are is also
    # what estimating their size needs. Only photos without an up-to-date mask are looked at again.
    masks = images.parent / "masks"
    if (work / "masks").is_dir() and not masks.exists():    # where they used to be kept
        (work / "masks").rename(masks)
    if not _finished(work, "people") and occlusion.available():
        log("== Finding people to ignore")
        try:
            occlusion.write_masks(images, masks, lambda f: f"{f.name}.png", log, tracker.callback("people"), gpu=use_gpu)
            _finish(work, "people")
        except Exception as e:  # e.g. the model can't be downloaded: only costs the masking
            log(f"occlusion: failed ({e}); not masking people")
    if not _finished(work, "people"):
        masks = None

    rejected = work / PLAIN_SIFT_FILE

    def place() -> tuple[Path | None, int]:
        """Features, matching, the mapper and the rest step, each where not finished: the largest model and how many it places."""
        cmd = [COLMAP, "feature_extractor", "--database_path", str(db), "--image_path", str(images),
               "--ImageReader.camera_model", "SIMPLE_RADIAL"]
        if internet:
            cmd += ["--ImageReader.single_camera", "0", "--SiftExtraction.max_image_size", "2400"]
        else:
            cmd += ["--ImageReader.single_camera", "1"]
        if masks:
            cmd += ["--ImageReader.mask_path", str(masks)]
        if not _finished(work, "features"):
            log(f"== COLMAP: feature extraction ({'internet-collection' if internet else 'single-camera'} settings)")
            tracker.update("features", 0, f"0 of {n_images} photos")
            watch = _phases(tracker, "features", (r"Processed file \[(\d+)/(\d+)\]", 0, 1))
            device = None
            if internet and GPU_SIFT and use_gpu and not rejected.exists():     # plain SIFT, all on the GPU or not at all: the two find different features
                gpu, why = gpu_choice(True)
                log(f"feature_extractor: GPU_SIFT is set: {'plain SIFT on the GPU' if gpu else 'not this time'} ({why})")
                if gpu:
                    try:
                        _run(cmd + ["--SiftExtraction.use_gpu", "1"], work, log, watch)
                        device = "gpu"
                    except RuntimeError as e:
                        log(f"feature_extractor: GPU run failed ({e}): starting again on the CPU, with its usual features")
                        _fell_back(work, "feature_extractor", f"GPU SIFT failed ({e}): started again with the CPU's SIFT", log)
                        for f in work.glob(db.name + "*"):
                            f.unlink()
            if device is None:
                # COLMAP has no GPU version of the affine shape and domain-size pooling of the internet settings
                cmd += AFFINE_SIFT if internet else []
                device = _gpu_or_cpu(cmd, "--SiftExtraction.use_gpu", use_gpu and not internet, work, log, watch)
            (work / EXTRACT_FILE).write_text(json.dumps({"args": reconstruction.extract_args(cmd), "device": device,
                                                          **({"plain_sift": "rejected"} if rejected.exists() else {})}))
            _finish(work, "features")

        if not _finished(work, "matching"):
            log("== COLMAP: matching")
            tracker.update("matching")
            # smaller blocks than the default 50 only make for finer progress: all pairs get matched either way
            cmd = [COLMAP, "exhaustive_matcher", "--database_path", str(db), "--ExhaustiveMatching.block_size", "20"]
            if internet:
                cmd += ["--SiftMatching.guided_matching", "1"]
            _gpu_or_cpu(cmd, "--SiftMatching.use_gpu", use_gpu, work, log, _matching(tracker), GPU_MATCH_ARGS)
            _finish(work, "matching")

        if not _finished(work, "mapper"):
            tracker.update("mapper")
            if any(sparse.iterdir()):   # what an interrupted run left may be half written, and it'll be made again
                sparse.rename(work / f"interrupted_sparse{len(list(work.glob('interrupted_sparse*')))}")
                sparse.mkdir()
            how, why = _global_mapper(db, images, sparse, work, log, tracker, n_images, min_registered, internet, use_gpu)
            if how is None:
                if MAPPER == "global":
                    log(f"placing the cameras with COLMAP 3.9.1's incremental mapper instead: {why}")
                how = _incremental_mapper(db, images, sparse, work, log, tracker, n_images, min_registered, internet, why)
            if rejected.exists():
                how["plain_sift_rejected"] = json.loads(rejected.read_text())
            (work / MAPPER_FILE).write_text(json.dumps(how, indent=1))
        model, registered = _largest_model(sparse)
        if model is not None and min_registered <= registered < n_images and not _finished(work, "rest"):
            model, registered = _place_rest(db, model, work, log, tracker, registered, n_images)
            _finish(work, "rest")
        return model, registered

    model, registered = place()
    extract = json.loads((work / EXTRACT_FILE).read_text()) if (work / EXTRACT_FILE).exists() else {}
    if internet and extract.get("device") == "gpu" and not rejected.exists() and not _finished(work, "sift-judged"):
        gpu = placing_summary(model, registered, db, log)
        why = _plain_sift_short(gpu)
        log(f"GPU SIFT: {_said(gpu)}" + (f": {why}" if why else ""))
        if why:     # solved again from the CPU's SIFT, with affine shapes and domain-size pooling, as before GPU_SIFT
            log("GPU SIFT: placing the photos again from the CPU's SIFT, with affine shapes and domain-size pooling")
            rejected.write_text(json.dumps({"why": why, "gpu": gpu, "aside": PLAIN_SIFT_ASIDE}))
            _set_aside_placing(work)
    if rejected.exists() and not _finished(work, "sift-judged"):
        model, registered = place()
        record = json.loads(rejected.read_text())
        cpu = placing_summary(model, registered, db, log)
        record.update(cpu=cpu, better=plain_sift_better(record["gpu"], cpu))
        rejected.write_text(json.dumps(record))
        how = json.loads((work / MAPPER_FILE).read_text()) if (work / MAPPER_FILE).exists() else {}
        (work / MAPPER_FILE).write_text(json.dumps({**how, "plain_sift_rejected": record}, indent=1))
        _fell_back(work, "plain_sift", f"{record['why']}: placed again from the CPU's SIFT. The GPU's {_said(record['gpu'])}; "
                   f"the CPU's {_said(cpu)}: " + ("the CPU's did better" if record["better"] else "the CPU's did no better"),
                   log, gpu=record["gpu"], cpu=cpu, better=record["better"])
    _finish(work, "sift-judged")
    log(f"registered {registered}/{n_images} images in the largest model")
    try:
        rec = write_models(sparse, work, db, model)
        others = [m for m in rec["models"] if model is None or m["name"] != model.name]
        tied = lambda m: sum((rec.get("left") or {}).get(p, {}).get("inliers", 0) >= TIE_LEAST for p in m["photos"])  # noqa: E731
        if others:
            log(f"other models: {', '.join(str(len(m['photos'])) for m in others[:8])} photo(s)" +
                (f"; of their photos the largest doesn't place, tied to one it does ({TIE_LEAST}+ verified matches): "
                 f"{', '.join(str(tied(m)) for m in others[:8])}" if "left" in rec else ""))
    except OSError as e:
        log(f"couldn't record the mapper's models: {e}")
    if model is None or registered < min_registered:
        raise RuntimeError(f"only {registered} image(s) could be registered (need >= {min_registered}); "
                           "the photos may not overlap enough for photogrammetry")
    _finish(work, "mapper")

    if _finished(work, "level"):
        level = json.loads((work / "level.json").read_text())
        model, report = Path(level["model"]), level["report"]
    else:
        model = _upright(model, images, work, log, tracker)
        report = _level_from_readings(model, images, work, log)
        (work / "level.json").write_text(json.dumps({"model": str(model), "report": report}))
        _finish(work, "level")

    def keep_orientation() -> None:
        if report:
            orientation.save(out_dir, report, model)
        if _largest_model(sparse)[0] == sparse / "vggt" and (work / "vggt" / "placed.json").exists():   # the mesh's model came from VGGT's
            shutil.copyfile(work / "vggt" / "placed.json", out_dir / "placement.json")
        if (work / MODELS_FILE).exists():
            shutil.copyfile(work / MODELS_FILE, out_dir / MODELS_FILE)
        if (work / FALLBACKS_FILE).exists():
            shutil.copyfile(work / FALLBACKS_FILE, out_dir / FALLBACKS_FILE)
        how = json.loads((work / MAPPER_FILE).read_text()) if (work / MAPPER_FILE).exists() else None
        if how:
            (out_dir / MAPPER_FILE).write_text(json.dumps(how, indent=1))
        extract = json.loads((work / EXTRACT_FILE).read_text()) if (work / EXTRACT_FILE).exists() else None
        reconstruction.keep(model, db, out_dir, log, colmap_build(), extract, how)
        check_model(model, db, out_dir, log)

    if not _finished(work, "undistort"):
        log("== COLMAP: undistort")
        tracker.update("undistort")
        _run([COLMAP, "image_undistorter", "--image_path", str(images), "--input_path", str(model),
              "--output_path", str(dense), "--output_type", "COLMAP"], work, log,
             _phases(tracker, "undistort", (r"Undistorting image \[(\d+)/(\d+)\]", 0, 1)))
        _finish(work, "undistort")

    mask_args = ["--ignore-mask-label", "0"] if _finished(work, "people-undistorted") else []
    if masks and not mask_args:
        # COLMAP can't undistort the masks along with the photos, so each is moved as its photo was (warp_masks());
        # people are only looked for again in a photo that couldn't be. OpenMVS looks for <image name>.mask.png
        # next to each image
        try:
            name_for = lambda f: f"{f.stem}.mask.png"  # noqa: E731
            try:
                left = occlusion.warp_masks(masks, orientation.read_cameras(model), orientation.read_cameras(dense / "sparse"),
                                            dense / "images", name_for, log, tracker.callback("people-undistorted"))
                if left:
                    log(f"occlusion: {len(left)} undistorted photo(s) had no mask to move: looking for people in them")
            except Exception as e:      # then as it was done before: people looked for again in every undistorted photo
                log(f"occlusion: the masks couldn't be moved to the undistorted photos ({type(e).__name__}: {e}); looking for people in those")
                left = None
            if left is None or left:
                occlusion.write_masks(dense / "images", dense / "images", name_for, log,
                                      tracker.callback("people-undistorted"), gpu=use_gpu)
            mask_args = ["--ignore-mask-label", "0"]
            _finish(work, "people-undistorted")
        except Exception as e:
            log(f"occlusion: failed ({e}); not masking people in the dense steps")

    if not _finished(work, "import"):
        log("== OpenMVS: import scene")
        tracker.update("import")
        _run([_mvs("InterfaceCOLMAP"), "-i", str(dense), "-o", "scene.mvs", "--image-folder", str(dense / "images"),
              "-w", str(work)], work, log)
        _finish(work, "import")

    if not _finished(work, "dense"):
        log("== OpenMVS: densify")
        tracker.update("dense")
        densify = ["scene.mvs", "-w", str(work), "--resolution-level", "2", *mask_args,
                   *(["--mask-path", str(dense / "images")] if mask_args else []), *_mvs24(MVS24_DENSIFY)]
        cuda, on_gpu = _mvs_cuda("DensifyPointCloud"), False
        passes = GEOMETRIC_ITERS if "--geometric-iters" in densify else 2
        if cuda:
            gpu, why = mvs_gpu_choice(use_gpu)
            log(f"DensifyPointCloud: using the {'GPU' if gpu else 'CPU'} ({why})")
            if gpu:
                try:
                    _run([cuda, *densify, "--cuda-device", "-1"], work, log, _geometric(tracker, passes))
                    on_gpu = True
                except RuntimeError as e:   # the CPU build carries on from the depth maps made so far
                    log(f"DensifyPointCloud: GPU run failed ({e}), carrying on with the CPU")
                    _fell_back(work, "DensifyPointCloud", f"the GPU run failed ({e}): carried on with the CPU build", log)
        if not on_gpu:
            _run([_mvs("DensifyPointCloud"), *densify], work, log, _geometric(tracker, passes))
        _finish(work, "dense")

    if not _finished(work, "mesh"):
        log("== OpenMVS: reconstruct mesh")
        tracker.update("mesh")
        _run([_mvs("ReconstructMesh"), "scene_dense.mvs", "-w", str(work), *_mvs24(MVS24_MESH)], work, log,
             _phases(tracker, "mesh", (r"Points inserted \d+ \(([\d.]+)%", 0, 0.5), (r"Points weighted \d+ \(([\d.]+)%", 0.5, 0.9)))
        _finish(work, "mesh")
    mesh = work / "scene_dense_mesh.ply"
    faces = ply_faces(mesh)
    if faces is not None and faces < MIN_FACES:
        raise RuntimeError(f"the mesh has only {faces} face(s) (need >= {MIN_FACES}): too few points were found in the photos "
                           "to build a surface")

    tex = _mvs("TextureMesh")
    if tex:
        log("== OpenMVS: texture mesh")
        tracker.update("texture")
        try:
            # ReconstructMesh only writes the .ply, so texture it against the dense scene. GLB loads
            # directly in the viewer (and OpenMVS 2.3's OBJ writer segfaults); its texture images are
            # separate files referenced by relative URI, so they're copied alongside it. Faces no photo
            # saw get --empty-color, by default a bright orange that reads as part of the scene.
            _run([tex, "scene_dense.mvs", "-m", "scene_dense_mesh.ply", "-o", "scene_dense_mesh_texture.mvs",
                  "-w", str(work), *texture_args(work, dense / "images", mask_args, log)], work, log,
                 _phases(tracker, "texture", (r"Initialized views \d+ \(([\d.]+)%", 0, 0.3),
                         (r"Assigning the best view .*completed", 0.3, 0.6), (r"Generating texture atlas .*completed", 0.6, 0.95)))
            glb = work / "scene_dense_mesh_texture.glb"
            if glb.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
                keep_orientation()
                for f in work.glob("scene_dense_mesh_texture_*"):
                    shutil.copyfile(f, out_dir / f.name)
                return Path(shutil.copyfile(glb, out_dir / "mesh.glb"))
        except Exception as e:  # texturing is optional
            log(f"texturing failed ({e}); keeping untextured mesh")

    out_dir.mkdir(parents=True, exist_ok=True)
    keep_orientation()
    dst = out_dir / "mesh.ply"
    shutil.copyfile(mesh, dst)    # not its mode: OpenMVS writes PLYs readable only by whoever ran it
    return dst


# --------------------------------------------------------------------------
# Fallback: DEMO relief mesh from the first photo (so the app is testable
# without COLMAP/OpenMVS). Clearly labelled as a demo in the job log.
# --------------------------------------------------------------------------
def ply_faces(path: Path) -> int | None:
    """How many faces a PLY file says it has, from its header, or None if that can't be read."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    m = re.search(rb"^element face (\d+)", head.split(b"end_header", 1)[0], re.M)
    return int(m[1]) if m and head.startswith(b"ply") else None


def write_ply(path: Path, verts: list[tuple[float, float, float]], colors: list[tuple[int, int, int]],
              faces: list[tuple[int, int, int]]) -> None:
    with open(path, "wb") as f:
        f.write(("ply\nformat binary_little_endian 1.0\n"
                 f"element vertex {len(verts)}\n"
                 "property float x\nproperty float y\nproperty float z\n"
                 "property uchar red\nproperty uchar green\nproperty uchar blue\n"
                 f"element face {len(faces)}\nproperty list uchar int vertex_indices\nend_header\n").encode())
        for (x, y, z), (r, g, b) in zip(verts, colors):
            f.write(struct.pack("<fffBBB", x, y, z, r, g, b))
        for a, b, c in faces:
            f.write(struct.pack("<Biii", 3, a, b, c))


def demo_relief(images: Path, out_dir: Path, log: Log, grid: int = 96) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "mesh.ply"
    photos = sorted(p for p in images.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    verts: list = []
    colors: list = []
    try:
        from PIL import Image  # optional
        if not photos:
            raise RuntimeError("no photos")
        img = Image.open(photos[0]).convert("RGB")
        img.thumbnail((grid, grid))
        w, h = img.size
        gray = img.convert("L")
        log(f"DEMO MODE: building a relief mesh from {photos[0].name} ({w}x{h} grid)")
        for j in range(h):
            for i in range(w):
                z = gray.getpixel((i, j)) / 255.0 * 0.25
                verts.append(((i / (w - 1) - 0.5) * 2, (0.5 - j / (h - 1)) * 2 * h / w, z))
                colors.append(img.getpixel((i, j)))
    except Exception as e:
        log(f"DEMO MODE: {e}; building a procedural surface instead")
        import math
        w = h = grid
        for j in range(h):
            for i in range(w):
                x = (i / (w - 1) - 0.5) * 2
                y = (j / (h - 1) - 0.5) * 2
                z = 0.25 * math.sin(3 * x) * math.cos(3 * y)
                verts.append((x, y, z))
                c = int(128 + 120 * z / 0.25)
                colors.append((c, 180, 255 - c))
    faces = []
    for j in range(h - 1):
        for i in range(w - 1):
            a = j * w + i
            b, c, d = a + 1, a + w, a + w + 1
            faces += [(a, c, b), (b, c, d)]
    write_ply(dst, verts, colors, faces)
    log(f"wrote {dst.name}: {len(verts)} vertices, {len(faces)} faces")
    return dst


# --------------------------------------------------------------------------
def reconstruct(images: Path, work: Path, out_dir: Path, log: Log, use_gpu: bool = False,
                internet: bool = False, tracker: Tracker | None = None) -> tuple[Path, bool]:
    """Returns (mesh_path, is_demo). internet=True for harvested web photo collections."""
    n = len([p for p in images.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    log(f"{n} photo(s) in {images}")
    if real_pipeline_ready():
        return reconstruct_colmap_openmvs(images, work, out_dir, log, use_gpu, internet=internet, tracker=tracker), False
    missing = [k for k, v in tools_available().items() if not v and k != "TextureMesh"]
    log(f"COLMAP/OpenMVS not found ({', '.join(missing)} missing) - falling back to DEMO mode. "
        "Install them (see Dockerfile) for real photogrammetry.")
    if tracker:
        tracker.update("demo", 0.1)
    return demo_relief(images, out_dir, log), True
