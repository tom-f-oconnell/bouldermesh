"""Hiding the stray pieces of a mesh, as a step that can be edited or deleted.

Reconstructions come with small fragments around the rock, and sheets of giant triangles that are
often distant scenery. A mesh's own files are never changed: this finds its connected pieces, decides
which to hide, and keeps that beside it, for the viewer to apply and for a cleaned download to be
made on request.

  cleanup.json        the pieces, which are hidden, and the rule used
  cleanup_faces.bin   the piece of every face, in the order of the GLB's primitives
  mesh.cleaned.glb    made when a cleaned download is asked for

design.md has the measurements behind the rule.
"""
from __future__ import annotations

import base64
import copy
import importlib.util
import json
import struct
import time
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
MESH_FILE = "mesh.glb"
JSON_FILE = "cleanup.json"
FACES_FILE = "cleanup_faces.bin"
CLEANED_FILE = "mesh.cleaned.glb"
MIN_SHARE = 0.01     # hide pieces with fewer faces than this share of the main piece's
MAX_SPARSER = 5.0    # hide pieces with this many times fewer faces per area than the main piece
_DTYPES = {5126: "<f4", 5125: "<u4", 5123: "<u2", 5121: "u1"}
_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def available() -> bool:
    return importlib.util.find_spec("numpy") is not None


def mesh_file(mesh_dir: Path) -> Path | None:
    """A mesh's GLB: mesh.glb for a reconstruction, or the only one there under whatever name it was uploaded."""
    if (mesh_dir / MESH_FILE).exists():
        return mesh_dir / MESH_FILE
    found = [p for p in mesh_dir.glob("*.glb") if p.name != CLEANED_FILE]
    return found[0] if len(found) == 1 else None


def _read_glb(path: Path) -> tuple[dict, list[bytes]]:
    """A GLB's JSON and its buffers: the BIN chunk, or for split textures also embedded data URIs."""
    with open(path, "rb") as f:
        magic, _, _ = struct.unpack("<III", f.read(12))
        if magic != 0x46546C67:
            raise ValueError("not a GLB file")
        n, _ = struct.unpack("<II", f.read(8))
        j = json.loads(f.read(n))
        head = f.read(8)
        bin_chunk = f.read(struct.unpack("<II", head)[0]) if len(head) == 8 else b""
    bufs = []
    for b in j.get("buffers", []):
        uri = b.get("uri")
        if uri and not uri.startswith("data:"):
            raise ValueError("buffers in separate files aren't supported")
        bufs.append(base64.b64decode(uri.split(",", 1)[1]) if uri else bin_chunk)
    return j, bufs


def _accessor(j: dict, bufs: list[bytes], i: int):
    import numpy as np
    a = j["accessors"][i]
    if "bufferView" not in a or a["componentType"] not in _DTYPES:
        raise ValueError("compressed or unusual geometry isn't supported")
    v = j["bufferViews"][a["bufferView"]]
    dtype = np.dtype(_DTYPES[a["componentType"]])
    c = _COMPONENTS[a["type"]]
    if v.get("byteStride", dtype.itemsize * c) != dtype.itemsize * c:
        raise ValueError("interleaved geometry isn't supported")
    data = np.frombuffer(bufs[v.get("buffer", 0)], dtype, a["count"] * c, v.get("byteOffset", 0) + a.get("byteOffset", 0))
    return data.reshape(-1, c) if c > 1 else data


def _primitives(j: dict) -> list[tuple[int, int, dict]]:
    return [(mi, pi, p) for mi, m in enumerate(j.get("meshes", [])) for pi, p in enumerate(m["primitives"])]


def _geometry(j: dict, bufs: list[bytes]):
    """All primitives together: positions, triangles, and each primitive's face count."""
    import numpy as np
    Ps, Is, counts, offset = [], [], [], 0
    for mi, pi, p in _primitives(j):
        if p.get("mode", 4) != 4 or "indices" not in p:
            raise ValueError("only indexed triangles are supported")
        P = _accessor(j, bufs, p["attributes"]["POSITION"])
        I = _accessor(j, bufs, p["indices"]).reshape(-1, 3).astype(np.int64)
        Ps.append(P)
        Is.append(I + offset)
        counts.append({"mesh": mi, "primitive": pi, "faces": len(I)})
        offset += len(P)
    if not Ps:
        raise ValueError("no geometry")
    return np.vstack(Ps), np.vstack(Is), counts


def _components(F, nv: int):
    """The connected component of each of nv vertices, joined by the triangles F: its lowest vertex."""
    import numpy as np
    parent = np.arange(nv)
    a, b = np.r_[F[:, 0], F[:, 1]], np.r_[F[:, 1], F[:, 2]]
    while True:
        pa, pb = parent[a], parent[b]
        differ = pa != pb
        if not differ.any():
            return parent
        np.minimum.at(parent, np.maximum(pa, pb)[differ], np.minimum(pa, pb)[differ])
        while True:
            jumped = parent[parent]
            if (jumped == parent).all():
                break
            parent = jumped
        a, b = a[differ], b[differ]     # edges already within one component stay that way


def analyse(path: Path):
    """The pieces of a GLB, most faces first, and the piece of each of its faces.

    Vertices are split along texture seams and between textures, so they're welded first, or a single
    surface would count as hundreds of pieces.

    Returns:
        (pieces, labels, primitive face counts).
    """
    import numpy as np
    j, bufs = _read_glb(path)
    P, I, counts = _geometry(j, bufs)
    _, weld = np.unique(P, axis=0, return_inverse=True)
    weld = weld.ravel()
    F = weld[I]
    roots = _components(F, int(weld.max()) + 1)[F[:, 0]]
    _, raw, faces = np.unique(roots, return_inverse=True, return_counts=True)
    raw = raw.ravel()
    order = np.argsort(-faces, kind="stable")
    rank = np.empty(len(order), dtype=np.int64)
    rank[order] = np.arange(len(order))
    labels = rank[raw]
    faces = faces[order]

    Q = P.astype(np.float64)
    area = np.linalg.norm(np.cross(Q[I[:, 1]] - Q[I[:, 0]], Q[I[:, 2]] - Q[I[:, 0]]), axis=1) / 2
    areas = np.bincount(labels, weights=area, minlength=len(faces))
    lo = np.full((len(faces), 3), np.inf)
    hi = np.full((len(faces), 3), -np.inf)
    for corner in range(3):
        np.minimum.at(lo, labels, Q[I[:, corner]])
        np.maximum.at(hi, labels, Q[I[:, corner]])
    main_density = faces[0] / max(float(areas[0]), 1e-12)
    pieces = [{"id": i, "faces": int(faces[i]), "area": round(float(areas[i]), 6),
               "size": [round(float(x), 4) for x in hi[i] - lo[i]],
               "center": [round(float(x), 4) for x in (hi[i] + lo[i]) / 2],
               "density": round(float(faces[i] / max(float(areas[i]), 1e-12) / main_density), 4)}
              for i in range(len(faces))]
    return pieces, labels, counts


def automatic(pieces: list[dict]) -> list[int]:
    """Which pieces the rule hides. Judged by faces, not surface: a sheet of giant triangles can have
    most of a mesh's surface and hardly any of its faces."""
    main = pieces[0]["faces"]
    return [p["id"] for p in pieces[1:] if p["faces"] < MIN_SHARE * main or p["density"] < 1 / MAX_SPARSER]


def _write_json(mesh_dir: Path, record: dict) -> None:
    tmp = mesh_dir / (JSON_FILE + ".partial")
    tmp.write_text(json.dumps(record))
    tmp.replace(mesh_dir / JSON_FILE)
    (mesh_dir / CLEANED_FILE).unlink(missing_ok=True)


def load(mesh_dir: Path) -> dict | None:
    """A mesh's cleanup, {"deleted": True} if it was deleted, or None if it never had one."""
    try:
        return json.loads((mesh_dir / JSON_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clean(mesh_dir: Path, log: Log = print, force: bool = False) -> dict | None:
    """Work out a mesh's cleanup, unless it has one or had one deleted.

    Args:
        force: start again from the rule, losing any edits, or bring back a deleted cleanup.

    Returns:
        The cleanup, or None if there's no GLB here to work on, as with an untextured PLY.
    """
    import numpy as np
    if not force:
        existing = load(mesh_dir)
        if existing:
            return existing
    path = mesh_file(mesh_dir)
    if not path:
        return None
    pieces, labels, counts = analyse(path)
    dtype = "<u2" if len(pieces) <= 65536 else "<u4"
    tmp = mesh_dir / (FACES_FILE + ".partial")
    tmp.write_bytes(labels.astype(dtype).tobytes())
    tmp.replace(mesh_dir / FACES_FILE)
    removed = automatic(pieces)
    record = {"created": time.time(), "rule": {"min_share": MIN_SHARE, "max_sparser": MAX_SPARSER}, "label_bytes": np.dtype(dtype).itemsize,
              "faces": int(len(labels)), "primitives": counts, "pieces": pieces, "automatic": removed, "removed": removed}
    _write_json(mesh_dir, record)
    s = summary(record)
    log(f"cleanup: hiding {s['hidden']} of {s['pieces']} pieces, {100 * s['hidden_faces'] / max(1, s['faces']):.2f}% of the faces; the mesh itself is unchanged")
    return record


def summary(record: dict | None) -> dict | None:
    """What the mesh list needs to know."""
    if not record:
        return None
    if record.get("deleted"):
        return {"deleted": True}
    hidden = set(record["removed"])
    return {"pieces": len(record["pieces"]), "hidden": len(hidden), "faces": record["faces"], "edited": set(record["automatic"]) != hidden,
            "hidden_faces": sum(p["faces"] for p in record["pieces"] if p["id"] in hidden)}


def set_removed(mesh_dir: Path, removed: list[int]) -> dict:
    """Choose which pieces are hidden."""
    record = load(mesh_dir)
    if not record or record.get("deleted"):
        raise ValueError("this mesh has no cleanup to edit")
    ids = {p["id"] for p in record["pieces"]}
    wanted = sorted({int(x) for x in removed})
    if not set(wanted) <= ids:
        raise ValueError("no such piece")
    if len(wanted) == len(ids):
        raise ValueError("at least one piece has to stay")
    record["removed"] = wanted
    _write_json(mesh_dir, record)
    return record


def delete(mesh_dir: Path) -> None:
    """Remove the cleanup, which shows the whole mesh again, and remember that so it isn't redone."""
    (mesh_dir / FACES_FILE).unlink(missing_ok=True)
    _write_json(mesh_dir, {"deleted": True, "created": time.time()})


def strip_textures(src: Path, dst: Path) -> Path:
    """A copy of a GLB with only its shape: the same meshes, primitives and faces in the same order,
    so its cleanup still fits, with one plain material, and no images or texture coordinates, so
    nothing of the photos it was textured from. Made again only if src is newer."""
    import numpy as np
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    j, bufs = _read_glb(src)
    blob, views, accessors = bytearray(), [], []

    def accessor(values, kind: str, component: int, target: int, bounds: bool = False) -> int:
        blob.extend(b"\0" * (-len(blob) % 4))
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": values.nbytes, "target": target})
        blob.extend(values.tobytes())
        a = {"bufferView": len(views) - 1, "componentType": component, "count": len(values), "type": kind}
        if bounds:
            a["min"], a["max"] = [float(x) for x in values.min(0)], [float(x) for x in values.max(0)]
        accessors.append(a)
        return len(accessors) - 1

    new = {k: copy.deepcopy(j[k]) for k in ("asset", "scene", "scenes", "nodes") if k in j}
    new["materials"] = [{"name": "untextured", "doubleSided": True,
                         "pbrMetallicRoughness": {"baseColorFactor": [0.72, 0.75, 0.8, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.9}}]
    new["meshes"] = []
    for m in j.get("meshes", []):
        prims = []
        for p in m["primitives"]:
            out = {"attributes": {}, "material": 0, "mode": p.get("mode", 4)}
            for name in ("POSITION", "NORMAL"):
                if name in p["attributes"]:
                    values = np.ascontiguousarray(_accessor(j, bufs, p["attributes"][name]), "<f4")
                    out["attributes"][name] = accessor(values, "VEC3", 5126, 34962, bounds=name == "POSITION")
            if "indices" in p:
                out["indices"] = accessor(np.ascontiguousarray(_accessor(j, bufs, p["indices"]), "<u4"), "SCALAR", 5125, 34963)
            prims.append(out)
        new["meshes"].append({"primitives": prims})
    blob.extend(b"\0" * (-len(blob) % 4))
    new["bufferViews"], new["accessors"], new["buffers"] = views, accessors, [{"byteLength": len(blob)}]
    text = json.dumps(new).encode()
    text += b" " * (-len(text) % 4)
    tmp = dst.with_suffix(".partial")
    with open(tmp, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(text) + 8 + len(blob)))
        f.write(struct.pack("<II", len(text), 0x4E4F534A) + text)
        f.write(struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))
    tmp.replace(dst)
    return dst


def write_cleaned(mesh_dir: Path) -> Path | None:
    """A GLB without the hidden pieces, using the same texture files; None if nothing is hidden."""
    import numpy as np
    record = load(mesh_dir)
    if not record or record.get("deleted") or not record["removed"]:
        return None
    out = mesh_dir / CLEANED_FILE
    if out.exists():    # any change to the cleanup deletes it
        return out
    j, bufs = _read_glb(mesh_file(mesh_dir))
    if j.get("skins") or j.get("animations"):
        raise ValueError("animated meshes aren't supported")
    labels = np.frombuffer((mesh_dir / FACES_FILE).read_bytes(), {2: "<u2", 4: "<u4"}[record["label_bytes"]])
    keep_piece = np.ones(len(record["pieces"]), bool)
    keep_piece[record["removed"]] = False
    keep = keep_piece[labels]

    new = copy.deepcopy(j)
    blob = bytearray()
    views, accessors = [], []

    def add(data: bytes, target: int | None = None) -> int:
        blob.extend(b"\0" * (-len(blob) % 4))
        view = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target:
            view["target"] = target
        blob.extend(data)
        views.append(view)
        return len(views) - 1

    def add_accessor(values, kind: str, component: int, target: int, bounds: bool = False) -> int:
        a = {"bufferView": add(values.tobytes(), target), "componentType": component, "count": len(values), "type": kind}
        if bounds:
            a["min"], a["max"] = [float(x) for x in values.min(0)], [float(x) for x in values.max(0)]
        accessors.append(a)
        return len(accessors) - 1

    start = 0
    for mi, mesh in enumerate(new["meshes"]):
        kept = []
        for pi, p in enumerate(mesh["primitives"]):
            I = _accessor(j, bufs, j["meshes"][mi]["primitives"][pi]["indices"]).reshape(-1, 3)
            mine = keep[start:start + len(I)]
            start += len(I)
            if not mine.any():
                continue
            used, remapped = np.unique(I[mine].ravel(), return_inverse=True)
            for name, i in list(p["attributes"].items()):
                old = j["accessors"][i]
                values = _accessor(j, bufs, i)[used]
                p["attributes"][name] = add_accessor(values, old["type"], old["componentType"], 34962, bounds=name == "POSITION")
            p["indices"] = add_accessor(remapped.ravel().astype("<u4"), "SCALAR", 5125, 34963)
            kept.append(p)
        mesh["primitives"] = kept
    used_meshes = {}
    meshes = []
    for mi, mesh in enumerate(new["meshes"]):
        if mesh["primitives"]:
            used_meshes[mi] = len(meshes)
            meshes.append(mesh)
    new["meshes"] = meshes
    for node in new.get("nodes", []):
        if "mesh" in node:
            if node["mesh"] in used_meshes:
                node["mesh"] = used_meshes[node["mesh"]]
            else:
                del node["mesh"]
    for image, old in zip(new.get("images", []), j.get("images", [])):   # images inside the file come too
        if "bufferView" in old:
            v = j["bufferViews"][old["bufferView"]]
            start = v.get("byteOffset", 0)
            image["bufferView"] = add(bufs[v.get("buffer", 0)][start:start + v["byteLength"]])
    new["bufferViews"], new["accessors"] = views, accessors
    blob.extend(b"\0" * (-len(blob) % 4))
    new["buffers"] = [{"byteLength": len(blob)}]
    text = json.dumps(new).encode()
    text += b" " * (-len(text) % 4)
    tmp = mesh_dir / (CLEANED_FILE + ".partial")
    with open(tmp, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(text) + 8 + len(blob)))
        f.write(struct.pack("<II", len(text), 0x4E4F534A) + text)
        f.write(struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))
    tmp.replace(out)
    return out
