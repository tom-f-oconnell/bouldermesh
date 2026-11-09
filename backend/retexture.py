"""Colour a mesh again from only its permitted photos (policy.py), for when the site shows only what's
clearly free to copy. Its shape stays as it is, made from all its photos, as shape is fact.

  permitted/   mesh.glb, the same faces in the same order as the mesh's own, coloured per vertex rather
               than textured, with the mesh's cleanup (which fits, as the faces are the same), and
               info.json saying which photos were used, or why none could be

Each vertex takes its colour from the permitted photo that sees it most squarely and from nearest, among
those whose view of it isn't blocked (a coarse depth buffer of the mesh's vertices per photo, which also
hides its far side, as OpenMVS's normals may point in or out) and that don't have a person there (masks/). Vertices no permitted photo sees are grey. OpenMVS's
TextureMesh would give sharper results, but on a mesh most of which the permitted photos don't see, it
crashed, or (without seam levelling) didn't finish in 15 minutes. design.md has more.

Needs the mesh's cameras.json with each photo's lens (kept since 2026-11-03) and its project's photos.
"""
from __future__ import annotations

import json
import shutil
import struct
import time
from pathlib import Path
from typing import Callable

import beta
import meshclean
import policy
import provenance

Log = Callable[[str], None]
DIR, INFO = "permitted", "info.json"
GREY = 0.5            # in sRGB, for vertices no permitted photo sees
DEPTH_CELLS = 400     # the depth buffer's long side, in cells
DEPTH_SLACK = 0.02    # a vertex this far behind the nearest in its cell, as a share of its depth, is still seen


def usable(mesh_dir: Path, photos: Path, videos=None) -> tuple[list[dict], str | None]:
    """The permitted photos placed in a mesh, with their cameras, or why there aren't any to use.
    `videos` is provenance.load's: what's known of a frame's video beyond what the harvest recorded."""
    record, cams_file = provenance.load(mesh_dir, videos), mesh_dir / "cameras.json"
    if not record or not cams_file.exists():
        return [], "where its photos were placed wasn't kept: rebuild it"
    placed = {c["name"]: c for c in json.loads(cams_file.read_text())}
    if not any("lens" in c for c in placed.values()):
        return [], "its cameras' lenses weren't kept: rebuild it"
    ok = [placed[p["file"]] for p in record["photos"] if policy.permitted(p) and p["file"] in placed
          and "lens" in placed[p["file"]] and (photos / p["file"]).exists()]
    return ok, None if ok else "none of its placed photos is clearly free to copy"


def normals(V, F):
    import numpy as np
    n = np.zeros_like(V)
    fn = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])   # area-weighted
    for k in range(3):
        np.add.at(n, F[:, k], fn)
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)


def vertex_colours(V, F, cams: list[dict], photos: Path, masks: Path | None = None, log: Log = print):
    """sRGB colours (0-1) for the vertices V of faces F, from these cameras' photos, and which were seen."""
    import numpy as np
    from PIL import Image
    V, F = np.asarray(V, float), np.asarray(F)
    N = normals(V, F)
    TOP = 3     # the median of each vertex's best views, so that one view blocked by something not in the mesh
    #            (a crash pad, a spotter the masks missed) doesn't colour it
    colours = np.full((len(V), TOP, 3), np.nan)
    scores = np.zeros((len(V), TOP))
    for cam in cams:
        with Image.open(photos / cam["name"]) as im:
            img = np.asarray(im.convert("RGB"), float) / 255
        h, w = img.shape[:2]
        f, cx, cy, k, _ = beta.lens(cam, (w, h))
        R, t = beta._rotation(cam["q"]), np.asarray(cam["t"], float)
        P = V @ R.T + t
        z = P[:, 2]
        front = z > 1e-9
        safe = np.where(front, z, 1.0)
        x, y = np.where(front, P[:, 0] / safe, 0.0), np.where(front, P[:, 1] / safe, 0.0)
        r = 1 + k * (x * x + y * y)
        u, v = f * x * r + cx, f * y * r + cy
        inside = front & (u >= 0) & (u < w - 1) & (v >= 0) & (v < h - 1)
        to_cam = (-R.T @ t) - V
        dist = np.linalg.norm(to_cam, axis=1)
        facing = np.abs(np.einsum("ij,ij->i", N, to_cam)) / np.maximum(dist, 1e-12)   # OpenMVS's normals may point either way
        cell = DEPTH_CELLS / max(w, h)     # the nearest vertex in each cell is what the photo sees there
        cu, cv = np.where(inside, u * cell, 0).astype(int), np.where(inside, v * cell, 0).astype(int)
        nearest = np.full((int(h * cell) + 2, int(w * cell) + 2), np.inf)
        np.minimum.at(nearest, (cv[inside], cu[inside]), z[inside])
        seen = inside & (facing > 0.05)
        seen[seen] &= z[seen] <= nearest[cv[seen], cu[seen]] * (1 + DEPTH_SLACK)
        mask = masks / f"{cam['name']}.png" if masks else None
        if mask and mask.exists():     # black where people are
            with Image.open(mask) as m:
                person = np.asarray(m.convert("L").resize((w, h))) < 128
            seen[seen] &= ~person[v[seen].astype(int), u[seen].astype(int)]
        score = np.where(seen, facing / np.maximum(dist, 1e-12), 0.0)
        worst = scores.argmin(axis=1)
        better = seen & (score > scores[np.arange(len(V)), worst])
        idx = np.nonzero(better)[0]
        colours[idx, worst[idx]] = img[v[idx].round().astype(int), u[idx].round().astype(int)]
        scores[idx, worst[idx]] = score[idx]
    seen_any = scores.max(axis=1) > 0
    colour = np.full((len(V), 3), GREY)
    colour[seen_any] = np.nanmedian(colours[seen_any], axis=1)
    log(f"retexture: {int(seen_any.sum())} of {len(V)} vertices seen by a permitted photo")
    return colour, seen_any


def write_coloured(src: Path, dst: Path, colour_of) -> None:
    """A copy of a GLB with the same meshes, primitives and faces, coloured per vertex: colour_of(i, positions)
    gives primitive i's vertices' sRGB colours. No images or texture coordinates come with it."""
    import numpy as np
    j, bufs = meshclean._read_glb(src)
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

    new = {k: j[k] for k in ("asset", "scene", "scenes", "nodes") if k in j}
    new["materials"] = [{"name": "permitted-photos", "doubleSided": True,
                         "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0.0, "roughnessFactor": 0.9}}]
    new["meshes"] = []
    i = 0
    for m in j.get("meshes", []):
        prims = []
        for p in m["primitives"]:
            P = np.ascontiguousarray(meshclean._accessor(j, bufs, p["attributes"]["POSITION"]), "<f4")
            srgb = np.clip(colour_of(i, P), 0, 1)
            linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)   # glTF colours are linear
            out = {"attributes": {"POSITION": accessor(P, "VEC3", 5126, 34962, bounds=True),
                                  "COLOR_0": accessor(np.ascontiguousarray(linear, "<f4"), "VEC3", 5126, 34962)},
                   "material": 0, "mode": p.get("mode", 4)}
            if "indices" in p:
                out["indices"] = accessor(np.ascontiguousarray(meshclean._accessor(j, bufs, p["indices"]), "<u4"), "SCALAR", 5125, 34963)
            prims.append(out)
            i += 1
        new["meshes"].append({"primitives": prims})
    blob.extend(b"\0" * (-len(blob) % 4))
    new["bufferViews"], new["accessors"], new["buffers"] = views, accessors, [{"byteLength": len(blob)}]
    text = json.dumps(new).encode()
    text += b" " * (-len(text) % 4)
    with open(dst, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(text) + 8 + len(blob)))
        f.write(struct.pack("<II", len(text), 0x4E4F534A) + text)
        f.write(struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))


def retexture(mesh_dir: Path, photos: Path, log: Log, videos=None) -> dict:
    """Make permitted/ for a mesh, replacing any from before once the new one is finished, so that one
    stopped partway (as by a restart) leaves the old one.

    Returns:
        info.json's contents: the photos used and the share of the mesh they saw, or why there are none.
    """
    import numpy as np
    out, new = mesh_dir / DIR, mesh_dir / (DIR + ".partial")
    cams, why = usable(mesh_dir, photos, videos)
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    if why:
        info = {"photos": [], "note": why, "created": time.time()}
    else:
        log(f"retexture: from {len(cams)} permitted photo(s)")
        src = meshclean.mesh_file(mesh_dir)
        j, bufs = meshclean._read_glb(src)
        V, F, counts = meshclean._geometry(j, bufs)
        welded, inverse = np.unique(np.asarray(V, float), axis=0, return_inverse=True)   # the GLB splits vertices at texture seams
        inverse = inverse.ravel()
        colour, seen = vertex_colours(welded, inverse[F], cams, photos, photos.parent / "masks", log)
        starts = np.cumsum([0] + [len(meshclean._accessor(j, bufs, p["attributes"]["POSITION"])) for _, _, p in meshclean._primitives(j)])
        write_coloured(src, new / "mesh.glb", lambda i, P: colour[inverse[starts[i]:starts[i] + len(P)]])
        for name in ("cleanup.json", "cleanup_faces.bin"):     # the same faces, so the mesh's own cleanup fits
            if (mesh_dir / name).exists():
                shutil.copy(mesh_dir / name, new / name)
        info = {"photos": [c["name"] for c in cams], "seen": round(float(seen.mean()), 3), "created": time.time()}
    (new / INFO).write_text(json.dumps(info))
    old = mesh_dir / (DIR + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out.exists():
        out.rename(old)
    new.rename(out)
    shutil.rmtree(old, ignore_errors=True)
    return info


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((mesh_dir / DIR / INFO).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def has_variant(mesh_dir: Path) -> bool:
    return (mesh_dir / DIR / "mesh.glb").exists()
