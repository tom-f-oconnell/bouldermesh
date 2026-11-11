"""Photogrammetry pipeline: photos -> mesh.

Real path:  COLMAP (sparse SfM) -> OpenMVS (dense + mesh), via subprocess. Where COLMAP's mapper can't place the
            photos, VGGT places them and COLMAP refines that (vggt_place.py).
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
from pathlib import Path
from typing import Callable

import diagnostics
import meshopts
import occlusion
import orientation
import vggt_place
from climbs.common import Cancelled, jobctl
from diagnostics import gpu_free_mb
from progress import Tracker

Log = Callable[[str], None]

MAPPER_ATTEMPTS = 3
GPU_MIN_FREE_MB = 4000  # matching 113 photos peaked at about 3 GB
# COLMAP's minimum of 15 raw matches, on the GPU. Its matcher finds about 4% fewer, which would drop
# borderline pairs before guided matching can rescue them; design.md has the measurements.
GPU_MATCH_ARGS = ["--TwoViewGeometry.min_num_inliers", "12"]
MAX_TEXTURE = 4096      # larger atlases get split: an 8192 x 8192 one takes 270 MB in a browser, which can refuse it
MIN_FACES = 1000        # a mesh with fewer isn't one: harvests' empty meshes had 0-574, real ones 3,870 and up (design.md)
COLMAP = shutil.which("colmap")
# OpenMVS binaries are usually installed under /usr/local/bin/OpenMVS
_MVS_DIRS = [os.environ.get("OPENMVS_BIN", ""), "/usr/local/bin/OpenMVS", "/usr/local/bin", "/usr/bin"]


def _mvs(name: str) -> str | None:
    for d in _MVS_DIRS:
        if d and Path(d, name).exists():
            return str(Path(d, name))
    return shutil.which(name)


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


def _gpu_or_cpu(cmd: list[str], flag: str, allowed: bool, cwd: Path, log: Log, watch: Callable[[str], None] | None = None,
                gpu_args: list[str] | None = None) -> None:
    """Run a COLMAP step on the GPU if it can be, otherwise on the CPU.

    The GPU is shared, and COLMAP aborts outright if it runs out of memory, which can happen at any
    point. Going again on the CPU only does what's left, as COLMAP skips work already in its database.

    Args:
        flag: the option that turns the GPU on or off.
        allowed: False to stay on the CPU, for settings the GPU can't do or USE_GPU=0.
        gpu_args: further options for a run on the GPU.
    """
    gpu, why = gpu_choice(allowed)
    log(f"{cmd[1]}: using the {'GPU' if gpu else 'CPU'} ({why})")
    if gpu:
        try:
            _run(cmd + (gpu_args or []) + [flag, "1"], cwd, log, watch)
            return
        except RuntimeError as e:
            log(f"{cmd[1]}: GPU run failed ({e}), carrying on with the CPU")
    _run(cmd + [flag, "0"], cwd, log, watch)


def steps() -> list[tuple[str, str, float]]:
    """The progress steps of reconstruct(), weighted by typical CPU share of the run."""
    if not real_pipeline_ready():
        return [("demo", "Build demo mesh", 1)]
    people = occlusion.available()
    out = ([("people", "Find people in photos", 1)] if people else []) + [
        ("features", "Find features", 3), ("matching", "Match photos", 15), ("mapper", "Place cameras", 40),
        ("level", "Level the model", 1), ("undistort", "Undistort photos", 1)]
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


def _geometric(tracker: Tracker) -> Callable[[str], None]:
    """DensifyPointCloud's depth phases; the geometric-consistency one runs twice over."""
    rx = re.compile(r"(Geometric-consistent estimated|Estimated|Fused) depth-maps \d+ \(([\d.]+)%")
    state = {"passes": 0, "last": 0.0}

    def watch(line: str) -> None:
        m = rx.search(line)
        if not m:
            return
        pct = float(m[2]) / 100
        if m[1] == "Estimated":
            tracker.update("dense", 0.45 * pct, "estimating depth maps")
        elif m[1] == "Fused":
            tracker.update("dense", 0.85 + 0.15 * pct, "fusing depth maps")
        else:
            if pct < state["last"]:
                state["passes"] += 1
            state["last"] = pct
            tracker.update("dense", 0.45 + 0.40 * min(1.0, (state["passes"] + pct) / 2), "checking depth consistency")
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


def reconstruct_colmap_openmvs(images: Path, work: Path, out_dir: Path, log: Log, use_gpu: bool,
                               internet: bool = False, min_registered: int = 6, tracker: Tracker | None = None) -> Path:
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
            occlusion.write_masks(images, masks, lambda f: f"{f.name}.png", log, tracker.callback("people"))
            _finish(work, "people")
        except Exception as e:  # e.g. the model can't be downloaded: only costs the masking
            log(f"occlusion: failed ({e}); not masking people")
    if not _finished(work, "people"):
        masks = None

    cmd = [COLMAP, "feature_extractor", "--database_path", str(db), "--image_path", str(images),
           "--ImageReader.camera_model", "SIMPLE_RADIAL"]
    if internet:
        cmd += ["--ImageReader.single_camera", "0", "--SiftExtraction.estimate_affine_shape", "1",
                "--SiftExtraction.domain_size_pooling", "1", "--SiftExtraction.max_image_size", "2400"]
    else:
        cmd += ["--ImageReader.single_camera", "1"]
    if masks:
        cmd += ["--ImageReader.mask_path", str(masks)]
    if not _finished(work, "features"):
        log(f"== COLMAP: feature extraction ({'internet-collection' if internet else 'single-camera'} settings)")
        tracker.update("features", 0, f"0 of {n_images} photos")
        # COLMAP has no GPU version of the affine shape and domain-size pooling of the internet settings
        _gpu_or_cpu(cmd, "--SiftExtraction.use_gpu", use_gpu and not internet, work, log,
                    _phases(tracker, "features", (r"Processed file \[(\d+)/(\d+)\]", 0, 1)))
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
        log("== COLMAP: sparse reconstruction (mapper)")
        tracker.update("mapper")
        if any(sparse.iterdir()):   # what an interrupted run left may be half written, and it'll be made again
            sparse.rename(work / f"interrupted_sparse{len(list(work.glob('interrupted_sparse*')))}")
            sparse.mkdir()
        cmd = [COLMAP, "mapper", "--database_path", str(db), "--image_path", str(images), "--output_path", str(sparse)]
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
    model, registered = _largest_model(sparse)
    log(f"registered {registered}/{n_images} images in the largest model")
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

    if not _finished(work, "undistort"):
        log("== COLMAP: undistort")
        tracker.update("undistort")
        _run([COLMAP, "image_undistorter", "--image_path", str(images), "--input_path", str(model),
              "--output_path", str(dense), "--output_type", "COLMAP"], work, log,
             _phases(tracker, "undistort", (r"Undistorting image \[(\d+)/(\d+)\]", 0, 1)))
        _finish(work, "undistort")

    mask_args = ["--ignore-mask-label", "0"] if _finished(work, "people-undistorted") else []
    if masks and not mask_args:
        # found again rather than undistorted along with the photos, which COLMAP can't do; OpenMVS
        # looks for <image name>.mask.png next to each image
        try:
            occlusion.write_masks(dense / "images", dense / "images", lambda f: f"{f.stem}.mask.png", log,
                                  tracker.callback("people-undistorted"))
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
        _run([_mvs("DensifyPointCloud"), "scene.mvs", "-w", str(work), "--resolution-level", "2", *mask_args,
              *(["--mask-path", str(dense / "images")] if mask_args else [])], work, log, _geometric(tracker))
        _finish(work, "dense")

    if not _finished(work, "mesh"):
        log("== OpenMVS: reconstruct mesh")
        tracker.update("mesh")
        _run([_mvs("ReconstructMesh"), "scene_dense.mvs", "-w", str(work)], work, log,
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
                  "-w", str(work), "--export-type", "glb", "--empty-color", str(0x808080),
                  "--max-texture-size", str(MAX_TEXTURE), *mask_args], work, log,
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
