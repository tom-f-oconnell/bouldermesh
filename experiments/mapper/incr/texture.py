"""Textures from a capture's photos first: the rock as the phone saw it, web photos only where the capture didn't reach.

TextureMesh (OpenMVS) picks each face's photo by how much of it the photo shows, so a close capture photo usually
wins where it sees a face, but not always, and a web photo's light, season or people can win too. So, in two passes
on the same mesh, the faces are split by who may texture them:

  A: TextureMesh with only the capture's photos (--views-file), faces no capture photo sees left in a marker colour;
  B: TextureMesh with every photo, for the faces A left (only if there are any);

and one GLB is made of A's faces where A coloured them and B's elsewhere (two materials, each with its own atlas
pages). Each pass levels its own seams; the boundary between the two isn't levelled (said in what's returned, with
how long the boundary is). The share of the surface (by area) A textured is what the capture covers.

    texture.py split <obj A> <obj B or -> <out.glb> [--marker ff00ff]    the composite, and its numbers as JSON
    texture.py ply <mesh.glb> <out.ply>                                   a GLB's shape as a PLY, to texture again

Pure numpy and PIL: run where the OBJs are (core.py texture steps write them in dense.new).
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import numpy as np

MARKER = (255, 0, 255)


def read_obj(path: Path) -> dict:
    """OpenMVS's textured OBJ: v, vt, faces as (v index, vt index) triples, each face's material, and the materials'
    texture images (from the .mtl)."""
    V, T, F, FT, M = [], [], [], [], []
    mtl, cur = {}, None
    base = path.parent
    for line in open(path, errors="replace"):
        if line.startswith("v "):
            V.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("vt "):
            T.append([float(x) for x in line.split()[1:3]])
        elif line.startswith("f "):
            idx = [p.split("/") for p in line.split()[1:4]]
            F.append([int(i[0]) - 1 for i in idx])
            FT.append([int(i[1]) - 1 if len(i) > 1 and i[1] else -1 for i in idx])
            M.append(cur)
        elif line.startswith("usemtl "):
            cur = line.split(None, 1)[1].strip()
        elif line.startswith("mtllib "):
            name = None
            for l2 in open(base / line.split(None, 1)[1].strip(), errors="replace"):
                if l2.startswith("newmtl "):
                    name = l2.split(None, 1)[1].strip()
                elif l2.strip().startswith("map_Kd") and name:
                    mtl[name] = base / l2.split(None, 1)[1].strip()
    return {"V": np.array(V), "T": np.array(T), "F": np.array(F, np.int64), "FT": np.array(FT, np.int64),
            "M": np.array(M, object), "maps": mtl}


def face_colours(obj: dict) -> np.ndarray:
    """The colour each face's texture has at its middle (one sample a face, from its own atlas page)."""
    from PIL import Image
    out = np.zeros((len(obj["F"]), 3), np.uint8)
    for name, img_path in obj["maps"].items():
        faces = np.flatnonzero(obj["M"] == name)
        if not len(faces):
            continue
        im = np.asarray(Image.open(img_path).convert("RGB"))
        h, w = im.shape[:2]
        uv = obj["T"][obj["FT"][faces]].mean(axis=1)
        x = np.clip((uv[:, 0] * w).astype(int), 0, w - 1)
        y = np.clip(((1 - uv[:, 1]) * h).astype(int), 0, h - 1)
        out[faces] = im[y, x]
    return out


def areas(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    return np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1) / 2


def covered(obj: dict, marker=MARKER, tol: int = 8) -> np.ndarray:
    """Faces a pass textured: those whose colour isn't the marker it was told to leave empty faces."""
    c = face_colours(obj).astype(int)
    return np.abs(c - np.array(marker)).max(axis=1) > tol


def boundary_edges(F: np.ndarray, a_faces: np.ndarray) -> int:
    """How many edges have a face of A's on one side and one of B's on the other."""
    edges = {}
    for f, tri in enumerate(F):
        for i in range(3):
            e = tuple(sorted((tri[i], tri[(i + 1) % 3])))
            edges.setdefault(e, []).append(f)
    return sum(1 for fs in edges.values() if len(fs) == 2 and a_faces[fs[0]] != a_faces[fs[1]])


def write_glb(path: Path, parts: list[dict]) -> None:
    """A GLB of triangle primitives, each {V (n x 3), UV (n x 2), I (faces x 3, into its own V), image: Path}: one
    material (an unlit-looking base colour texture) a primitive."""
    from PIL import Image
    import io
    blobs, views, accessors, meshes_prims, images, textures, materials = [], [], [], [], [], [], []
    offset = 0

    def add(data: bytes, target=None) -> int:
        nonlocal offset
        pad = (-len(data)) % 4
        blobs.append(data + b"\0" * pad)
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data), **({"target": target} if target else {})})
        offset += len(data) + pad
        return len(views) - 1
    for p in parts:
        V = np.asarray(p["V"], np.float32)
        UV = np.asarray(p["UV"], np.float32)
        I = np.asarray(p["I"], np.uint32)
        vv = add(V.tobytes(), 34962)
        accessors.append({"bufferView": vv, "componentType": 5126, "count": len(V), "type": "VEC3",
                          "min": V.min(axis=0).tolist(), "max": V.max(axis=0).tolist()})
        a_pos = len(accessors) - 1
        uv = add(UV.tobytes(), 34962)
        accessors.append({"bufferView": uv, "componentType": 5126, "count": len(UV), "type": "VEC2"})
        a_uv = len(accessors) - 1
        ii = add(I.tobytes(), 34963)
        accessors.append({"bufferView": ii, "componentType": 5125, "count": int(I.size), "type": "SCALAR"})
        a_i = len(accessors) - 1
        buf = io.BytesIO()
        Image.open(p["image"]).convert("RGB").save(buf, "JPEG", quality=92)
        iv = add(buf.getvalue())
        images.append({"bufferView": iv, "mimeType": "image/jpeg"})
        textures.append({"source": len(images) - 1})
        materials.append({"pbrMetallicRoughness": {"baseColorTexture": {"index": len(textures) - 1}, "metallicFactor": 0.0,
                                                   "roughnessFactor": 1.0}, "doubleSided": True})
        meshes_prims.append({"attributes": {"POSITION": a_pos, "TEXCOORD_0": a_uv}, "indices": a_i, "material": len(materials) - 1})
    gltf = {"asset": {"version": "2.0", "generator": "experiments/mapper/incr/texture.py"}, "scene": 0, "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}], "meshes": [{"primitives": meshes_prims}], "buffers": [{"byteLength": offset}],
            "bufferViews": views, "accessors": accessors, "images": images, "textures": textures, "materials": materials}
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    bin_ = b"".join(blobs)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bin_)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(bin_), 0x004E4942) + bin_)


def parts_of(obj: dict, faces: np.ndarray) -> list[dict]:
    """The faces named, as one primitive per atlas page: vertices taken per face corner (a corner's UV is its own)."""
    out = []
    for name, img in obj["maps"].items():
        fs = faces[obj["M"][faces] == name]
        if not len(fs):
            continue
        V = obj["V"][obj["F"][fs]].reshape(-1, 3)
        UV = obj["T"][obj["FT"][fs]].reshape(-1, 2).copy()
        UV[:, 1] = 1 - UV[:, 1]         # glTF's v runs down the image
        out.append({"V": V, "UV": UV, "I": np.arange(len(V)).reshape(-1, 3), "image": img})
    return out


def glb_geometry(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """The vertices and faces of a GLB's meshes (every triangle primitive, with its node's transform where it has a
    matrix), merged: a former mesh's shape, to texture again."""
    data = Path(path).read_bytes()
    n = struct.unpack_from("<I", data, 12)[0]
    g = json.loads(data[20:20 + n])
    bin_start = 20 + n + 8
    binary = data[bin_start:bin_start + struct.unpack_from("<I", data, 20 + n)[0]]
    comp = {5126: ("<f4", 4), 5125: ("<u4", 4), 5123: ("<u2", 2), 5121: ("<u1", 1)}
    width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}

    def read(i):
        a = g["accessors"][i]
        v = g["bufferViews"][a["bufferView"]]
        dt, size = comp[a["componentType"]]
        k = width[a["type"]]
        start = v.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = v.get("byteStride", size * k)
        if stride == size * k:
            return np.frombuffer(binary, dt, a["count"] * k, start).reshape(-1, k) if k > 1 else np.frombuffer(binary, dt, a["count"], start)
        rows = [np.frombuffer(binary, dt, k, start + j * stride) for j in range(a["count"])]
        return np.array(rows)
    Vs, Fs, base = [], [], 0
    mats = {}
    for node in g.get("nodes", []):
        if "mesh" in node:
            mats[node["mesh"]] = np.array(node.get("matrix", np.eye(4).T.ravel().tolist()), float).reshape(4, 4).T
    for mi, mesh in enumerate(g["meshes"]):
        M = mats.get(mi, np.eye(4))
        for prim in mesh["primitives"]:
            if prim.get("mode", 4) != 4:
                continue
            V = read(prim["attributes"]["POSITION"]).astype(np.float64)
            V = V @ M[:3, :3].T + M[:3, 3]
            I = read(prim["indices"]).astype(np.int64).reshape(-1, 3) if "indices" in prim else np.arange(len(V)).reshape(-1, 3)
            Vs.append(V)
            Fs.append(I + base)
            base += len(V)
    return np.vstack(Vs), np.vstack(Fs)


def write_ply(path: Path, V: np.ndarray, F: np.ndarray) -> None:
    """A binary PLY of a mesh, as OpenMVS reads one."""
    with open(path, "wb") as f:
        f.write((f"ply\nformat binary_little_endian 1.0\nelement vertex {len(V)}\nproperty float x\nproperty float y\n"
                 f"property float z\nelement face {len(F)}\nproperty list uchar int vertex_indices\nend_header\n").encode())
        f.write(np.asarray(V, "<f4").tobytes())
        rows = np.zeros(len(F), np.dtype([("n", "u1"), ("i", "<i4", 3)]))
        rows["n"], rows["i"] = 3, F
        f.write(rows.tobytes())


def split(obj_a: Path, obj_b: Path | None, out: Path, marker=MARKER) -> dict:
    """One GLB of A's faces where A coloured them and B's elsewhere; with the share of the area each textured."""
    A = read_obj(obj_a)
    have = covered(A, marker)
    area = areas(A["V"], A["F"])
    share = float(area[have].sum() / area.sum()) if area.sum() else 0.0
    parts = parts_of(A, np.flatnonzero(have))
    b_faces = 0
    if obj_b is not None and not have.all():
        B = read_obj(obj_b)
        if len(B["F"]) != len(A["F"]) or not np.array_equal(B["F"], A["F"]):
            raise ValueError("the two passes' meshes differ: they must texture the same faces in the same order")
        rest = np.flatnonzero(~have)
        parts += parts_of(B, rest)
        b_faces = len(rest)
    write_glb(out, parts)
    return {"faces": int(len(A["F"])), "capture_faces": int(have.sum()), "other_faces": b_faces,
            "capture_share": round(share, 4), "boundary_edges": boundary_edges(A["F"], have) if b_faces else 0,
            "seams_levelled": "within each pass; not across the boundary between them"}


if __name__ == "__main__":
    if sys.argv[1] == "ply":
        write_ply(Path(sys.argv[3]), *glb_geometry(Path(sys.argv[2])))
    elif sys.argv[1] == "split":
        print(json.dumps(split(Path(sys.argv[2]), None if sys.argv[3] == "-" else Path(sys.argv[3]), Path(sys.argv[4]))))
