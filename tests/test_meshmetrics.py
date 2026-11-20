#!/usr/bin/env python3
"""The Meshes tab's numbers about a mesh (backend/meshmetrics.py): where its photos were taken from, how many distinct
viewpoints those are, how far round the rock they go, and keeping them beside the mesh. Offline.

    python3 tests/test_meshmetrics.py
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import meshmetrics as mm  # noqa: E402


def camera(az_deg: float, el_deg: float = 10.0, dist: float = 5.0, target=(1.0, 2.0, 3.0)) -> dict:
    """COLMAP's (q, t) for a camera at that azimuth round the target (up is +Y), looking at it."""
    az, el = math.radians(az_deg), math.radians(el_deg)
    c = [target[0] + dist * math.cos(el) * math.cos(az), target[1] + dist * math.sin(el), target[2] + dist * math.cos(el) * math.sin(az)]
    z = mm._unit(mm._sub(list(target), c))                       # looks along +z
    x = mm._unit([z[2], 0.0, -z[0]])                               # level, to its side
    y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]]
    r = [x, y, z]                                                  # world to camera, by rows
    q = quaternion(r)
    t = [-sum(r[i][j] * c[j] for j in range(3)) for i in range(3)]
    return {"name": f"p{az_deg:.0f}.jpg", "q": q, "t": t}


def quaternion(r) -> list[float]:
    """(w, x, y, z) of a rotation matrix, by whichever of its terms is largest (Shepperd's method)."""
    tr = r[0][0] + r[1][1] + r[2][2]
    if tr > 0:
        s = 2 * math.sqrt(tr + 1)
        return [s / 4, (r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s]
    i = max(range(3), key=lambda k: r[k][k])
    j, k = (i + 1) % 3, (i + 2) % 3
    s = 2 * math.sqrt(1 + r[i][i] - r[j][j] - r[k][k])
    q = [0.0] * 4
    q[0] = (r[k][j] - r[j][k]) / s
    q[1 + i] = s / 4
    q[1 + j] = (r[j][i] + r[i][j]) / s
    q[1 + k] = (r[k][i] + r[i][k]) / s
    return q


tmp = Path(tempfile.mkdtemp(prefix="bouldermesh-metrics-"))

def write_glb(path: Path, P, I) -> None:
    """A bare GLB: positions and triangles, nothing else."""
    import struct

    import numpy as np
    blob, views = bytearray(), []
    for data in (np.asarray(P, "<f4"), np.asarray(I, "<u4")):
        blob += b"\0" * (-len(blob) % 4)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": data.nbytes})
        blob += data.tobytes()
    blob += b"\0" * (-len(blob) % 4)
    j = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}], "buffers": [{"byteLength": len(blob)}],
         "bufferViews": views,
         "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(P), "type": "VEC3", "min": np.min(P, 0).tolist(), "max": np.max(P, 0).tolist()},
                       {"bufferView": 1, "componentType": 5125, "count": int(np.size(I)), "type": "SCALAR"}],
         "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}]}
    text = json.dumps(j).encode()
    text += b" " * (-len(text) % 4)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(text) + 8 + len(blob)))
        f.write(struct.pack("<II", len(text), 0x4E4F534A) + text + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))


def shapes() -> None:
    """A photo on a bent card passes thickness() but not sheet_spread(); a rock, or one face of one with relief, passes
    both; shape() keeps them while the mesh is unchanged."""
    try:
        import numpy as np
    except ImportError:
        print("numpy not installed: skipping shapes")
        return
    n = 60
    x, y = np.meshgrid(np.linspace(-1, 1, n), np.linspace(-0.7, 0.7, n))
    tris = [(i * n + j, i * n + j + 1, (i + 1) * n + j) for i in range(n - 1) for j in range(n - 1)]
    rng = np.random.default_rng(1)
    made = {}
    for name, z in (("bent card", 0.15 * x * x + rng.normal(0, 0.001, x.shape)),
                    ("flat card", rng.normal(0, 0.001, x.shape)),
                    ("rock face", 0.12 * np.sin(5 * x) * np.cos(4 * y) + 0.1 * x * x),):
        d = tmp / name.replace(" ", "-")
        d.mkdir()
        write_glb(d / "mesh.glb", np.c_[x.ravel(), y.ravel(), z.ravel()], tris)
        made[name] = (mm.thickness(d / "mesh.glb"), mm.sheet_spread(d / "mesh.glb"), d)
    u, v = np.meshgrid(np.linspace(0, np.pi, 40), np.linspace(0, 2 * np.pi, 40))
    ball = np.c_[(np.sin(u) * np.cos(v)).ravel(), (0.8 * np.sin(u) * np.sin(v)).ravel(), (0.6 * np.cos(u)).ravel()]
    (tmp / "rock").mkdir()
    write_glb(tmp / "rock" / "mesh.glb", ball, [(0, 1, 2)])
    made["rock"] = (mm.thickness(tmp / "rock" / "mesh.glb"), mm.sheet_spread(tmp / "rock" / "mesh.glb"), tmp / "rock")
    flat, sheet, _ = made["bent card"]
    assert flat > mm.FLAT_BELOW and sheet < mm.SHEET_BELOW, ("passes the flat check, not the sheet's", made["bent card"])
    assert made["flat card"][0] < mm.FLAT_BELOW and made["flat card"][1] < mm.SHEET_BELOW, made["flat card"]
    for name in ("rock face", "rock"):
        assert made[name][0] > mm.FLAT_BELOW and made[name][1] > 3 * mm.SHEET_BELOW, (name, made[name])
    d = made["bent card"][2]
    got = mm.shape(d)
    assert got == {"flat": round(flat, 4), "sheet": round(sheet, 4), "sky": None}, got
    assert mm.shape(d, compute=False) == got and (d / mm.SHAPE_FILE).exists(), "kept"
    assert mm.shape(made["rock"][2], compute=False) is None, "nothing kept: not worked out unless asked"
    assert mm.thickness(tmp / "none.glb") is None and mm.sheet_spread(tmp / "none.glb") is None
    print("shapes OK")

try:
    ring = [camera(a) for a in range(0, 360, 30)]
    got = mm.cameras(ring)
    assert all(abs(f[1] + math.sin(math.radians(10))) < 1e-6 for _, f in got), "each looks at the rock, a little down"
    centre = mm.rock_centre(got)
    assert all(abs(a - b) < 1e-6 for a, b in zip(centre, (1, 2, 3))), centre
    dirs = [mm._unit(mm._sub(c, centre)) for c, _ in got]
    assert abs(mm.distinct_viewpoints(dirs) - 12) < 1e-9 and mm.around(dirs, [0, 1, 0]) == 330.0, "all round, 30° apart"
    clump = mm.cameras([camera(a) for a in (0, 2, 4, 6, 8, 10)] + [camera(90)])
    c2 = mm.rock_centre(clump)
    d2 = [mm._unit(mm._sub(c, c2)) for c, _ in clump]
    assert abs(mm.distinct_viewpoints(d2) - 2) < 1e-9, "six frames from one spot count once"
    assert abs(mm.around(d2, [0, 1, 0]) - 90) < 1e-6
    assert mm.around(d2[:1], [0, 1, 0]) == 0.0 and mm.rock_centre(clump[:1]) is None

    # a mesh's folder: worked out once, kept in metrics.json, and again when its cameras change
    d = tmp / "mesh"
    d.mkdir()
    (d / "cameras.json").write_text(json.dumps(ring))
    (d / "cleanup.json").write_text(json.dumps({"faces": 123456}))
    (d / "orientation.json").write_text(json.dumps({"method": "photos", "estimate": {"down": [0, -1, 0]}, "placed": 12}))
    (d / "timings.json").write_text(json.dumps({"photos_in": 20}))
    m = mm.load(d)
    assert m == {"faces": 123456, "placed": 12, "photos_in": 20, "views": 12.0, "around": 330.0}, m
    (d / "cleanup.json").write_text(json.dumps({"faces": 1}))
    kept = json.loads((d / mm.FILE).read_text())
    assert mm.load(d)["faces"] == 1 and json.loads((d / mm.FILE).read_text()) != kept, "worked out again"
    stamp = (d / mm.FILE).stat().st_mtime_ns
    time.sleep(0.01)
    assert mm.load(d)["faces"] == 1 and (d / mm.FILE).stat().st_mtime_ns == stamp, "kept, not worked out again"
    bare = tmp / "uploaded"
    bare.mkdir()
    assert mm.load(bare) == {"faces": None, "placed": None, "photos_in": None, "views": None, "around": None}
    old = tmp / "older"     # meshed before cameras.json: its photo_sources.json has every photo it was given
    old.mkdir()
    (old / "photo_sources.json").write_text(json.dumps({"basis": "given", "photos": [{"file": f"{i}.jpg"} for i in range(7)]}))
    assert mm.load(old)["photos_in"] == 7 and mm.load(old)["placed"] is None
    shapes()
    print("ALL MESH METRICS TESTS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
