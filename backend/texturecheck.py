"""Whether a mesh's texture came out patched with black, as OpenMVS 2.4.0's TextureMesh painted a sixth to a half of an
atlas's charts before its sampler was patched (Dockerfile; upstream issue #1251), so that it can be textured again
(textureagain.py).

A texture's atlas is its charts (the photos' colour) on a background of the empty colour (pipeline.py's grey, 0x808080).
What's measured is the share of the charts' pixels that are black: a texture made since the patch has some (a chart's
padding, faces no photo saw well), 3-7% in those looked at; a black-patched one has 25-50%.

    share(image)        black pixels' share of an atlas image's charts
    check(mesh_dir)     {share: the worst of its textures, textures: how many, black: whether it's black-patched},
                        kept beside the mesh as texture-check.json and measured again only when its textures change
"""
from __future__ import annotations

import json
from pathlib import Path

FILE = "texture-check.json"
VERSION = 1
SIDE = 1024         # measured on a copy at most this many pixels across (nearest neighbour: a pixel is black or not)
BLACK = 6           # a pixel no channel of which is above this is black
EMPTY = 128         # the background's grey, give or take 1 (TextureMesh's --empty-color, pipeline.py)
BLACK_LEAST = 0.2   # a texture whose charts are this much black or more is black-patched: textured since the patch,
                    # 3-7%; before it, 24-47% (Nov 16, the live site's meshes)


def textures(mesh_dir: Path) -> list[Path]:
    """A mesh's own texture images (not its old ones, kept in texture-before/ when it was textured again)."""
    return sorted(p for p in Path(mesh_dir).glob("scene_dense_mesh_texture_*") if p.suffix.lower() in (".png", ".jpg", ".jpeg"))


def share(image: Path) -> float:
    """The share of an atlas image's charts (what isn't the empty grey) that's black."""
    import numpy as np
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(image) as im:
        im = im.convert("RGB")
        if max(im.size) > SIDE:
            k = SIDE / max(im.size)
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.NEAREST)
        a = np.asarray(im)
    grey = np.all(np.abs(a.astype(np.int16) - EMPTY) <= 1, axis=2)
    used = int((~grey).sum())
    return float((a.max(axis=2) <= BLACK).sum() / used) if used else 0.0


def check(mesh_dir: Path) -> dict | None:
    """Whether a mesh's texture is black-patched: {share, textures, black, version, of}, measured again only when its
    textures change (of: their names, sizes and times). None for a mesh with no texture of its own."""
    files = textures(mesh_dir)
    if not files:
        return None
    of = [[f.name, f.stat().st_size, int(f.stat().st_mtime)] for f in files]
    kept = Path(mesh_dir) / FILE
    try:
        old = json.loads(kept.read_text())
        if old.get("version") == VERSION and old.get("of") == of:
            return old
    except (OSError, ValueError):
        pass
    worst = max(share(f) for f in files)
    out = {"share": round(worst, 4), "textures": len(files), "black": worst >= BLACK_LEAST, "version": VERSION, "of": of}
    try:
        kept.write_text(json.dumps(out))
    except OSError:
        pass
    return out
