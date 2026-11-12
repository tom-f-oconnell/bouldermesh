"""Which way a mesh faces: the compass bearing its own -Z axis points once it stands upright (+Y up, as the viewer
shows it), for placing meshes around each other (the viewer's Nearby). From the best there is of:

  edited    turned by hand in the viewer and saved
  skyline   the skyline in its photos against the horizon from elevation data (skyline.py): to about 10°, where
            distant mountains show; worked out in the background, and kept when the rest is worked out again
  compass   its placed photos whose camera recorded where it pointed (GPSImgDirection), against where the photo was
            placed: real data, though few photos have it (design.md, "Placing meshes together"); one photo's is often
            20-30° off, so the skyline comes first, unless STRONG_COMPASS photos agree
  text      Mountain Project saying which face a climb is on ("the north face", "faces west"), against where that
            climb's photos look from in the mesh: a guess, to start from, which any of the others replaces

  heading.json  {version, created, edited, skyline, compass, text}: each null or {deg, ...what it came from}; and
                skyline_note, how its skyline was last looked at (skyline.check()'s note)

A face that "faces north" points north, so its photos were taken from north of it, looking south.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path

FILE = "heading.json"
VERSION = 1          # recomputed (all but an edit) when this changes
ORDER = ("edited", "skyline", "compass", "text")
STRONG_COMPASS = 3   # photos whose compasses agree (within 15°) for the compass to come before the skyline
_lock = threading.Lock()     # heading.json is written from more than one thread
GPS_IFD, IMG_DIRECTION, IMG_DIRECTION_REF = 0x8825, 17, 16
MIN_LEVEL = 0.3      # a camera looking more steeply up or down than this (its axis's horizontal share) says little

BEARINGS = {"north": 0, "northeast": 45, "east": 90, "southeast": 135, "south": 180, "southwest": 225, "west": 270,
            "northwest": 315}
ABBREVIATIONS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}
_WORD = r"(north|south|east|west)(?:[- ]?(east|west))?"
_WORDS = [re.compile(rf"\b{_WORD}[- ]?(?:facing|face|side|wall|aspect|arete|arête|corner|prow)\b", re.I),
          re.compile(rf"\bfac(?:es|ing) (?:the )?{_WORD}\b", re.I)]
_ABBREVIATED = re.compile(r"\b(NE|NW|SE|SW|N|E|S|W)[- ]?(?:face|side|facing)\b")      # capitals only: "a N face"
_ELSEWHERE = re.compile(r"\b(descen|downclimb|down climb|walk[- ]?off|approach|hike|trail|parking)", re.I)


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((mesh_dir / FILE).read_text())
    except (OSError, ValueError):
        return None


def _save(mesh_dir: Path, record: dict) -> dict:
    tmp = mesh_dir / (FILE + ".partial")
    tmp.write_text(json.dumps(record))
    tmp.replace(mesh_dir / FILE)
    return record


def best(record: dict | None) -> dict | None:
    """The heading to use: {deg, source, detail}, from the first of ORDER there is; None if none."""
    compass = (record or {}).get("compass") or {}
    strong = compass.get("photos", 0) >= STRONG_COMPASS and compass.get("spread_deg", 180) <= 15
    for source in ("edited", "compass", "skyline", "text") if strong else ORDER:
        found = (record or {}).get(source)
        if found:
            return {"deg": round(found["deg"] % 360, 1), "source": source, "detail": found.get("detail", "")}
    return None


def _bearing(x: float, z: float) -> float:
    """The compass bearing, in the mesh's own frame, of a horizontal direction: its -Z axis as north, +X as east."""
    return math.degrees(math.atan2(x, -z)) % 360


def _mean(degrees: list[float]) -> tuple[float, float]:
    """The circular mean of some bearings, and their spread (circular standard deviation), in degrees."""
    s, c = sum(math.sin(math.radians(d)) for d in degrees), sum(math.cos(math.radians(d)) for d in degrees)
    r = min(1.0, math.hypot(s, c) / len(degrees))
    return math.degrees(math.atan2(s, c)) % 360, math.degrees(math.sqrt(-2 * math.log(r))) if r > 1e-9 else 180.0


def face_named(text: str) -> tuple[float, str] | None:
    """The face of its rock a climb's text says it's on, as a compass bearing, and the words that say so; None if it
    names none, or only in a sentence about getting there or off."""
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text or ""):
        if _ELSEWHERE.search(sentence):
            continue
        for pattern in _WORDS:
            m = pattern.search(sentence)
            if m:
                word = (m[1] + (m[2] or "")).lower()
                if word in BEARINGS:
                    return BEARINGS[word], m[0]
        m = _ABBREVIATED.search(sentence)
        if m:
            return ABBREVIATIONS[m[1]], m[0]
    return None


def _poses(mesh_dir: Path) -> list[dict]:
    """Each placed photo's name, centre and viewing axis, in the mesh's own coordinates (cameras.json)."""
    import numpy as np
    try:
        cams = json.loads((mesh_dir / "cameras.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for cam in cams:
        w, x, y, z = cam["q"]
        R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                      [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                      [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])
        out.append({"name": cam["name"], "centre": -R.T @ np.asarray(cam["t"], float), "axis": R.T @ np.array([0.0, 0.0, 1.0])})
    return out


def _photo_direction(path: Path) -> tuple[float, bool] | None:
    """Where a photo's camera pointed (GPSImgDirection), and whether it's magnetic rather than true north."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            gps = im.getexif().get_ifd(GPS_IFD)
            if IMG_DIRECTION not in gps:
                return None
            return float(gps[IMG_DIRECTION]) % 360, str(gps.get(IMG_DIRECTION_REF, "T")).strip().upper().startswith("M")
    except Exception:
        return None


def from_compass(mesh_dir: Path, photos_dir: Path | None) -> dict | None:
    """The heading its placed photos' compass readings give, or None if none has one."""
    if photos_dir is None or not photos_dir.is_dir():
        return None
    found, magnetic = [], False
    for pose in _poses(mesh_dir):
        read = _photo_direction(photos_dir / pose["name"])
        a = pose["axis"]
        if read is None or math.hypot(a[0], a[2]) < MIN_LEVEL:
            continue
        found.append((read[0] - _bearing(a[0], a[2])) % 360)
        magnetic |= read[1]
    if not found:
        return None
    deg, spread = _mean(found)
    return {"deg": deg, "photos": len(found), "spread_deg": round(spread, 1), "magnetic": magnetic,
            "detail": f"the compass readings of {len(found)} of its photo{'s' if len(found) > 1 else ''}"
                      + (f", within {spread:.0f}°" if len(found) > 1 else "") + (" (magnetic north)" if magnetic else "")}


def _looked_from(poses: list[dict]):
    """Which way, horizontally, a set of photos look at the rock from: from the point nearest all their axes towards
    them. None if they don't say."""
    import numpy as np
    if not poses:
        return None
    A, b = np.zeros((3, 3)), np.zeros(3)
    for p in poses:
        d = p["axis"] / np.linalg.norm(p["axis"])
        P = np.eye(3) - np.outer(d, d)
        A += P
        b += P @ p["centre"]
    target = np.linalg.lstsq(A, b, rcond=None)[0] if len(poses) > 1 else poses[0]["centre"] + poses[0]["axis"]
    v = np.zeros(3)
    for p in poses:
        h = (p["centre"] - target) * np.array([1.0, 0.0, 1.0])
        n = np.linalg.norm(h)
        if n > 1e-9:
            v += h / n
    return None if np.linalg.norm(v) < 0.2 * len(poses) else (float(v[0]), float(v[2]))


def from_text(mesh_dir: Path, climbs: list[dict]) -> dict | None:
    """The heading Mountain Project's text gives: each climb that names its face (climbs: {id, name, text, own}, own
    for a climb whose mesh this is, made from its photos alone), against where it's seen from in the mesh: its view
    (views.json) on a boulder's mesh, or on its own mesh, where all the photos look from. None if none does."""
    import viewpoints
    views = (viewpoints.load(mesh_dir) or {}).get("climbs") or {}
    poses = None
    found = []
    for c in climbs:
        named = face_named(c.get("text") or "")
        if not named:
            continue
        view = views.get(c["id"])
        if view:
            side = (view["eye"][0] - view["target"][0], view["eye"][2] - view["target"][2])
        elif c.get("own"):
            poses = _poses(mesh_dir) if poses is None else poses
            side = _looked_from(poses)
        else:
            side = None
        if side is None or math.hypot(*side) < 1e-9:
            continue
        found.append({"id": c["id"], "name": c["name"], "words": named[1], "faces": named[0],
                      "deg": (named[0] - _bearing(*side)) % 360})
    if not found:
        return None
    deg, spread = _mean([f["deg"] for f in found])
    names = ", ".join(f"{f['name']} ({f['words']})" for f in found[:3]) + (f" and {len(found) - 3} more" if len(found) > 3 else "")
    return {"deg": deg, "climbs": found, "spread_deg": round(spread, 1),
            "detail": f"Mountain Project's text: {names}" + (f", within {spread:.0f}°" if len(found) > 1 else "")}


def compute(mesh_dir: Path, photos_dir: Path | None, climbs: list[dict]) -> dict:
    """Work its heading out again from its photos and its climbs' text, keeping an edit and the skyline's; the
    record."""
    compass, text = from_compass(mesh_dir, photos_dir), from_text(mesh_dir, climbs)
    with _lock:
        old = load(mesh_dir) or {}
        return _save(mesh_dir, {"version": VERSION, "created": time.time(), "edited": old.get("edited"),
                                "skyline": old.get("skyline"), "skyline_note": old.get("skyline_note"),
                                "compass": compass, "text": text})


def set_skyline(mesh_dir: Path, found: dict | None, note: dict) -> dict:
    """Keep what the skyline gave (skyline.check()): its heading or None, and how it went."""
    with _lock:
        record = load(mesh_dir) or {"version": 0, "created": time.time(), "edited": None, "compass": None, "text": None}
        record.update(skyline=found, skyline_note=note)
        return _save(mesh_dir, record)


def current(mesh_dir: Path) -> bool:
    return (load(mesh_dir) or {}).get("version") == VERSION


def set_edited(mesh_dir: Path, deg: float | None) -> dict:
    """Turn it by hand (deg, a compass bearing for its -Z axis), or with None go back to what's worked out."""
    if deg is not None and not (isinstance(deg, (int, float)) and not isinstance(deg, bool) and math.isfinite(deg)):
        raise ValueError("deg: a number of degrees, or null")
    with _lock:
        record = load(mesh_dir) or {"version": 0, "created": time.time(), "compass": None, "text": None}
        record["edited"] = None if deg is None else {"deg": float(deg) % 360, "at": time.time(), "detail": "turned by hand"}
        return _save(mesh_dir, record)
