"""The ground around a place, from free elevation data made from lidar, for the viewer's Nearby: the terrain under and
around the meshes there, and the height each stands at.

  USGS 3DEP, in the US: 1 m where its lidar reaches (Joshua Tree, the Buttermilks, Hueco Tanks, Red Rocks and Yosemite
  all have it), else 1/3 arc-second (about 10 m), from its ImageServer; public domain
  IGN's RGE ALTI, in France: 1 m, from the Géoplateforme's WMS; Etalab's open licence
Elsewhere none. Both are bare earth: what lidar took for vegetation and buildings is gone, and big boulders are
sometimes kept as ground, sometimes not. Each square fetched is kept in data/cache/terrain, as the ground doesn't change:

  <key>.bin    its heights in metres, row by row from its north-west corner, as little-endian float32
  <key>.json   {source, lat, lng (its middle), width_m, height_m, nx, ny, made}

TERRAIN_OFFLINE=1 fetches nothing (for tests).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

USGS = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
IGN = "https://data.geopf.fr/wms-r"
NAMES = {"usgs": "USGS 3DEP lidar elevation", "ign": "IGN RGE ALTI elevation"}
SIDES = (150, 300, 500, 800, 1200, 2000, 3000)   # metres across a square may be, so that squares are shared
MOST_PIXELS = 500        # along a side: 1 m apart up to 500 m across, further apart beyond
SNAP = 0.0005            # degrees its middle is snapped to (about 50 m), so that meshes near each other share one
NO_DATA = -1000.0        # heights below this are the services' "none here"
AGENT = "BoulderMesh (elevation for showing meshes in place)"
_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def source_for(lat: float, lng: float) -> str | None:
    """Which service has elevation here: "usgs" in the US (its 50 states' rough boxes), "ign" in mainland France and
    Corsica, else None."""
    us = (24.0 <= lat <= 50.0 and -125.0 <= lng <= -66.0) or (51.0 <= lat <= 72.0 and -170.0 <= lng <= -129.0) \
        or (18.5 <= lat <= 22.5 and -161.0 <= lng <= -154.0)
    if us:
        return "usgs"
    if 41.0 <= lat <= 51.5 and -5.5 <= lng <= 10.0:
        return "ign"
    return None


def metres_per_degree(lat: float) -> tuple[float, float]:
    """Metres a degree of longitude and of latitude are, here."""
    return 111320.0 * math.cos(math.radians(lat)), 110574.0


def _url(source: str, south: float, west: float, north: float, east: float, nx: int, ny: int) -> str:
    if source == "usgs":
        return USGS + "?" + urllib.parse.urlencode({
            "bbox": f"{west},{south},{east},{north}", "bboxSR": 4326, "imageSR": 4326, "size": f"{nx},{ny}", "format": "tiff",
            "pixelType": "F32", "noData": -9999, "interpolation": "RSP_BilinearInterpolation", "f": "image"})
    return IGN + "?" + urllib.parse.urlencode({
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES",
        "STYLES": "", "CRS": "EPSG:4326", "BBOX": f"{south},{west},{north},{east}", "WIDTH": nx, "HEIGHT": ny,
        "FORMAT": "image/x-bil;bits=32"})


def _fetch(url: str, tries: int = 3) -> bytes:
    """A service's answer, asked again after a moment if it was busy (USGS's sometimes answers 502)."""
    import urllib.error
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": AGENT}), timeout=90) as r:
                return r.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code < 500):
                raise
            time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


def _heights(source: str, data: bytes, nx: int, ny: int) -> list[float]:
    """The heights a service answered with, row by row from the north-west."""
    if source == "ign":
        if len(data) != nx * ny * 4:
            raise ValueError(f"IGN answered {len(data)} bytes, not {nx * ny * 4}: {data[:200]!r}")
        return list(struct.unpack(f"<{nx * ny}f", data))
    import io
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.size != (nx, ny):
                raise ValueError(f"USGS answered an image {im.size}, not {(nx, ny)}")
            return list(im.convert("F").getdata())
    except OSError as e:
        raise ValueError(f"USGS didn't answer with an image ({e}): {data[:200]!r}") from None


def square(cache: Path, lat: float, lng: float, side_m: float, fetch: Callable[[str], bytes] = _fetch) -> dict | None:
    """The heights of a square at least side_m across around (lat, lng), its middle snapped to SNAP: {source, lat, lng,
    width_m, height_m, nx, ny, made, key, path}, the heights in path as <key>.bin says. None where there's no
    elevation data, or none for this square (the service's "none here" over most of it)."""
    source = None if os.environ.get("TERRAIN_OFFLINE") == "1" else source_for(lat, lng)
    if source is None:
        return None
    side = next((s for s in SIDES if s >= side_m), SIDES[-1])
    lat, lng = round(lat / SNAP) * SNAP, round(lng / SNAP) * SNAP
    n = min(MOST_PIXELS, int(side))
    key = hashlib.sha1(f"{source}|{lat:.4f}|{lng:.4f}|{side}|{n}".encode()).hexdigest()[:16]
    meta, path = cache / f"{key}.json", cache / f"{key}.bin"
    with _locks_lock:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        if meta.exists():
            record = json.loads(meta.read_text())
            return {**record, "key": key, "path": path} if record.get("nx") else None
        kx, ky = metres_per_degree(lat)
        south, north, west, east = lat - side / 2 / ky, lat + side / 2 / ky, lng - side / 2 / kx, lng + side / 2 / kx
        heights = _heights(source, fetch(_url(source, south, west, north, east, n, n)), n, n)
        good = [h for h in heights if h > NO_DATA and math.isfinite(h)]
        cache.mkdir(parents=True, exist_ok=True)
        record = {"source": source, "lat": lat, "lng": lng, "width_m": float(side), "height_m": float(side), "nx": n, "ny": n,
                  "made": time.time()}
        if len(good) < 0.5 * len(heights):
            meta.write_text(json.dumps({**record, "nx": 0, "ny": 0, "why": "no data here"}))
            return None
        low = min(good)     # a few holes take the lowest around, rather than a cliff to "none here"
        tmp = path.with_suffix(".partial")
        tmp.write_bytes(struct.pack(f"<{len(heights)}f", *(h if h > NO_DATA and math.isfinite(h) else low for h in heights)))
        tmp.replace(path)
        meta.write_text(json.dumps(record))
        return {**record, "key": key, "path": path}


def placed(record: dict, lat: float, lng: float) -> dict:
    """A square as the viewer places it around a point (lat, lng): its north-west corner's offsets east and north of
    the point, in metres, and the distance between neighbouring heights each way."""
    kx, ky = metres_per_degree(lat)
    return {"source": record["source"], "name": NAMES[record["source"]], "nx": record["nx"], "ny": record["ny"],
            "west_m": round((record["lng"] - lng) * kx - record["width_m"] / 2, 3),
            "north_m": round((record["lat"] - lat) * ky + record["height_m"] / 2, 3),
            "dx": record["width_m"] / record["nx"], "dy": record["height_m"] / record["ny"]}
