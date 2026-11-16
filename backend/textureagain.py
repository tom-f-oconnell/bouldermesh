"""Texture a mesh again, keeping its shape: from the cameras its reconstruction kept (reconstruction.py), its project's
photos and people masks, and the mesh itself, without placing the photos, densifying or meshing again. About a minute
or two, against ten or more for a whole rebuild; for when how meshes are textured changes, or texturing went wrong.

Each step is the pipeline's own (pipeline.py), from the photos' undistortion on:

  1. the kept model (cameras placed and levelled, as the mesh was made from) out of reconstruction.tar.gz;
  2. COLMAP's image_undistorter on the project's photos, and the people masks moved as the photos were;
  3. OpenMVS's InterfaceCOLMAP, for a scene of those cameras;
  4. the mesh's faces, read back from its GLB in the same order (its vertices welded again where TextureMesh split them
     along texture seams), as a PLY;
  5. OpenMVS's TextureMesh, as the pipeline runs it.

The new GLB must have the same faces, in the same order and places, as the old one: the mesh's cleanup (its hidden
pieces, meshclean.py) and anything else that names faces by their index stays right. Where it doesn't (an atlas split
into several textures orders faces by texture), nothing is changed and NotSame says so.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

import meshclean
import occlusion
import orientation
import pipeline
import reconstruction

Log = Callable[[str], None]
GLB = "scene_dense_mesh_texture.glb"


class NotSame(RuntimeError):
    """The mesh textured again doesn't have the old one's faces in the same order and places."""


def faces_of(glb: Path):
    """A GLB's triangles as (welded vertex positions, faces into them), faces in the file's order, vertices split along
    texture seams joined again."""
    import numpy as np
    j, bufs = meshclean._read_glb(glb)
    P, I, _ = meshclean._geometry(j, bufs)
    P = np.asarray(P, dtype=np.float32)
    uniq, weld = np.unique(P, axis=0, return_inverse=True)
    return uniq, weld.ravel()[I]


def write_ply(path: Path, verts, faces) -> None:
    """A binary PLY of these vertices and triangles, as ReconstructMesh writes one."""
    import numpy as np
    with open(path, "wb") as f:
        f.write(("ply\nformat binary_little_endian 1.0\n"
                 f"element vertex {len(verts)}\nproperty float x\nproperty float y\nproperty float z\n"
                 f"element face {len(faces)}\nproperty list uchar int vertex_indices\nend_header\n").encode())
        f.write(np.asarray(verts, dtype="<f4").tobytes())
        rec = np.zeros(len(faces), dtype=[("n", "u1"), ("v", "<i4", (3,))])
        rec["n"], rec["v"] = 3, faces
        f.write(rec.tobytes())


def same_faces(old: Path, new: Path, tol: float = 1e-4) -> str | None:
    """Why new's faces aren't old's, in the same order and places; None if they are."""
    import numpy as np
    Po, Fo = faces_of(old)
    Pn, Fn = faces_of(new)
    if len(Fo) != len(Fn):
        return f"{len(Fn)} faces, where the mesh had {len(Fo)}"
    off = np.abs(Po[Fo] - Pn[Fn]).max() if len(Fo) else 0.0
    scale = float(np.abs(Po).max()) or 1.0
    return None if off <= tol * scale else f"its faces aren't in the same order or places (up to {off:.3g} apart)"


def texture_again(mesh_dir: Path, photos: Path, masks: Path | None, work: Path, log: Log, use_gpu: bool = False) -> Path:
    """Texture mesh_dir's mesh.glb again in work/ (made empty first), from its kept reconstruction, the project's photos
    and its people masks (<photo name>.png, or None); the new GLB, its textures beside it. Raises RuntimeError if what's
    needed isn't there or a step fails, NotSame if the faces didn't stay as they were."""
    old = mesh_dir / "mesh.glb"
    if not old.is_file():
        raise RuntimeError("only a mesh made as a GLB can be textured again")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    if reconstruction.unpack(mesh_dir, work / "kept") is None:
        raise RuntimeError("its reconstruction wasn't kept, so its cameras aren't known")
    model, dense = work / "kept" / "model", work / "dense"
    wanted = {c["name"] for c in orientation.read_cameras(model)}
    missing = sorted(n for n in wanted if not (photos / n).is_file())
    if missing:
        raise RuntimeError(f"{len(missing)} of its {len(wanted)} placed photos aren't in its project "
                           f"({', '.join(missing[:3])}{'...' if len(missing) > 3 else ''})")

    log(f"== texture again: {len(wanted)} placed photos")
    pipeline._run([pipeline.COLMAP, "image_undistorter", "--image_path", str(photos), "--input_path", str(model),
                   "--output_path", str(dense), "--output_type", "COLMAP"], work, log)
    mask_args = []
    if masks and masks.is_dir():
        name_for = lambda f: f"{f.stem}.mask.png"  # noqa: E731
        try:
            left = occlusion.warp_masks(masks, orientation.read_cameras(model), orientation.read_cameras(dense / "sparse"),
                                        dense / "images", name_for, log)
            if left:
                occlusion.write_masks(dense / "images", dense / "images", name_for, log, gpu=use_gpu)
            mask_args = ["--ignore-mask-label", "0"]
        except Exception as e:      # noqa: BLE001 - as the pipeline: textured without them
            log(f"occlusion: failed ({type(e).__name__}: {e}); texturing without the people masks")
    pipeline._run([pipeline._mvs("InterfaceCOLMAP"), "-i", str(dense), "-o", "scene.mvs", "--image-folder", str(dense / "images"),
                   "-w", str(work)], work, log)

    verts, faces = faces_of(old)
    write_ply(work / "mesh.ply", verts, faces)
    log(f"the mesh: {len(faces)} faces, {len(verts)} vertices")
    pipeline._run([pipeline._mvs("TextureMesh"), "scene.mvs", "-m", "mesh.ply", "-o", "scene_dense_mesh_texture.mvs",
                   "-w", str(work), *pipeline.texture_args(work, dense / "images", mask_args, log)], work, log)
    new = work / GLB
    if not new.is_file():
        raise RuntimeError("TextureMesh made no GLB")
    why = same_faces(old, new)
    if why:
        raise NotSame(f"not used: textured again, {why}")
    return new


def install(mesh_dir: Path, new: Path, log: Log) -> None:
    """Put a mesh textured again (texture_again()) in place of the mesh's own: mesh.glb and its textures, the old ones
    kept in texture-before/ (the last ones only)."""
    before = mesh_dir / "texture-before"
    if before.exists():
        shutil.rmtree(before)
    before.mkdir()
    for f in [mesh_dir / "mesh.glb", *mesh_dir.glob("scene_dense_mesh_texture_*")]:
        if f.is_file():
            shutil.copy2(f, before / f.name)
    for f in [*mesh_dir.glob("scene_dense_mesh_texture_*"), *(mesh_dir / "smaller").glob("scene_dense_mesh_texture_*")]:
        f.unlink()      # the shrunk copies too (shrink_texture() makes only those missing)
    for f in new.parent.glob("scene_dense_mesh_texture_*"):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg"):
            shutil.copyfile(f, mesh_dir / f.name)
    tmp = mesh_dir / "mesh.glb.new"
    shutil.copyfile(new, tmp)
    tmp.replace(mesh_dir / "mesh.glb")
    (before / "info.json").write_text(json.dumps({"why": "textured again (textureagain.py)"}))
    log("textured again: the mesh's texture replaced, the old one kept in texture-before/")

