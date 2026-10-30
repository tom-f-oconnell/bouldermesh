"""Photogrammetry pipeline: photos -> mesh.

Real path:  COLMAP (sparse SfM) -> OpenMVS (dense + mesh), via subprocess.
Fallback:   if either tool is missing, builds a labelled DEMO relief mesh from
            the first photo so the rest of the app can be exercised.

Every step logs to a callback so the UI can show progress.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]

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


def _run(cmd: list[str], cwd: Path, log: Log) -> None:
    log("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
    if proc.wait() != 0:
        raise RuntimeError(f"{cmd[0]} exited with {proc.returncode}")


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


def _upright(model: Path, images: Path, work: Path, log: Log) -> Path:
    """COLMAP models have no meaningful orientation, and tend to come out upside down in the
    viewer. Level the model from its cameras (most photos are taken upright), which puts gravity
    along +Y, then flip it so +Y is up as glTF expects. Returns the model to use from here on."""
    log("== COLMAP: level the model")
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


def reconstruct_colmap_openmvs(images: Path, work: Path, out_dir: Path, log: Log, use_gpu: bool,
                               internet: bool = False, min_registered: int = 6) -> Path:
    """internet=True tunes COLMAP for unordered photos from many cameras (per-image intrinsics,
    affine-shape SIFT, domain-size pooling, guided matching) - slower but far more tolerant."""
    work.mkdir(parents=True, exist_ok=True)
    db = work / "database.db"
    sparse = work / "sparse"
    dense = work / "dense"
    sparse.mkdir(exist_ok=True)
    gpu = "1" if use_gpu else "0"
    n_images = len([p for p in images.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")])

    log(f"== COLMAP: feature extraction ({'internet-collection' if internet else 'single-camera'} settings)")
    cmd = [COLMAP, "feature_extractor", "--database_path", str(db), "--image_path", str(images),
           "--SiftExtraction.use_gpu", gpu, "--ImageReader.camera_model", "SIMPLE_RADIAL"]
    if internet:
        cmd += ["--ImageReader.single_camera", "0", "--SiftExtraction.estimate_affine_shape", "1",
                "--SiftExtraction.domain_size_pooling", "1", "--SiftExtraction.max_image_size", "2400"]
    else:
        cmd += ["--ImageReader.single_camera", "1"]
    _run(cmd, work, log)

    log("== COLMAP: matching")
    cmd = [COLMAP, "exhaustive_matcher", "--database_path", str(db), "--SiftMatching.use_gpu", gpu]
    if internet:
        cmd += ["--SiftMatching.guided_matching", "1"]
    _run(cmd, work, log)

    log("== COLMAP: sparse reconstruction (mapper)")
    cmd = [COLMAP, "mapper", "--database_path", str(db), "--image_path", str(images), "--output_path", str(sparse)]
    if internet:
        cmd += ["--Mapper.ba_refine_principal_point", "1", "--Mapper.min_num_matches", "20"]
    _run(cmd, work, log)
    model, registered = _largest_model(sparse)
    log(f"registered {registered}/{n_images} images in the largest model")
    if model is None or registered < min_registered:
        raise RuntimeError(f"only {registered} image(s) could be registered (need >= {min_registered}); "
                           "the photo set doesn't overlap enough for photogrammetry")

    model = _upright(model, images, work, log)

    log("== COLMAP: undistort")
    _run([COLMAP, "image_undistorter", "--image_path", str(images), "--input_path", str(model),
          "--output_path", str(dense), "--output_type", "COLMAP"], work, log)

    log("== OpenMVS: import scene")
    _run([_mvs("InterfaceCOLMAP"), "-i", str(dense), "-o", "scene.mvs", "--image-folder", str(dense / "images"),
          "-w", str(work)], work, log)

    log("== OpenMVS: densify")
    _run([_mvs("DensifyPointCloud"), "scene.mvs", "-w", str(work), "--resolution-level", "2"], work, log)

    log("== OpenMVS: reconstruct mesh")
    _run([_mvs("ReconstructMesh"), "scene_dense.mvs", "-w", str(work)], work, log)
    mesh = work / "scene_dense_mesh.ply"

    tex = _mvs("TextureMesh")
    if tex:
        log("== OpenMVS: texture mesh")
        try:
            # ReconstructMesh only writes the .ply, so texture it against the dense scene. GLB loads
            # directly in the viewer (and OpenMVS 2.3's OBJ writer segfaults); its texture images are
            # separate files referenced by relative URI, so they're copied alongside it. Faces no photo
            # saw get --empty-color, by default a bright orange that reads as part of the scene.
            _run([tex, "scene_dense.mvs", "-m", "scene_dense_mesh.ply", "-o", "scene_dense_mesh_texture.mvs",
                  "-w", str(work), "--export-type", "glb", "--empty-color", str(0x808080)], work, log)
            glb = work / "scene_dense_mesh_texture.glb"
            if glb.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
                for f in work.glob("scene_dense_mesh_texture_*"):
                    shutil.copy(f, out_dir / f.name)
                return Path(shutil.copy(glb, out_dir / "mesh.glb"))
        except Exception as e:  # texturing is optional
            log(f"texturing failed ({e}); keeping untextured mesh")

    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "mesh.ply"
    shutil.copy(mesh, dst)
    return dst


# --------------------------------------------------------------------------
# Fallback: DEMO relief mesh from the first photo (so the app is testable
# without COLMAP/OpenMVS). Clearly labelled as a demo in the job log.
# --------------------------------------------------------------------------
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
                internet: bool = False) -> tuple[Path, bool]:
    """Returns (mesh_path, is_demo). internet=True for harvested web photo collections."""
    n = len([p for p in images.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    log(f"{n} photo(s) in {images}")
    if real_pipeline_ready():
        return reconstruct_colmap_openmvs(images, work, out_dir, log, use_gpu, internet=internet), False
    missing = [k for k, v in tools_available().items() if not v and k != "TextureMesh"]
    log(f"COLMAP/OpenMVS not found ({', '.join(missing)} missing) - falling back to DEMO mode. "
        "Install them (see Dockerfile) for real photogrammetry.")
    return demo_relief(images, out_dir, log), True
