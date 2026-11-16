#!/usr/bin/env python3
"""Telling a black-patched texture (OpenMVS 2.4.0's unpatched TextureMesh) from a sound one (texturecheck.py), on
made-up atlases: charts of colour on the empty grey, some of them black."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time

import numpy as np
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import texturecheck  # noqa: E402


def atlas(path: pathlib.Path, black: float, size: int = 2048) -> None:
    """An atlas half charts (rock-coloured noise) and half grey, `black` of the charts' pixels black, in blotches."""
    rng = np.random.default_rng(1)
    a = np.full((size, size, 3), 128, np.uint8)
    h = size // 2
    a[:h] = rng.integers(60, 200, (h, size, 3), dtype=np.uint8)
    rows = int(round(black * h))
    a[:rows, :size] = 0                         # a blotch as tall as that share of the charts
    Image.fromarray(a).save(path)


with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    for black, want in ((0.04, False), (0.15, False), (0.25, True), (0.45, True)):
        m = d / f"m{black}"
        m.mkdir()
        atlas(m / "scene_dense_mesh_texture_0.png", black)
        got = texturecheck.check(m)
        assert abs(got["share"] - black) < 0.01 and got["black"] is want and got["textures"] == 1, (black, got)
    # the worst of several textures counts; the old ones kept in texture-before/ don't
    m = d / "two"
    (m / "texture-before").mkdir(parents=True)
    atlas(m / "scene_dense_mesh_texture_0.png", 0.03)
    atlas(m / "scene_dense_mesh_texture_1.png", 0.4)
    atlas(m / "texture-before" / "scene_dense_mesh_texture_0.png", 0.9)
    got = texturecheck.check(m)
    assert got["black"] and got["textures"] == 2 and abs(got["share"] - 0.4) < 0.01, got
    # kept beside the mesh, and measured again only when a texture changes
    kept = json.loads((m / texturecheck.FILE).read_text())
    assert kept["share"] == got["share"]
    (m / texturecheck.FILE).write_text(json.dumps({**kept, "share": 0.99}))
    assert texturecheck.check(m)["share"] == 0.99, "unchanged textures: what was kept"
    time.sleep(1.1)
    atlas(m / "scene_dense_mesh_texture_1.png", 0.02)      # textured again
    again = texturecheck.check(m)
    assert not again["black"] and again["share"] < 0.05, again
    assert texturecheck.check(d / "nothing" if (d / "nothing").mkdir() is None else d) is None, "no texture: nothing to say"
print("black-patched textures told apart OK")
print("ALL TEXTURE CHECK TESTS PASSED")
