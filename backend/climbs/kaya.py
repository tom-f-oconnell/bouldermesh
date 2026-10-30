"""KAYA (kayaclimb.com) adapter - link + manual import only.

Why so little
-------------
* kaya-app.kayaclimb.com is a client-rendered JS shell: the HTML served to a plain
  HTTP client contains no climb data, no OpenGraph tags, no JSON blob.  Climb pages,
  beta videos and coordinates only exist behind the app's private API.
* KAYA's Terms of Service explicitly forbid "data mining, robots, scraping, or similar
  data gathering" and circumventing content protections, so this project does not
  drive a browser against it or call the private API.
* Their 2025 data-ethos post says names/grades/GPS are "part of the commons" and that
  authors can export data, so the right path for bulk access is to ask them
  (support@kayaclimb.com) or go through a guidebook author who holds the export.

What this module does
---------------------
* fetch(url): records a Kaya link as a Climb with whatever can be read from the URL
  (slug -> name guess).  Everything else is filled in by the user in the UI or by
  linking the record to a Mountain Project / OpenBeta record for the same problem.
* from_export(rows): converts a user-provided export (CSV/JSON rows with name, grade,
  area, lat, lng, url, video columns in any reasonable spelling) into Climb records.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from .common import Climb, Media, find_videos

KAYA_HOSTS = ("kaya-app.kayaclimb.com", "kayaclimb.com", "www.kayaclimb.com")


def fetch(url: str, **_) -> list[Climb]:
    u = urlparse(url)
    if not any(u.netloc.endswith(h) for h in KAYA_HOSTS):
        raise ValueError("not a kayaclimb.com URL")
    parts = [p for p in u.path.split("/") if p]
    kind = "boulder"
    name = None
    if parts:
        slug = unquote(parts[-1])
        slug = re.sub(r"-\d+$", "", slug)              # "Index-345070" -> "Index"
        name = re.sub(r"[-_]+", " ", slug).strip() or None
        if parts[0] in ("location", "destination", "gym", "area"):
            kind = "area"
    c = Climb(source="kaya", source_id=u.path.strip("/") or url, url=url, name=name or "Kaya climb", kind=kind,
              raw={"note": "KAYA has no public API and its ToS forbids scraping; metadata must be entered "
                           "manually or linked from another source."})
    return [c]


_ALIASES = {
    "name": ("name", "climb", "climb_name", "route", "problem", "title"),
    "grade": ("grade", "difficulty", "v_grade", "vgrade"),
    "area": ("area", "location", "crag", "destination", "sector", "boulder"),
    "lat": ("lat", "latitude"),
    "lng": ("lng", "lon", "long", "longitude"),
    "url": ("url", "link", "kaya_url", "share_link"),
    "video": ("video", "video_url", "beta", "beta_video", "youtube"),
    "fa": ("fa", "first_ascent"),
    "description": ("description", "notes", "beta_notes"),
}


def _pick(row: dict, key: str):
    low = {re.sub(r"\W+", "_", k.strip().lower()): v for k, v in row.items() if k}
    for a in _ALIASES[key]:
        if a in low and str(low[a]).strip():
            return str(low[a]).strip()
    return None


def from_export(rows: list[dict]) -> list[Climb]:
    out = []
    for i, row in enumerate(rows):
        name = _pick(row, "name")
        if not name:
            continue
        lat, lng = _pick(row, "lat"), _pick(row, "lng")
        try:
            lat, lng = (float(lat), float(lng)) if lat and lng else (None, None)
        except ValueError:
            lat, lng = None, None
        url = _pick(row, "url") or f"kaya-export://{i}"
        area = _pick(row, "area")
        vids = find_videos(" ".join(str(v) for v in row.values()), "kaya", url)
        v = _pick(row, "video")
        if v and not vids:
            vids = [Media(url=v, kind="video", page=url, source="kaya")]
        c = Climb(source="kaya", source_id=url, url=url, name=name, kind="boulder", grade=_pick(row, "grade"),
                  fa=_pick(row, "fa"), lat=lat, lng=lng, coord_source="manual" if lat is not None else None,
                  area_path=[area] if area else [], boulder_name=area, description=_pick(row, "description"),
                  videos=vids, raw={"export_row": row})
        out.append(c)
    return out
