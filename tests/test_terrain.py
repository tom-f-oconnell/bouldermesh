#!/usr/bin/env python3
"""The ground around a place (terrain.py), with stand-ins for USGS's and IGN's elevation services. Offline."""
from __future__ import annotations

import io
import math
import os
import pathlib
import struct
import sys
import tempfile
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import terrain  # noqa: E402

try:
    from PIL import Image
except ImportError:
    print("Pillow not installed: skipping")
    sys.exit(0)
os.environ.pop("TERRAIN_OFFLINE", None)

assert terrain.source_for(34.01, -116.16) == "usgs" and terrain.source_for(61.2, -149.9) == "usgs", "Joshua Tree, Alaska"
assert terrain.source_for(48.45, 2.63) == "ign" and terrain.source_for(-32.3, 19.0) is None, "Fontainebleau, Rocklands"


def slope(nx, ny, missing=0):
    """Heights rising 0.1 m a pixel eastward from 1000 m, row by row from the north-west, the first `missing` none."""
    return [-9999.0 if k < missing else 1000.0 + 0.1 * (k % nx) for k in range(nx * ny)]


asked = []


def usgs(url):
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    asked.append(q)
    nx, ny = (int(v) for v in q["size"].split(","))
    im = Image.new("F", (nx, ny))
    im.putdata(slope(nx, ny))
    out = io.BytesIO()
    im.save(out, "TIFF")
    return out.getvalue()


with tempfile.TemporaryDirectory() as td:
    cache = pathlib.Path(td)
    r = terrain.square(cache, 34.01419, -116.15821, 230, fetch=usgs)
    assert r and r["source"] == "usgs" and (r["nx"], r["width_m"]) == (300, 300.0) and len(asked) == 1, r
    west, south, east, north = (float(v) for v in asked[0]["bbox"].split(","))
    kx, ky = terrain.metres_per_degree(r["lat"])
    assert abs((east - west) * kx - 300) < 0.5 and abs((north - south) * ky - 300) < 0.5, "300 m each way"
    assert abs(r["lat"] / terrain.SNAP - round(r["lat"] / terrain.SNAP)) < 1e-6, "its middle snapped"
    heights = struct.unpack(f"<{300 * 300}f", r["path"].read_bytes())
    assert abs(heights[0] - 1000) < 1e-3 and abs(heights[299] - 1029.9) < 1e-3, heights[:2]
    p = terrain.placed(r, 34.01419, -116.15821)
    assert p["dx"] == p["dy"] == 1.0 and -150 - 30 < p["west_m"] < -150 + 30 and 150 - 30 < p["north_m"] < 150 + 30, p
    assert terrain.square(cache, 34.01421, -116.15819, 230, fetch=usgs)["key"] == r["key"] and len(asked) == 1, "a mesh 3 m away shares it, kept"
    big = terrain.square(cache, 34.01419, -116.15821, 2500, fetch=usgs)
    assert (big["nx"], big["width_m"]) == (500, 3000.0), "500 heights a side at most: 6 m apart"
    print("USGS OK")

    def ign(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        nx, ny = int(q["WIDTH"]), int(q["HEIGHT"])
        return struct.pack(f"<{nx * ny}f", *slope(nx, ny, missing=nx * ny // 10))
    r = terrain.square(cache, 48.4462, 2.6326, 230, fetch=ign)
    heights = struct.unpack(f"<{r['nx'] * r['ny']}f", r["path"].read_bytes())
    assert r["source"] == "ign" and min(heights) == 1000.0, "holes take the lowest height, not -9999"
    assert terrain.square(cache, 48.6, 2.6, 230, fetch=lambda url: struct.pack("<90000f", *([-99999.0] * 90000))) is None
    assert terrain.square(cache, 48.6, 2.6, 230, fetch=lambda url: 1 / 0) is None, "remembered as none, not asked again"
    try:
        terrain.square(cache, 48.7, 2.6, 230, fetch=lambda url: b"<ServiceException>busy</ServiceException>")
        raise AssertionError("a service's error page")
    except ValueError as e:
        assert "IGN answered" in str(e), e
    assert terrain.square(cache, -32.3, 19.0, 230, fetch=lambda url: 1 / 0) is None, "nowhere it has data: not asked"
    os.environ["TERRAIN_OFFLINE"] = "1"
    assert terrain.square(cache, 34.5, -116.5, 230, fetch=lambda url: 1 / 0) is None
    del os.environ["TERRAIN_OFFLINE"]
    print("IGN OK")
print("ALL TERRAIN TESTS PASSED")
