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
    print("ALL MESH METRICS TESTS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
