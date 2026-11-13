"""What BoulderMesh's phone apps record with each photo they take (app/), and what a mesh made from such photos learns
from it: which way is down, how big it is, and where it is.

Each photo carries its readings in its own file, so that they go wherever the photo does (a project's photos, a
machine's job): in the EXIF UserComment, as MARK followed by JSON. app/FORMAT.md has every field. Those used here:

  down       gravity, a unit vector in the stored pixels' axes: x along the rows, y down the columns, z the way the camera
             looks (as orientation.down_in_image() gives an iPhone's)
  ar         {s, p}: the phone's place when it was taken, in metres, in the frame its motion tracking (ARKit, ARCore)
             kept for session s: +Y up
  gps        {lat, lng, acc}: its fix then, and how far off it says that may be, in metres

Where the camera pointed is in the standard GPSImgDirection, from true north, which heading.py reads already.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

MARK = b"BMSH1:"
EXIF_IFD, USER_COMMENT = 0x8769, 0x9286
AR_SD = 0.02            # log sd of a scale from motion tracking, however well its places fit: its own drift
AR_MIN = 4              # photos of one session a scale from it needs
AR_SPREAD = 0.3         # metres its places have to be spread over (rms), to say anything of scale
GPS_MIN = 4             # photos with fixes a position needs
GPS_WORST = 25.0        # metres: a fix that says it may be further off than this isn't used
EARTH = 6378137.0


def parse(comment) -> dict | None:
    """The readings in an EXIF UserComment, as bytes or text (with or without its 8-byte character code)."""
    raw = comment.encode("utf-8", "replace") if isinstance(comment, str) else bytes(comment or b"")
    at = raw.find(MARK)
    for codec in ("utf-16-le", "utf-16-be") if at < 0 else ():     # written as Unicode: two bytes a character, either way round
        wide = raw.find(MARK.decode().encode(codec))
        if wide >= 0:
            raw, at = raw[wide:].decode(codec, "replace").encode("utf-8", "replace"), 0
            break
    if at < 0:
        return None
    try:
        blob = json.loads(raw[at + len(MARK):].split(b"\0", 1)[0].decode("utf-8", "replace"))
    except ValueError:
        return None
    return blob if isinstance(blob, dict) else None


def read(path: Path) -> dict | None:
    """A photo's readings, or None if an app didn't take it."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            comment = im.getexif().get_ifd(EXIF_IFD).get(USER_COMMENT)
    except Exception:
        return None
    return parse(comment) if comment else None


def down(blob: dict | None) -> list[float] | None:
    """Its gravity reading, a unit vector in the stored pixels' axes, or None if it has none worth using."""
    v = (blob or {}).get("down")
    if not isinstance(v, list) or len(v) != 3 or not all(isinstance(x, (int, float)) for x in v):
        return None
    size = math.sqrt(sum(x * x for x in v))
    return [x / size for x in v] if 0.5 < size < 1.5 else None


def _centre(cam: dict):
    import numpy as np
    w, x, y, z = cam["q"]
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                  [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                  [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])
    return -R.T @ np.asarray(cam["t"], float)


def similarity(X, Y) -> tuple[float, "object", "object"]:
    """The scale, rotation and shift taking points X onto Y, least squares (Umeyama): Y ~ s R X + t."""
    import numpy as np
    X, Y = np.asarray(X, float), np.asarray(Y, float)
    mx, my = X.mean(axis=0), Y.mean(axis=0)
    A, B = X - mx, Y - my
    U, S, Vt = np.linalg.svd(B.T @ A / len(X))
    D = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        D[2, 2] = -1
    R = U @ D @ Vt
    s = float(np.trace(np.diag(S) @ D) / (A * A).sum() * len(X))
    return s, R, my - s * R @ mx


def scale_estimates(cams: list[dict], blobs: dict[str, dict]) -> list[dict]:
    """A scale from each motion-tracking session whose photos were placed: how far apart the phone says they were taken,
    against how far apart the model has them. Places the two disagree on (a photo misplaced, tracking that jumped) are
    left out, twice over.

    Returns:
        For scale.combine(): [{kind: "phone", metres_per_unit, sd, n, of, session, rms_m, spread_m}].
    """
    import numpy as np
    by: dict[str, list[tuple]] = {}
    for cam in cams:
        ar = (blobs.get(cam["name"]) or {}).get("ar") or {}
        p = ar.get("p")
        if isinstance(p, list) and len(p) == 3 and ar.get("track", "normal") == "normal":
            by.setdefault(str(ar.get("s", "")), []).append((_centre(cam), np.asarray(p, float)))
    out = []
    for session, pairs in sorted(by.items()):
        X, Y = np.array([a for a, _ in pairs]), np.array([b for _, b in pairs])
        keep = np.ones(len(X), bool)
        for _ in range(3):
            if keep.sum() < AR_MIN:
                break
            s, R, t = similarity(X[keep], Y[keep])
            off = np.linalg.norm(Y - (s * (R @ X.T).T + t), axis=1)
            again = off <= max(0.05, 3 * 1.4826 * float(np.median(off[keep])))
            if (again == keep).all():
                break
            keep = again
        n = int(keep.sum())
        if n < AR_MIN:
            continue
        s, R, t = similarity(X[keep], Y[keep])
        spread = float(np.sqrt(((Y[keep] - Y[keep].mean(axis=0)) ** 2).sum(axis=1).mean()))
        if spread < AR_SPREAD or s <= 0:
            continue
        rms = float(np.sqrt((np.linalg.norm(Y[keep] - (s * (R @ X[keep].T).T + t), axis=1) ** 2).mean()))
        out.append({"kind": "phone", "metres_per_unit": round(s, 6), "sd": round(float(np.hypot(AR_SD, rms / spread / math.sqrt(n))), 4),
                    "n": n, "of": len(pairs), "session": session, "rms_m": round(rms, 3), "spread_m": round(spread, 2)})
    return out


def position(cams: list[dict], blobs: dict[str, dict], metres_per_unit: float, point=None, north: float | None = None) -> dict | None:
    """Where a levelled mesh is, from the GPS fixes of its placed photos: the phone's places around it, as the model
    has them, turned and shifted onto their fixes. Fixes far from the rest are left out.

    Args:
        metres_per_unit: its scale.
        point: the point of the mesh to place (its own coordinates); its origin if None.
        north: its heading if known (heading.py's deg: the compass bearing of its -Z axis), used where the fixes are too
            close together to say which way it faces.

    Returns:
        {lat, lng, acc_m (a standard error, counting fixes within 10 s of each other as one), n, of, heading_deg and
        heading_se_deg (None if the fixes couldn't say), heading_from ("gps", "given" or None)}, or None with too few.
    """
    import numpy as np
    rows = []
    for cam in cams:
        blob = blobs.get(cam["name"]) or {}
        g = blob.get("gps") or {}
        try:
            lat, lng, acc = float(g["lat"]), float(g["lng"]), float(g.get("acc") or 10.0)
        except (KeyError, TypeError, ValueError):
            continue
        if acc <= GPS_WORST and abs(lat) <= 90 and abs(lng) <= 180:
            c = _centre(cam)
            rows.append((c[0] * metres_per_unit, c[2] * metres_per_unit, lat, lng, max(acc, 1.0), float(blob.get("t") or 0)))
    if len(rows) < GPS_MIN:
        return None
    a = np.array(rows)
    lat0, lng0 = float(np.median(a[:, 2])), float(np.median(a[:, 3]))
    east = np.radians(a[:, 3] - lng0) * EARTH * math.cos(math.radians(lat0))
    nrth = np.radians(a[:, 2] - lat0) * EARTH
    w = 1 / a[:, 4] ** 2
    keep = np.ones(len(a), bool)
    for _ in range(3):
        ww = w * keep
        mx, mz = np.average(a[:, 0], weights=ww), np.average(a[:, 1], weights=ww)
        me, mn = np.average(east, weights=ww), np.average(nrth, weights=ww)
        dx, dz, de, dn = a[:, 0] - mx, a[:, 1] - mz, east - me, nrth - mn
        # in the mesh's frame -Z is its north and +X its east (heading._bearing()): turned by H, a point (x, z) is
        # (x cos H - z sin H) east and (-x sin H - z cos H) north of the middle
        num = float(np.sum(ww * (-de * dz - dn * dx)))
        den = float(np.sum(ww * (de * dx - dn * dz)))
        radius = float(np.sqrt(np.average(dx * dx + dz * dz, weights=ww)))
        H = math.atan2(num, den)
        told = radius > 0 and math.hypot(num, den) > 0
        use = H if told else math.radians(north or 0.0)
        pe = dx * math.cos(use) - dz * math.sin(use)
        pn = -dx * math.sin(use) - dz * math.cos(use)
        off = np.hypot(de - pe, dn - pn)
        again = off <= max(3.0, 3 * 1.4826 * float(np.median(off[keep])))
        if (again == keep).all() or again.sum() < GPS_MIN:
            break
        keep = again
    n = int(keep.sum())
    times = np.sort(a[keep, 5])
    independent = max(1, 1 + int(np.sum(np.diff(times) > 10))) if times[-1] > 0 else max(1, n // 5)
    resid = float(np.sqrt(np.average(off[keep] ** 2, weights=w[keep])))
    heading_se = math.degrees(max(resid, 1.0) / (radius * math.sqrt(independent))) if radius > 0.5 else None
    by_gps = told and heading_se is not None and heading_se <= 25
    if not by_gps and north is not None:
        use = math.radians(north)
    px, pz = (0.0, 0.0) if point is None else (float(point[0]) * metres_per_unit, float(point[2]) * metres_per_unit)
    e = me + (px - mx) * math.cos(use) - (pz - mz) * math.sin(use)
    nn = mn - (px - mx) * math.sin(use) - (pz - mz) * math.cos(use)
    return {"lat": round(lat0 + math.degrees(nn / EARTH), 7), "lng": round(lng0 + math.degrees(e / (EARTH * math.cos(math.radians(lat0)))), 7),
            "acc_m": round(max(resid, float(np.median(a[keep, 4]))) / math.sqrt(independent), 2), "n": n, "of": len(a),
            "heading_deg": round(math.degrees(H) % 360, 1) if by_gps else None,
            "heading_se_deg": round(heading_se, 1) if by_gps else None,
            "heading_from": "gps" if by_gps else "given" if north is not None else None}


def blobs(photos: Path, names: list[str]) -> dict[str, dict]:
    """The readings of those of these photos an app took, by name."""
    out = {}
    for n in names:
        b = read(photos / n)
        if b:
            out[n] = b
    return out
