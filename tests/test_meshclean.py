#!/usr/bin/env python3
"""Hiding a mesh's stray pieces, on small GLBs built here, laid out as OpenMVS writes them. Offline."""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import struct
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import meshclean  # noqa: E402

try:
    import numpy as np
except ImportError:
    print("numpy not installed: skipping (the app then shows meshes whole)")
    sys.exit(0)


def grid(nx: int, ny: int, x0: float = 0.0, spacing: float = 1.0, split_seam: bool = False):
    """A flat sheet of 2 * nx * ny triangles. With split_seam, its vertices are doubled down the middle, as along a texture seam."""
    P, I = [], []
    index = {}

    def vertex(i, j, side=0):
        key = (i, j, side if split_seam and i == nx // 2 else 0)
        if key not in index:
            index[key] = len(P)
            P.append((x0 + i * spacing, j * spacing, 0.0))
        return index[key]

    for i in range(nx):
        for j in range(ny):
            right = 1
            a, b = vertex(i, j, right), vertex(i + 1, j, 0)
            c, d = vertex(i, j + 1, right), vertex(i + 1, j + 1, 0)
            I += [(a, b, c), (b, d, c)]
    return np.array(P, "<f4"), np.array(I, "<u4")


def glb(path: pathlib.Path, parts: list[tuple], data_uri_for_second: bool = True) -> None:
    """Writes primitives, the first in the BIN chunk and any others in a data URI, as OpenMVS does for split textures."""
    j = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}], "buffers": [], "bufferViews": [], "accessors": [],
         "images": [{"uri": f"tex_{k}.png"} for k in range(len(parts))], "textures": [{"source": k} for k in range(len(parts))],
         "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": k}}} for k in range(len(parts))],
         "meshes": [{"primitives": []}]}
    chunks = []
    for k, (P, I) in enumerate(parts):
        uv = P[:, :2].astype("<f4") / max(1.0, float(P.max()))
        blob = bytearray()
        views = []
        for data, target in ((P, 34962), (uv, 34962), (I, 34963)):
            views.append((len(blob), data.nbytes, target))
            blob += data.tobytes()
        b = k if data_uri_for_second else 0
        if data_uri_for_second and k:
            j["buffers"].append({"byteLength": len(blob), "uri": "data:application/octet-stream;base64," + base64.b64encode(bytes(blob)).decode()})
        else:
            j["buffers"].append({"byteLength": len(blob)})
            chunks.append(bytes(blob))
        base_view = len(j["bufferViews"])
        for off, n, target in views:
            j["bufferViews"].append({"buffer": b, "byteOffset": off, "byteLength": n, "target": target})
        base_acc = len(j["accessors"])
        j["accessors"] += [{"bufferView": base_view, "componentType": 5126, "count": len(P), "type": "VEC3", "min": [float(x) for x in P.min(0)], "max": [float(x) for x in P.max(0)]},
                           {"bufferView": base_view + 1, "componentType": 5126, "count": len(P), "type": "VEC2"},
                           {"bufferView": base_view + 2, "componentType": 5125, "count": I.size, "type": "SCALAR"}]
        j["meshes"][0]["primitives"].append({"attributes": {"POSITION": base_acc, "TEXCOORD_0": base_acc + 1}, "indices": base_acc + 2, "material": k})
    text = json.dumps(j).encode()
    text += b" " * (-len(text) % 4)
    bin_ = chunks[0] + b"\0" * (-len(chunks[0]) % 4)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(text) + 8 + len(bin_)))
        f.write(struct.pack("<II", len(text), 0x4E4F534A) + text)
        f.write(struct.pack("<II", len(bin_), 0x004E4942) + bin_)


def shift(part, dx: float):
    P, I = part
    P = P.copy()
    P[:, 0] += dx
    return P, I


def join(*parts):
    Ps, Is, off = [], [], 0
    for P, I in parts:
        Ps.append(P)
        Is.append(I + off)
        off += len(P)
    return np.vstack(Ps), np.vstack(Is).astype("<u4")


# the rock: dense and with a texture seam down its middle, and in two textures
main_a = grid(40, 40, split_seam=True)                        # 3200 faces
main_b = grid(10, 40, x0=40.0)                                # 800 more, touching it: the same piece, in the second texture
big_second = grid(30, 20, x0=100.0)                           # 1200 faces, just as dense: real rock in two pieces, kept
tiny = grid(2, 2, x0=300.0)                                   # 8 faces, under 1%
sheet = grid(10, 10, x0=500.0, spacing=50.0)                  # 200 faces (5%), but 2500x sparser, with most of the surface
borderline = grid(5, 4, x0=1100.0, spacing=2.0)               # 40 faces: exactly 1% of 4000, and 4x sparser: stays
scene = [join(main_a, big_second, tiny), join(main_b, sheet, borderline)]

with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    glb(d / "mesh.glb", scene)
    before = hashlib.sha1((d / "mesh.glb").read_bytes()).hexdigest()

    # --- finding pieces, across seams and textures
    pieces, labels, counts = meshclean.analyse(d / "mesh.glb")
    assert [p["faces"] for p in pieces] == [4000, 1200, 200, 40, 8], [p["faces"] for p in pieces]
    assert counts == [{"mesh": 0, "primitive": 0, "faces": 3200 + 1200 + 8}, {"mesh": 0, "primitive": 1, "faces": 800 + 200 + 40}], counts
    assert len(labels) == 5448 and labels[0] == 0 and labels[3200] == 1 and labels[4400] == 4, labels[[0, 3200, 4400]]
    assert labels[4408] == 0 and labels[5208] == 2 and labels[5408] == 3, labels[[4408, 5208, 5408]]
    assert pieces[1]["density"] == 1.0 and pieces[2]["density"] < 0.001 and pieces[2]["area"] > 100 * pieces[0]["area"], pieces[2]
    assert abs(pieces[3]["density"] - 0.25) < 1e-6, pieces[3]
    assert pieces[2]["size"] == [500.0, 500.0, 0.0] and pieces[0]["center"][0] == 25.0, pieces[0]
    print("pieces OK")

    # --- the rule
    assert meshclean.automatic(pieces) == [2, 4], "a sparse sheet and a tiny piece go; real second piece and borderline one stay"
    single = [{"id": 0, "faces": 10, "density": 1.0}]
    assert meshclean.automatic(single) == []
    print("rule OK")

    # --- the step: nothing changes the mesh itself
    logged = []
    rec = meshclean.clean(d, logged.append)
    assert rec["removed"] == [2, 4] == rec["automatic"] and rec["label_bytes"] == 2, rec
    assert "hiding 2 of 5 pieces" in logged[0] and "unchanged" in logged[0], logged
    assert (d / meshclean.FACES_FILE).stat().st_size == 2 * 5448
    assert np.array_equal(np.frombuffer((d / meshclean.FACES_FILE).read_bytes(), "<u2"), labels)
    s = meshclean.summary(rec)
    assert (s["pieces"], s["hidden"], s["hidden_faces"], s["faces"], s["edited"]) == (5, 2, 208, 5448, False), s
    assert meshclean.clean(d, logged.append) == rec and len(logged) == 1, "already done: not redone"

    # --- click-to-restore edits, kept across reloads, and undone by running the rule again
    rec = meshclean.set_removed(d, [4, 2, 3, 3])
    assert rec["removed"] == [2, 3, 4] and meshclean.load(d)["removed"] == [2, 3, 4] and meshclean.summary(rec)["edited"], rec["removed"]
    assert meshclean.set_removed(d, [])["removed"] == []
    for bad, why in (([99], "no such piece"), ([0, 1, 2, 3, 4], "at least one")):
        try:
            meshclean.set_removed(d, bad)
            raise AssertionError(bad)
        except ValueError as e:
            assert why in str(e), e
    assert meshclean.clean(d, logged.append) ["removed"] == [], "edits aren't overwritten by a later run"
    assert meshclean.write_cleaned(d) is None, "nothing hidden: the original is what to download"
    assert meshclean.clean(d, logged.append, force=True)["removed"] == [2, 4]

    # --- the cleaned download
    out = meshclean.write_cleaned(d)
    assert out.name == meshclean.CLEANED_FILE
    p2, l2, c2 = meshclean.analyse(out)
    assert [p["faces"] for p in p2] == [4000, 1200, 40], [p["faces"] for p in p2]
    assert c2 == [{"mesh": 0, "primitive": 0, "faces": 4400}, {"mesh": 0, "primitive": 1, "faces": 840}], c2
    j2, b2 = meshclean._read_glb(out)
    assert [i["uri"] for i in j2["images"]] == ["tex_0.png", "tex_1.png"] and len(j2["buffers"]) == 1, j2["images"]
    first = j2["meshes"][0]["primitives"][0]
    assert j2["accessors"][first["attributes"]["POSITION"]]["count"] < len(scene[0][0]), "unused vertices are dropped"
    pos = j2["accessors"][j2["meshes"][0]["primitives"][1]["attributes"]["POSITION"]]
    assert pos["max"][0] == 1110.0 and pos["min"][0] == 40.0, pos
    uv = meshclean._accessor(j2, b2, first["attributes"]["TEXCOORD_0"])
    P2 = meshclean._accessor(j2, b2, first["attributes"]["POSITION"])
    assert np.allclose(uv, P2[:, :2] / float(scene[0][0].max())), "each kept vertex keeps its texture coordinates"
    assert meshclean.write_cleaned(d) == out
    rec = meshclean.set_removed(d, [4])
    assert not out.exists(), "an edit invalidates it"
    assert [p["faces"] for p in meshclean.analyse(meshclean.write_cleaned(d))[0]] == [4000, 1200, 200, 40]
    # a whole texture's worth of pieces can go
    meshclean.set_removed(d, [1, 4])
    assert meshclean.analyse(meshclean.write_cleaned(d))[2][0]["faces"] == 3200
    print("cleaned download OK")

    # --- deleting it: gone, the mesh whole again, and not brought back by the next pass
    meshclean.delete(d)
    assert meshclean.load(d) == {"deleted": True, "created": meshclean.load(d)["created"]}
    assert not (d / meshclean.FACES_FILE).exists() and not (d / meshclean.CLEANED_FILE).exists()
    assert meshclean.summary(meshclean.load(d)) == {"deleted": True}
    assert meshclean.clean(d, logged.append).get("deleted"), "a deleted cleanup stays deleted"
    assert meshclean.write_cleaned(d) is None
    try:
        meshclean.set_removed(d, [1])
        raise AssertionError("nothing to edit")
    except ValueError:
        pass
    assert meshclean.clean(d, logged.append, force=True)["removed"] == [2, 4], "...until asked for"
    assert hashlib.sha1((d / "mesh.glb").read_bytes()).hexdigest() == before, "the mesh itself never changes"
    print("delete OK")

# --- what it can't work on
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    assert meshclean.clean(d) is None and meshclean.load(d) is None and meshclean.summary(None) is None, "no GLB, as with an untextured PLY"
    (d / "mesh.glb").write_bytes(b"ply\nnot a glb at all")
    try:
        meshclean.clean(d)
        raise AssertionError("not a GLB")
    except ValueError as e:
        assert "not a GLB" in str(e), e
    assert meshclean.load(d) is None, "a failure leaves nothing behind"
    glb(d / "mesh.glb", [grid(3, 3)])
    assert meshclean.clean(d, lambda s: None)["removed"] == [], "one piece: nothing to hide"
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    glb(d / "my boulder.glb", [join(grid(20, 20), grid(1, 1, x0=100.0))])   # an upload, under its own name
    assert meshclean.mesh_file(d).name == "my boulder.glb"
    assert meshclean.clean(d, lambda s: None)["removed"] == [1]
    assert meshclean.write_cleaned(d).name == meshclean.CLEANED_FILE
    assert meshclean.mesh_file(d).name == "my boulder.glb", "its own cleaned copy doesn't confuse it"
    glb(d / "another.glb", [grid(2, 2)])
    assert meshclean.mesh_file(d) is None, "which one? don't guess"
print("edge cases OK")

# --- connected components match a known implementation, on awkward shapes
try:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
except ImportError:
    coo_matrix = None
rng = np.random.default_rng(0)
for nv, nf in ((50, 12), (2000, 800), (5000, 6000), (40000, 30000)):
    F = rng.integers(0, nv, (nf, 3))
    roots = meshclean._components(F, nv)
    assert (roots[F[:, 0]] == roots[F[:, 1]]).all() and (roots[F[:, 1]] == roots[F[:, 2]]).all()
    if coo_matrix is not None:
        g = coo_matrix((np.ones(nf * 2), (np.r_[F[:, 0], F[:, 1]], np.r_[F[:, 1], F[:, 2]])), shape=(nv, nv))
        n, theirs = connected_components(g, directed=False)
        assert len(np.unique(roots)) == n, (len(np.unique(roots)), n)
        pairs = np.unique(np.c_[roots, theirs], axis=0)
        assert len(pairs) == n, "the same grouping"
chain = np.c_[np.arange(0, 3000), np.arange(1, 3001), np.arange(1, 3001)]     # a long thin strip, the slowest shape to resolve
assert len(np.unique(meshclean._components(chain, 3001))) == 1
print("components OK")
print("ALL MESH CLEANUP TESTS PASSED")
