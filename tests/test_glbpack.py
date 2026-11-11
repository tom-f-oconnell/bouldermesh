#!/usr/bin/env python3
"""Packed copies of meshes for the viewer (glbpack.py), on a small GLB built here as OpenMVS writes them: two textures,
the second's geometry in a data URI, and stray pieces. Offline; needs node or deno, and numpy."""
from __future__ import annotations

import base64
import json
import os
import pathlib
import struct
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import glbpack  # noqa: E402
import meshclean  # noqa: E402

try:
    import numpy as np
except ImportError:
    print("numpy not installed: skipping")
    sys.exit(0)
if not glbpack.available():
    print("neither node nor deno is installed: skipping (the viewer then gets meshes as they are)")
    sys.exit(0)


def grid(nx: int, ny: int, x0: float = 0.0, seam: bool = False):
    """A wavy sheet of 2 * nx * ny triangles; with seam, its vertices doubled down the middle, as along a texture seam."""
    P, I, index = [], [], {}

    def vertex(i, j, side=0):
        key = (i, j, side if seam and i == nx // 2 else 0)
        if key not in index:
            index[key] = len(P)
            P.append((x0 + i, float(j), 0.3 * np.sin(i / 3) * np.cos(j / 4)))
        return index[key]
    for i in range(nx):
        for j in range(ny):
            a, b, c, d = vertex(i, j, 1), vertex(i + 1, j), vertex(i, j + 1, 1), vertex(i + 1, j + 1)
            I += [(a, b, c), (b, d, c)]
    return np.array(P, "<f4"), np.array(I, "<u4")


def join(*parts):
    Ps, Is, off = [], [], 0
    for P, I in parts:
        Ps.append(P)
        Is.append(I + off)
        off += len(P)
    return np.vstack(Ps), np.vstack(Is).astype("<u4")


def write_glb(path: pathlib.Path, parts: list[tuple], extensions: list[str] = ()) -> None:
    """Primitives with texture coordinates, the first in the BIN chunk and the others in data URIs."""
    j = {"asset": {"version": "2.0", "generator": "OpenMVS"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
         "buffers": [], "bufferViews": [], "accessors": [], "extensionsUsed": ["KHR_materials_unlit", *extensions],
         "images": [{"uri": f"tex_{k}.png"} for k in range(len(parts))], "textures": [{"source": k} for k in range(len(parts))],
         "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": k}}} for k in range(len(parts))], "meshes": [{"primitives": []}]}
    first = b""
    for k, (P, I) in enumerate(parts):
        uv = ((P[:, :2] - P[:, :2].min(0)) / np.ptp(P[:, :2], 0).max()).astype("<f4")
        blob, views = bytearray(), []
        for data, target in ((P, 34962), (uv, 34962), (I, 34963)):
            views.append((len(blob), data.nbytes, target))
            blob += data.tobytes()
        if k:
            j["buffers"].append({"byteLength": len(blob), "uri": "data:application/octet-stream;base64," + base64.b64encode(bytes(blob)).decode()})
        else:
            j["buffers"].append({"byteLength": len(blob)})
            first = bytes(blob)
        view = len(j["bufferViews"])
        j["bufferViews"] += [{"buffer": k, "byteOffset": off, "byteLength": n, "target": t} for off, n, t in views]
        acc = len(j["accessors"])
        j["accessors"] += [{"bufferView": view, "componentType": 5126, "count": len(P), "type": "VEC3", "min": P.min(0).tolist(), "max": P.max(0).tolist()},
                           {"bufferView": view + 1, "componentType": 5126, "count": len(P), "type": "VEC2"},
                           {"bufferView": view + 2, "componentType": 5125, "count": I.size, "type": "SCALAR"}]
        j["meshes"][0]["primitives"].append({"attributes": {"POSITION": acc, "TEXCOORD_0": acc + 1}, "indices": acc + 2, "material": k})
    text = json.dumps(j).encode()
    text += b" " * (-len(text) % 4)
    first += b"\0" * (-len(first) % 4)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 28 + len(text) + len(first)))
        f.write(struct.pack("<II", len(text), 0x4E4F534A) + text + struct.pack("<II", len(first), 0x004E4942) + first)


rock = grid(60, 40, seam=True)              # 4800 faces
rock_b = grid(20, 40, x0=60.0)              # 1600 more, touching it, in the second texture
stray = grid(2, 2, x0=300.0)                # 8 faces, hidden
scene = [join(rock, stray), rock_b]
FACES = 4808 + 1600

with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    src = d / "mesh.glb"
    write_glb(src, scene)
    original = src.read_bytes()
    assert glbpack.image_files(src) == ["tex_0.png", "tex_1.png"]
    assert glbpack.record(src) is None and glbpack.path(src, "full") is None, "nothing packed yet"

    # --- the full copy: every face, in another order, which maps back to the mesh's own
    lines = []
    rec = glbpack.pack(src, lines.append, extra={"textures": {"tex_0.png": {"side": 2048}}}, preview_faces=1000)
    assert glbpack.made_for(rec, 1000) and not glbpack.made_for(rec, 1500) and not glbpack.made_for(rec, 0)
    assert src.read_bytes() == original, "the mesh's own file doesn't change"
    assert rec["full"]["faces"] == FACES == rec["source_faces"] and rec["textures"] == {"tex_0.png": {"side": 2048}}, rec
    assert rec["full"]["bytes"] < len(original) / 2, (rec["full"]["bytes"], len(original))
    assert "packed for viewing" in lines[0] and "preview of" in lines[0], lines
    assert glbpack.record(src) == rec and sorted(p.name for p in (d / glbpack.DIR).iterdir()) == [
        "mesh.full.faces.bin", "mesh.full.glb", "mesh.json", "mesh.preview.faces.bin", "mesh.preview.glb"]
    packed = glbpack.path(src, "full").read_bytes()
    n = struct.unpack("<I", packed[12:16])[0]
    j = json.loads(packed[20:20 + n])
    assert j["extensionsRequired"] == ["EXT_meshopt_compression"] and "KHR_materials_unlit" in j["extensionsUsed"], j["extensionsUsed"]
    assert j["asset"]["extras"]["bouldermesh"] == {"lod": "full", "faces": FACES} and j["asset"]["generator"] == "OpenMVS"
    assert j["images"] == [{"uri": "tex_0.png"}, {"uri": "tex_1.png"}], "the same texture files, beside it"
    prims = j["meshes"][0]["primitives"]
    assert [j["accessors"][p["indices"]]["count"] // 3 for p in prims] == [4808, 1600], "the same primitives and faces"
    uv = j["accessors"][prims[0]["attributes"]["TEXCOORD_0"]]
    assert (uv["componentType"], uv["normalized"]) == (5123, True), uv
    pos = j["accessors"][prims[0]["attributes"]["POSITION"]]
    assert pos["componentType"] == 5126 and abs(pos["max"][0] - 302) < 1e-2 and abs(pos["min"][0]) < 1e-3, "in the same coordinates"
    origin = np.frombuffer(glbpack.path(src, "full", faces=True).read_bytes(), "<u4")
    assert np.array_equal(np.sort(origin), np.arange(FACES)) and not np.array_equal(origin, np.arange(FACES)), "every face once, reordered"
    assert origin[:4808].max() < 4808 <= origin[4808:].min(), "each primitive's own"
    print("full copy OK")

    # --- the preview: fewer faces, each from the piece its source face is in
    prev = rec["preview"]
    assert 800 <= prev["faces"] <= 1200 and prev["bytes"] < rec["full"]["bytes"], prev
    porigin = np.frombuffer(glbpack.path(src, "preview", faces=True).read_bytes(), "<u4")
    assert len(porigin) == prev["faces"] and porigin.max() < FACES
    print("preview OK")

    # --- the cleanup, put in each copy's order
    cleanup = meshclean.clean(d, lambda line: None)
    assert cleanup["removed"] == [2], cleanup
    labels = np.frombuffer((d / meshclean.FACES_FILE).read_bytes(), "<u2")
    full = np.frombuffer(glbpack.labels(src, "full", d / meshclean.FACES_FILE, 2), "<u2")
    assert np.array_equal(full, labels[origin]) and (full == 2).sum() == 8
    pl = np.frombuffer(glbpack.labels(src, "preview", d / meshclean.FACES_FILE, 2), "<u2")
    assert len(pl) == prev["faces"] and set(pl.tolist()) <= {0, 1, 2} and (pl == 0).sum() > (pl == 1).sum() > (pl == 2).sum()
    (d / "small.bin").write_bytes(b"\0\0" * 10)
    try:
        glbpack.labels(src, "full", d / "small.bin", 2)
        raise AssertionError("a cleanup of another mesh")
    except ValueError as e:
        assert "doesn't fit" in str(e)
    print("cleanup OK")

    # --- out of date, then packed again; a mesh under twice the preview's size gets none, and its old one goes
    os.utime(src, ns=(src.stat().st_atime_ns, src.stat().st_mtime_ns + 10**9))
    assert glbpack.record(src) is None and glbpack.path(src, "full") is None and glbpack.labels(src, "full", d / meshclean.FACES_FILE, 2) is None
    rec = glbpack.pack(src, lambda line: None, preview_faces=FACES // 2 + 1)
    assert "preview" not in rec and glbpack.path(src, "preview") is None and not (d / glbpack.DIR / "mesh.preview.glb").exists()
    rec = glbpack.pack(src, lambda line: None, preview_faces=FACES // 2)
    assert 0.8 * FACES // 2 <= rec["preview"]["faces"] <= FACES // 2 and glbpack.path(src, "preview")
    rec = glbpack.pack(src, lambda line: None, preview_faces=0)
    assert "preview" not in rec and glbpack.made_for(rec, 0), "0: no previews"
    print("repacking OK")

    # --- the versions shown when the site doesn't show all textures: coloured per vertex, or untextured
    import retexture
    (d / "permitted").mkdir()
    retexture.write_coloured(src, d / "permitted" / "mesh.glb", lambda i, P: np.c_[P[:, :1] * 0 + 0.25, P[:, :1] * 0 + 0.5, P[:, :1] * 0 + 1.0])
    rec = glbpack.pack(d / "permitted" / "mesh.glb", lambda line: None)
    packed = glbpack.path(d / "permitted" / "mesh.glb", "full").read_bytes()
    j = json.loads(packed[20:20 + struct.unpack("<I", packed[12:16])[0]])
    colour = j["accessors"][j["meshes"][0]["primitives"][0]["attributes"]["COLOR_0"]]
    assert (colour["componentType"], colour["type"], colour["normalized"]) == (5123, "VEC4", True) and rec["full"]["faces"] == FACES, colour
    assert (d / "permitted" / glbpack.DIR / "mesh.json").exists() and not j.get("images"), "kept beside it, with no images"
    untextured = meshclean.strip_textures(src, d / "mesh.untextured.glb")
    assert glbpack.pack(untextured, lambda line: None)["full"]["faces"] == FACES and glbpack.path(untextured, "full")
    assert glbpack.path(src, "full"), "each version has its own"
    print("permitted and untextured versions OK")

    # --- what it won't pack, which leaves the mesh's own file to be sent
    write_glb(d / "draco.glb", [rock_b], extensions=["KHR_draco_mesh_compression"])
    try:
        glbpack.pack(d / "draco.glb", lambda line: None)
        raise AssertionError("a compressed GLB")
    except RuntimeError as e:
        assert "already compressed" in str(e), e
    assert glbpack.record(d / "draco.glb") is None and not list((d / glbpack.DIR).glob(".*partial"))
    print("refusals OK")
print("ALL GLB PACKING TESTS PASSED")
