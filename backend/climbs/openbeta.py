"""OpenBeta (openbeta.io) adapter - the open-licensed climbing database.

* GraphQL endpoint: https://api.openbeta.io  (no key needed; please keep volume modest
  and consider sponsoring them if you lean on it).  Data is CC BY-SA 4.0; much of it
  was seeded from Mountain Project's historical open data, so records carry an
  `mp_id` cross reference.
* Coordinates: areas (incl. boulders, metadata.isBoulder) and climbs (metadata.lat/lng,
  may be null -> we fall back to the parent area).
* Media: user-uploaded photos tagged to climbs/areas (`media { mediaUrl width height }`).
  Files are served from OPENBETA_MEDIA_BASE (default https://media.openbeta.io).
* Search: by area name (`areas(filter:{area_name:{match}})`) and by proximity
  (`cragsNear(lnglat:{lat,lng}, maxDistance)` / `cragsWithin(filter:{bbox, zoom})`).

Field names follow openbeta-graphql's schema; if OpenBeta renames something the
error message from the API is surfaced verbatim so it's easy to adjust the fragments.
"""
from __future__ import annotations

import os
import re

from .common import Climb, Media, clean, find_videos, http

ENDPOINT = os.environ.get("OPENBETA_API", "https://api.openbeta.io")
MEDIA_BASE = os.environ.get("OPENBETA_MEDIA_BASE", "https://media.openbeta.io")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

CLIMB_FRAG = """
fragment ClimbFields on Climb {
  uuid name fa length
  grades { vscale font yds french ewbank uiaa }
  type { bouldering trad sport tr alpine aid }
  metadata { lat lng mp_id }
  content { description location protection }
  pathTokens
  media { mediaUrl width height format username }
}"""
AREA_FRAG = """
fragment AreaFields on Area {
  uuid area_name pathTokens
  metadata { lat lng isBoulder leaf mp_id }
  content { description }
  media { mediaUrl width height format username }
}"""


class OpenBetaError(RuntimeError):
    pass


def gql(query: str, variables: dict | None = None, use_cache: bool = True) -> dict:
    res = http.get_json(ENDPOINT, {"query": query, "variables": variables or {}}, use_cache=use_cache)
    if res.get("errors"):
        raise OpenBetaError("; ".join(e.get("message", str(e)) for e in res["errors"]))
    return res.get("data") or {}


def media_url(m: dict) -> str:
    u = m.get("mediaUrl") or ""
    return u if u.startswith("http") else MEDIA_BASE.rstrip("/") + "/" + u.lstrip("/")


def _media(items, page) -> list[Media]:
    out = []
    for m in items or []:
        out.append(Media(url=media_url(m), kind="image", page=page, credit=m.get("username"),
                         license="CC BY-SA 4.0 (OpenBeta user upload)", width=m.get("width"), height=m.get("height"),
                         source="openbeta"))
    return out


def _grade(g: dict | None, t: dict | None) -> tuple[str | None, dict]:
    g = {k: v for k, v in (g or {}).items() if v}
    if (t or {}).get("bouldering"):
        order = ("vscale", "font", "yds", "french", "ewbank", "uiaa")
    else:
        order = ("yds", "french", "ewbank", "uiaa", "vscale", "font")
    # boulders carry their V-grade in yds too (inherited from MP's rateYDS spans)
    parts = dict.fromkeys(g[k] for k in order if k in g)
    return (" ".join(parts) or None), g


def climb_from_node(n: dict, area: dict | None = None) -> Climb:
    t = n.get("type") or {}
    grade, grades = _grade(n.get("grades"), t)
    md = n.get("metadata") or {}
    path = list(n.get("pathTokens") or [])
    url = f"https://openbeta.io/climbs/{n['uuid']}"
    content = n.get("content") or {}
    text = " ".join(filter(None, [content.get("description"), content.get("location"), content.get("protection")]))
    fa = n.get("fa") if (n.get("fa") or "").strip().lower() not in ("", "unknown") else None
    c = Climb(source="openbeta", source_id=n["uuid"], url=url, name=n.get("name") or "?",
              kind="boulder" if t.get("bouldering") else "route", grade=grade, grades=grades, fa=fa,
              lat=md.get("lat"), lng=md.get("lng"), coord_source="route" if md.get("lat") is not None else None,
              area_path=path[:-1] if path and path[-1] == n.get("name") else path,
              description=clean(content.get("description")), location=clean(content.get("location")),
              protection=clean(content.get("protection")), images=_media(n.get("media"), url),
              videos=find_videos(text, "openbeta", url),
              cross_refs={"openbeta_uuid": n["uuid"], **({"mp_id": md["mp_id"]} if md.get("mp_id") else {})},
              raw={"type": t})
    if area:
        amd = area.get("metadata") or {}
        # climbs without their own GPS are stored with their area's, so equal coords aren't route-level
        if (c.lat is None or (c.lat, c.lng) == (amd.get("lat"), amd.get("lng"))) and amd.get("lat") is not None:
            c.lat, c.lng, c.coord_source = amd["lat"], amd["lng"], "parent-area"
        if amd.get("isBoulder"):
            c.boulder_name = area.get("area_name")
        if not c.area_path:
            c.area_path = list(area.get("pathTokens") or [])
        c.cross_refs.setdefault("openbeta_area_uuid", area.get("uuid"))
        if amd.get("mp_id"):
            c.cross_refs.setdefault("mp_area_id", amd["mp_id"])
    c.boulder_name = c.boulder_name or c.guess_boulder_name()
    return c


def area_from_node(a: dict) -> Climb:
    md = a.get("metadata") or {}
    url = f"https://openbeta.io/areas/{a['uuid']}"
    c = Climb(source="openbeta", source_id=a["uuid"], url=url, name=a.get("area_name") or "?", kind="area",
              lat=md.get("lat"), lng=md.get("lng"), coord_source="area" if md.get("lat") is not None else None,
              area_path=list(a.get("pathTokens") or []), description=clean((a.get("content") or {}).get("description")),
              images=_media(a.get("media"), url),
              cross_refs={"openbeta_area_uuid": a["uuid"], **({"mp_area_id": md["mp_id"]} if md.get("mp_id") else {})},
              raw={"isBoulder": md.get("isBoulder"), "leaf": md.get("leaf")})
    if md.get("isBoulder"):
        c.boulder_name = c.name
    return c


# ----------------------------------------------------------------------------
def get_climb(uuid: str) -> list[Climb]:
    data = gql(CLIMB_FRAG + AREA_FRAG + """
    query($id: ID) { climb(uuid: $id) { ...ClimbFields parent { ...AreaFields } } }""", {"id": uuid})
    n = data.get("climb")
    if not n:
        raise OpenBetaError(f"climb {uuid} not found")
    return [climb_from_node(n, n.get("parent"))]


def get_area(uuid: str, include_children: bool = True) -> list[Climb]:
    data = gql(CLIMB_FRAG + AREA_FRAG + """
    query($id: ID) { area(uuid: $id) { ...AreaFields
        climbs { ...ClimbFields }
        children { uuid area_name metadata { lat lng isBoulder } } } }""", {"id": uuid})
    a = data.get("area")
    if not a:
        raise OpenBetaError(f"area {uuid} not found")
    area = area_from_node(a)
    area.raw["children"] = [(f"https://openbeta.io/areas/{ch['uuid']}", ch["area_name"]) for ch in a.get("children") or []]
    out = [area]
    if include_children:
        out += [climb_from_node(n, a) for n in a.get("climbs") or []]
    return out


def fetch(url: str, include_children: bool = True, **_) -> list[Climb]:
    m = UUID_RE.search(url)
    if not m:
        raise ValueError("OpenBeta URLs need a uuid, e.g. https://openbeta.io/climbs/<uuid> or /areas/<uuid>")
    if "/climbs/" in url or "/climb/" in url:
        return get_climb(m[0])
    return get_area(m[0], include_children)


def search(query: str = "", lat: float | None = None, lng: float | None = None, radius_m: int = 2000,
           limit: int = 50) -> list[Climb]:
    """Areas whose name matches `query`, and/or crags near (lat, lng). Returns area records
    (kind='area'); use fetch()/get_area() on one of them to pull its climbs."""
    out: list[Climb] = []
    if query:
        data = gql(AREA_FRAG + """
        query($q: String!) { areas(filter: {area_name: {match: $q, exactMatch: false}}) { ...AreaFields } }""",
                   {"q": query})
        out += [area_from_node(a) for a in data.get("areas") or []]
    if lat is not None and lng is not None:
        data = gql("""
        query($lat: Float!, $lng: Float!, $d: Int!) {
          cragsNear(lnglat: {lat: $lat, lng: $lng}, maxDistance: $d, includeCrags: true) {
            count crags { uuid area_name pathTokens metadata { lat lng isBoulder leaf mp_id } } } }""",
                   {"lat": lat, "lng": lng, "d": int(radius_m)})
        for bucket in data.get("cragsNear") or []:
            out += [area_from_node(a) for a in bucket.get("crags") or []]
    seen, uniq = set(), []
    for c in out:
        if c.source_id not in seen:
            seen.add(c.source_id)
            uniq.append(c)
    return uniq[:limit]


def find_by_mp_id(mp_id: str, name_hint: str = "", area_hint: str = "") -> Climb | None:
    """Best-effort MP -> OpenBeta resolution. The API has no mp_id filter, so we search
    areas by name and compare metadata.mp_id on the climbs."""
    if not area_hint:
        return None
    for area in search(query=area_hint, limit=10):
        try:
            recs = get_area(area.source_id)
        except OpenBetaError:
            continue
        for r in recs:
            if r.cross_refs.get("mp_id") == str(mp_id):
                return r
        if name_hint:
            for r in recs:
                if r.name.lower() == name_hint.lower():
                    return r
    return None
