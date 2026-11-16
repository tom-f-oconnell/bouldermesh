"""Tests of texturing a mesh again (textureagain.py): reading its faces back from a GLB as TextureMesh writes one (split
along seams), the PLY made of them, the check that the new mesh's faces are the old one's, and putting it in place.
The tools themselves (COLMAP, OpenMVS) aren't run.

Run: python3 tests/test_textureagain.py
"""
import json, pathlib, struct, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
import numpy as np
import textureagain


def glb(path, P, F):
    """A minimal GLB of positions P and triangles F, as one primitive."""
    P, F = np.asarray(P, "<f4"), np.asarray(F, "<u4")
    pb, fb = P.tobytes(), F.tobytes()
    j = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(pb) + len(fb)}],
         "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pb)}, {"buffer": 0, "byteOffset": len(pb), "byteLength": len(fb)}],
         "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(P), "type": "VEC3"},
                       {"bufferView": 1, "componentType": 5125, "count": F.size, "type": "SCALAR"}],
         "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}]}
    js = json.dumps(j).encode()
    js += b" " * (-len(js) % 4)
    body = pb + fb
    body += b"\0" * (-len(body) % 4)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(body)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(body), 0x004E4942) + body)


with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    # two triangles sharing an edge, split along it as at a texture seam: 6 vertices, welded back to 4
    P = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]
    glb(d / "old.glb", P, [[0, 1, 2], [3, 4, 5]])
    V, F = textureagain.faces_of(d / "old.glb")
    assert len(V) == 4 and F.shape == (2, 3) and len({tuple(V[i]) for i in F.ravel()}) == 4, (V, F)
    assert (V[F[0]] == np.array(P[:3], "f4")).all() and (V[F[1]] == np.array(P[3:], "f4")).all(), "each face's corners kept in order"

    textureagain.write_ply(d / "mesh.ply", V, F)
    head, body = (d / "mesh.ply").read_bytes().split(b"end_header\n", 1)
    assert b"element vertex 4" in head and b"element face 2" in head and len(body) == 4 * 12 + 2 * 13

    # the same faces in the same order, split differently (as a new atlas would): the same; reordered or moved: not
    glb(d / "new.glb", [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], [[0, 1, 2], [1, 3, 2]])
    assert textureagain.same_faces(d / "old.glb", d / "new.glb") is None
    glb(d / "swapped.glb", [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], [[1, 3, 2], [0, 1, 2]])
    assert "order or places" in textureagain.same_faces(d / "old.glb", d / "swapped.glb")
    glb(d / "fewer.glb", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]])
    assert textureagain.same_faces(d / "old.glb", d / "fewer.glb") == "1 faces, where the mesh had 2"

    # put in place: mesh.glb and the textures replaced, shrunk copies dropped, the old ones kept in texture-before/
    mesh = d / "mesh"
    (mesh / "smaller").mkdir(parents=True)
    (mesh / "mesh.glb").write_bytes((d / "old.glb").read_bytes())
    (mesh / "scene_dense_mesh_texture_0.png").write_bytes(b"old texture")
    (mesh / "smaller" / "scene_dense_mesh_texture_0-2048.jpg").write_bytes(b"old shrunk")
    work = d / "work"
    work.mkdir()
    (work / textureagain.GLB).write_bytes((d / "new.glb").read_bytes())
    (work / "scene_dense_mesh_texture_0.png").write_bytes(b"new texture")
    textureagain.install(mesh, work / textureagain.GLB, lambda s: None)
    assert (mesh / "mesh.glb").read_bytes() == (d / "new.glb").read_bytes()
    assert (mesh / "scene_dense_mesh_texture_0.png").read_bytes() == b"new texture"
    assert not (mesh / "smaller" / "scene_dense_mesh_texture_0-2048.jpg").exists(), "made again from the new texture"
    assert (mesh / "texture-before" / "mesh.glb").read_bytes() == (d / "old.glb").read_bytes()
    assert (mesh / "texture-before" / "scene_dense_mesh_texture_0.png").read_bytes() == b"old texture"

    # what it needs: a GLB, and its kept reconstruction
    try:
        textureagain.texture_again(d / "nothing", d, None, d / "w2", print)
        raise AssertionError("no mesh.glb")
    except RuntimeError as e:
        assert "GLB" in str(e)
    try:
        textureagain.texture_again(mesh, d, None, d / "w3", print)
        raise AssertionError("no reconstruction kept")
    except RuntimeError as e:
        assert "reconstruction wasn't kept" in str(e)
# how far it is, from the tools' own counts, as a fraction of the whole
seen = []
w = textureagain._watch("undistort", lambda step, f: seen.append((step, round(f, 3))))
w("I20261116 Undistorting image [5/10]")
w("unrelated line")
t = textureagain._watch("texture", lambda step, f: seen.append((step, round(f, 3))))
t("Initialized views 18 (100.00%, 463ms)")
t("08:51:37 [ScnTextr] Generating texture atlas and image completed: 312 patches")
assert seen == [("undistort", 0.175), ("texture", 0.658), ("texture", 0.944)], seen
print("ALL TEXTURE AGAIN TESTS PASSED")
