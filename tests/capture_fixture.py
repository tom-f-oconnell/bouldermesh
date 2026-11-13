#!/usr/bin/env python3
"""A made-up rock for the phone apps' tests: a wall, and cameras as a reconstruction would have placed them.

Run as a script, it makes a data folder holding that rock as a mesh with a climb and two holds, for a server the apps
are tried against (.github/workflows/apps.yml):

    python3 tests/capture_fixture.py <data folder>
    DATA_DIR=<data folder> PORT=<port> python3 backend/server.py
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
MESH = "ca" * 6


def quat(R) -> list[float]:
    """A rotation matrix as w, x, y, z."""
    w = math.sqrt(max(0.0, 1 + R[0, 0] + R[1, 1] + R[2, 2])) / 2
    if w > 1e-6:
        return [w, (R[2, 1] - R[1, 2]) / (4 * w), (R[0, 2] - R[2, 0]) / (4 * w), (R[1, 0] - R[0, 1]) / (4 * w)]
    i = int(np.argmax(np.diag(R)))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(max(1e-12, 1 + R[i, i] - R[j, j] - R[k, k])) * 2
    q = [0.0, 0.0, 0.0, 0.0]
    q[0], q[1 + i], q[1 + j], q[1 + k] = (R[k, j] - R[j, k]) / s, s / 4, (R[j, i] + R[i, j]) / s, (R[k, i] + R[i, k]) / s
    return q


def camera(name: str, centre, target, f: float = 3000.0, size=(4000, 3000)) -> dict:
    """A photo placed at centre, looking at target, upright: as cameras.json has them (world to camera, +Z the way it looks)."""
    c, fwd = np.asarray(centre, float), np.asarray(target, float) - np.asarray(centre, float)
    fwd = fwd / np.linalg.norm(fwd)
    right = np.cross(fwd, [0.0, 1.0, 0.0])
    right = right / np.linalg.norm(right)
    R = np.array([right, np.cross(fwd, right), fwd])
    return {"name": name, "q": quat(R), "t": list(-R @ c), "lens": {"model": 2, "width": size[0], "height": size[1],
                                                                  "params": [f, size[0] / 2, size[1] / 2, 0.0]}}


def wall(A: int = 25, H: int = 16):
    """A wall of rock: part of a cylinder 2 units in radius and 3 tall, facing +Z, open at its sides and top.

    Returns:
        Its vertices and faces, as arrays.
    """
    angles, heights = np.linspace(-1.0, 1.0, A), np.linspace(0.0, 3.0, H)
    V = np.array([[2 * math.sin(a), y, 2 * math.cos(a)] for a in angles for y in heights])
    F = []
    for i in range(A - 1):
        for j in range(H - 1):
            a, b, c, d = i * H + j, (i + 1) * H + j, (i + 1) * H + j + 1, i * H + j + 1
            F += [[a, b, c], [a, c, d]]
    return V, np.array(F)


def cameras() -> list[dict]:
    """Nine photos of it from 6 units off, in an arc before it."""
    return [camera(f"cap_{i:03d}.jpg", [6 * math.sin(a), 1.5, 6 * math.cos(a)], [0, 1.5, 0]) for i, a in enumerate(np.linspace(-0.5, 0.5, 9))]


def make(data: pathlib.Path) -> str:
    """A data folder with the wall as a mesh, sized, with a climb on it and two holds. Returns the mesh's id."""
    os.environ["DATA_DIR"] = str(data)
    sys.path.insert(0, str(ROOT / "backend"))
    import pipeline
    import scale
    import server
    server.init_db()
    V, F = wall()
    mdir = server.MESHES / MESH
    mdir.mkdir(parents=True)
    pipeline.write_ply(mdir / "mesh.ply", [tuple(v) for v in V], [(120, 110, 100)] * len(V), [tuple(int(i) for i in f) for f in F])
    (mdir / "cameras.json").write_text(json.dumps(cameras()))
    (mdir / "holds.json").write_text(json.dumps({"holds": [{"p": [0.0, 1.0, 2.0], "role": "start"}, {"p": [0.5, 2.2, 1.936], "role": "finish"}]}))
    scale.save(mdir, {"status": "done", "metres_per_unit": 1.2})
    server.q("INSERT INTO meshes(id,name,filename,source,created,lat,lng) VALUES(?,?,?,?,?,?,?)", MESH, "A Wall", "mesh.ply", "project:x", 1.0,
             34.0, -116.0, write=True)
    server.q("INSERT INTO climbs(id,source,source_id,name,grade,created) VALUES('c1','test','1','Alpha','V2',1.0)", write=True)
    server.link_mesh(MESH, "c1", None)
    return MESH


if __name__ == "__main__":
    print(make(pathlib.Path(sys.argv[1]).resolve()))
