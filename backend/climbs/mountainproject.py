"""Mountain Project (mountainproject.com) adapter.

What is accessible
------------------
* No public API any more (the old api/v2 was retired). Everything below parses the
  public, server-rendered HTML of ONE page per call.
* Route page  /route/{id}/{slug}: name, grade(s), Type, FA, often a route-level GPS,
  star rating, Description/Location/Protection, photo links, YouTube/Vimeo links
  posted in comments.
* Area page   /area/{id}/{slug}: GPS, elevation, breadcrumb, child areas and routes,
  photos.  For bouldering the lowest area is usually the boulder itself.
* Photo page  /photo/{id}/{slug}: title, uploader, date.  Image files live at
  /assets/photos/climb/{id}_{size}_{ts}.jpg with size in {sqsmall, smallMed, medium, large}.
* Route Finder "Export to CSV" (/route-finder-export?...): MP's own export feature;
  gives every route under an area with *area* lat/lng.  Used by export_area_csv().

Terms
-----
The Adventure Projects terms of use disallow automated/bulk scraping.  This module
is deliberately low-volume: one page per import, disk cache, >=3 s between requests,
robots.txt respected.  Use it for climbs you are personally working with; use the
OpenBeta adapter for anything bulk.
"""
from __future__ import annotations

import csv
import io
import re
from urllib.parse import urljoin

from . import htmlmini
from .common import Climb, Media, clean, find_videos, http, parse_latlng

BASE = "https://www.mountainproject.com"
ROUTE_RE = re.compile(r"/route/(\d+)(?:/([^/?#]*))?")
AREA_RE = re.compile(r"/area/(\d+)(?:/([^/?#]*))?")
PHOTO_RE = re.compile(r"/photo/(\d+)(?:/([^/?#]*))?")
IMG_RE = re.compile(r"(https?://(?:www\.)?mountainproject\.com)?/assets/photos/climb/(\d+)_(sqsmall|small|smallMed|medium|large)_(\d+)\.(jpe?g|png)", re.I)
GRADE_CLASSES = ("rateYDS", "rateHueco", "rateFont", "rateFrench", "rateEwbanks", "rateUIAA", "rateZA", "rateBritish",
                 "rateAid", "rateIce", "rateMixed", "rateSnow")
SECTION_HEADINGS = ("description", "location", "protection", "getting there", "access issues")


def _abs(u: str) -> str:
    return urljoin(BASE, u)


def _details(doc: htmlmini.Node) -> dict:
    """The 'Type: / GPS: / FA: / Page Views:' table -> {label: value}."""
    out = {}
    for tr in doc.find_all("tr"):
        cells = [c for c in tr.children if isinstance(c, htmlmini.Node) and c.tag in ("td", "th")]
        if len(cells) >= 2:
            k = clean(cells[0].text()) or ""
            if k.endswith(":") and len(k) < 24:
                out[k[:-1].strip().lower()] = clean(cells[1].text())
    return out


def _breadcrumb(doc: htmlmini.Node) -> tuple[list[str], list[str]]:
    """Area links that precede the <h1> = the location hierarchy."""
    names, urls = [], []
    h1 = doc.find("h1")
    for a in doc.find_all("a"):
        if h1 is not None and _before(a, h1) is False:
            break
        href = a.get("href") or ""
        if AREA_RE.search(href) and "route-guide" not in href:
            name = clean(a.text())
            if name:
                names.append(name.rstrip("…"))
                urls.append(_abs(href))
    return names, urls


def _before(a: htmlmini.Node, b: htmlmini.Node) -> bool:
    """Document-order test (cheap: compare paths from root)."""
    def path(n):
        p = []
        while n.parent is not None:
            p.append(n.parent.children.index(n))
            n = n.parent
        return p[::-1]
    return path(a) < path(b)


def _sections(doc: htmlmini.Node) -> dict:
    out = {}
    for h in doc.find_all("h2") + doc.find_all("h3"):
        title = (clean(h.text()) or "").lower()
        title = re.sub(r"suggest change.*$", "", title).strip()
        if title in SECTION_HEADINGS:
            nxt = h.next_sibling_tag()
            if nxt is not None:
                out[title] = clean(nxt.text_lines().replace("\n", " "))
    return out


def _grades(doc: htmlmini.Node) -> tuple[str | None, dict]:
    grades, parts = {}, []
    h2 = None
    for h in doc.find_all("h2"):
        if h.find(pred=lambda n: any(c in GRADE_CLASSES for c in n.classes)):
            h2 = h
            break
    scope = h2 if h2 is not None else doc
    for span in scope.find_all("span"):
        for c in span.classes:
            if c in GRADE_CLASSES:
                g = clean(span.text())
                if g:
                    grades[c[4:].lower()] = g
                    parts.append(g)
    return (" ".join(dict.fromkeys(parts)) or None), grades


def _photos(doc: htmlmini.Node, html: str) -> list[Media]:
    out, seen = [], set()
    titles = {}
    for a in doc.find_all("a"):
        m = PHOTO_RE.search(a.get("href") or "")
        if m:
            pid = m[1]
            img = a.find("img")
            t = clean((img.get("alt") if img is not None else None) or a.get("title") or a.text())
            titles.setdefault(pid, (t, _abs(a.get("href"))))
    for m in IMG_RE.finditer(html):  # catches lazy-loaded data-src too
        pid, size, ts, ext = m[2], m[3], m[4], m[5]
        if pid in seen:
            continue
        seen.add(pid)
        t, page = titles.get(pid, (None, f"{BASE}/photo/{pid}"))
        out.append(Media(url=f"{BASE}/assets/photos/climb/{pid}_medium_{ts}.{ext}", kind="image", page=page,
                         title=t, source="mountainproject", license="© uploader (Mountain Project user content)"))
    for pid, (t, page) in titles.items():  # photo links whose <img> we never saw (e.g. 'Show more')
        if pid not in seen:
            out.append(Media(url="", kind="image", page=page, title=t, source="mountainproject",
                             license="© uploader (Mountain Project user content)"))
    return out


def resolve_image(media: Media, prefer_large: bool = True) -> str | None:
    """Fill in media.url from its photo page if needed; optionally upgrade to _large_."""
    if not media.url and media.page:
        html = http.get_text(media.page)
        m = IMG_RE.search(html)
        if m:
            media.url = f"{BASE}/assets/photos/climb/{m[2]}_medium_{m[4]}.{m[5]}"
    if media.url and prefer_large:
        large = media.url.replace("_medium_", "_large_")
        if large != media.url and http.head_ok(large):
            media.url = large
    return media.url or None


# ----------------------------------------------------------------------------
def parse_route(html: str, url: str) -> Climb:
    doc = htmlmini.parse(html)
    rid = (ROUTE_RE.search(url) or [None, "?"])[1]
    h1 = doc.find("h1")
    name = clean(h1.text()) if h1 is not None else (htmlmini.meta(doc, "og:title") or "").replace("Climb ", "")
    name = re.sub(r"\s+Suggest change$", "", name or "").strip() or f"route {rid}"
    det = _details(doc)
    grade, grades = _grades(doc)
    sec = _sections(doc)
    names, urls = _breadcrumb(doc)
    ll = parse_latlng(det.get("gps"))
    kind = "boulder" if "boulder" in (det.get("type") or "").lower() else "route"
    m = re.search(r"Avg:\s*([\d.]+)\s*from\s*([\d,]+)\s*votes?", html)
    text = doc.text_lines()
    c = Climb(source="mountainproject", source_id=rid, url=url, name=name, kind=kind, grade=grade, grades=grades,
              fa=det.get("fa"), rating=float(m[1]) if m else None, votes=int(m[2].replace(",", "")) if m else None,
              lat=ll[0] if ll else None, lng=ll[1] if ll else None, coord_source="route" if ll else None,
              area_path=names, area_urls=urls, description=sec.get("description"), location=sec.get("location"),
              protection=sec.get("protection"), images=_photos(doc, html),
              videos=find_videos(html, "mountainproject", url),
              cross_refs={"mp_id": rid}, raw={"details": det, "type": det.get("type")})
    c.boulder_name = c.guess_boulder_name()
    return c


def parse_area(html: str, url: str) -> tuple[Climb, list[tuple[str, str, str | None]], list[tuple[str, str]]]:
    """Returns (area record, [(route_url, name, grade)], [(child_area_url, name)])."""
    doc = htmlmini.parse(html)
    aid = (AREA_RE.search(url) or [None, "?"])[1]
    h1 = doc.find("h1")
    name = clean(h1.text()) if h1 is not None else f"area {aid}"
    name = re.sub(r"\s+Suggest change$", "", name or "").strip()
    det = _details(doc)
    sec = _sections(doc)
    names, urls = _breadcrumb(doc)
    ll = parse_latlng(det.get("gps"))
    elev = None
    me = re.search(r"([\d,]+)\s*m", det.get("elevation") or "")
    if me:
        elev = float(me[1].replace(",", ""))
    area = Climb(source="mountainproject", source_id=aid, url=url, name=name, kind="area",
                 lat=ll[0] if ll else None, lng=ll[1] if ll else None, coord_source="area" if ll else None,
                 elevation_m=elev, area_path=names + [name], area_urls=urls + [url],
                 description=sec.get("description"), location=sec.get("getting there"),
                 images=_photos(doc, html), videos=find_videos(html, "mountainproject", url),
                 cross_refs={"mp_area_id": aid}, raw={"details": det})
    routes, children = [], []
    seen = set()
    route_idx: dict[str, int] = {}
    for a in doc.find_all("a"):
        href = a.get("href") or ""
        rm = ROUTE_RE.search(href)
        if rm:
            tr = a
            while tr is not None and tr.tag != "tr":
                tr = tr.parent
            grade = None
            if tr is not None:   # route-table row: grade sits in the same row
                gs = [clean(s.text()) for s in tr.find_all("span") if any(c in GRADE_CLASSES for c in s.classes)]
                grade = " ".join(g for g in dict.fromkeys(gs) if g) or None
            if rm[1] in route_idx:   # already seen (e.g. linked from the description) -> prefer the table row
                if grade and routes[route_idx[rm[1]]][2] is None:
                    routes[route_idx[rm[1]]] = (_abs(href), clean(a.text()) or rm[1], grade)
                continue
            seen.add(rm[1])
            route_idx[rm[1]] = len(routes)
            routes.append((_abs(href), clean(a.text()) or rm[1], grade))
        am = AREA_RE.search(href)
        if am and _abs(href) not in urls and am[1] != aid and am[1] not in seen and h1 is not None and _before(a, h1) is False:
            t = clean(a.text())
            if t and len(t) < 80 and "map" not in href:
                seen.add(am[1])
                children.append((_abs(href), t))
    return area, routes, children


# ----------------------------------------------------------------------------
def fetch(url: str, include_children: bool = True, max_routes: int = 60, **_) -> list[Climb]:
    url = url.split("?")[0].split("#")[0]
    if ROUTE_RE.search(url):
        c = parse_route(http.get_text(url), url)
        if c.lat is None and c.area_urls:   # fall back to the boulder/area coordinates
            try:
                area, _, _ = parse_area(http.get_text(c.area_urls[-1]), c.area_urls[-1])
                if area.lat is not None:
                    c.lat, c.lng, c.coord_source, c.elevation_m = area.lat, area.lng, "parent-area", area.elevation_m
                c.images += [m for m in area.images if m.url not in {i.url for i in c.images}]
            except Exception as e:  # parent fetch is best-effort
                c.raw["parent_error"] = str(e)
        return [c]
    if AREA_RE.search(url):
        area, routes, children = parse_area(http.get_text(url), url)
        area.raw["routes"] = routes
        area.raw["children"] = children
        out = [area]
        if include_children:
            for rurl, rname, rgrade in routes[:max_routes]:
                try:
                    r = parse_route(http.get_text(rurl), rurl)
                except Exception as e:
                    r = Climb(source="mountainproject", source_id=(ROUTE_RE.search(rurl) or [0, "?"])[1], url=rurl,
                              name=rname, grade=rgrade, raw={"error": str(e)})
                if r.lat is None and area.lat is not None:
                    r.lat, r.lng, r.coord_source, r.elevation_m = area.lat, area.lng, "parent-area", area.elevation_m
                if not r.area_path:
                    r.area_path, r.area_urls = area.area_path, area.area_urls
                r.boulder_name = r.boulder_name or area.name
                out.append(r)
        return out
    raise ValueError("not a Mountain Project route or area URL")


def export_area_csv(area_id: str | int, route_type: str = "boulder") -> list[Climb]:
    """Mountain Project's Route Finder CSV export for everything under an area.
    Coordinates are the *area* lat/lng (MP's export doesn't include route-level GPS)."""
    params = ("selectedIds={id}&type={t}&diffMinrock=800&diffMinboulder=20000&diffMinaid=70000&diffMinice=30000"
              "&diffMinmixed=50000&diffMaxrock=12400&diffMaxboulder=21700&diffMaxaid=75260&diffMaxice=38500"
              "&diffMaxmixed=60000&is_trad_climb=1&is_sport_climb=1&is_top_rope=1&stars=0&pitches=0&sort1=area&sort2=rating")
    url = f"{BASE}/route-finder-export?" + params.format(id=area_id, t=route_type)
    text = http.get_text(url)
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        rurl = row.get("URL") or ""
        rid = (ROUTE_RE.search(rurl) or [0, rurl])[1]
        lat = row.get("Area Latitude"); lng = row.get("Area Longitude")
        path = [p.strip() for p in (row.get("Location") or "").split(">")]
        c = Climb(source="mountainproject", source_id=str(rid), url=rurl, name=row.get("Route") or rid,
                  kind="boulder" if "boulder" in (row.get("Route Type") or "").lower() else "route",
                  grade=row.get("Rating"), rating=float(row["Avg Stars"]) if row.get("Avg Stars") not in (None, "", "-1") else None,
                  lat=float(lat) if lat else None, lng=float(lng) if lng else None, coord_source="area" if lat else None,
                  area_path=path, cross_refs={"mp_id": str(rid)}, raw=dict(row))
        c.boulder_name = c.guess_boulder_name()
        out.append(c)
    return out
