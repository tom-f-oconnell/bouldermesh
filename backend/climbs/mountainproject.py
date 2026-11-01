"""Mountain Project (mountainproject.com) adapter.

What is accessible
------------------
* No public API any more (the old api/v2 was retired). Everything below parses the
  public, server-rendered HTML of ONE page per call.
* Route page  /route/{id}/{slug}: name, grade(s), Type, FA, often a route-level GPS,
  star rating, Description/Location/Protection, photo links, the beta-video gallery
  (youtube-nocookie embeds) and YouTube/Vimeo links posted in comments.
* Area page   /area/{id}/{slug}: GPS, elevation, breadcrumb, child areas and routes,
  photos.  For bouldering the lowest area is usually the boulder itself.
* Photo page  /photo/{id}/{slug}: title, uploader, date.  Image files live at
  /assets/photos/climb/{id}_{size}_{ts}.jpg with size in {sqsmall, smallMed, medium, large}.
* Route Finder "Export to CSV" (/route-finder-export?...): MP's own export feature;
  gives up to 1000 routes under an area with *area* lat/lng, in one request.  Used by
  export_area_csv().

Terms
-----
The Adventure Projects terms of use disallow automated/bulk scraping.  This module
is deliberately low-volume: one page per import, disk cache, robots.txt respected
including its Crawl-delay (60 s).  Use it for climbs you are personally working with;
use the OpenBeta adapter for anything bulk.  Meshing a whole area lists it with the one
export request, then reads one climb's page only when that climb's turn comes, which is
about one every 10 minutes or more.  Don't read every climb's page up front, e.g. for its tick count.
"""
from __future__ import annotations

import csv
import io
import json
import re
from html import unescape
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


def _own_text(n: htmlmini.Node) -> str | None:
    """Text directly inside n, skipping child elements (MP nests edit links, 'Climbing'
    suffixes and grade-system labels inside its <h1> and grade spans)."""
    return clean(unescape("".join(c for c in n.children if isinstance(c, str))))


def _details(doc: htmlmini.Node) -> dict:
    """The 'Type: / GPS: / FA: / Page Views:' table -> {label: value}. Value cells are
    joined (Elevation has separate imperial and metric cells); the first table wins over
    later label-like rows such as the route-finder form on area pages."""
    out = {}
    for tr in doc.find_all("tr"):
        cells = [c for c in tr.children if isinstance(c, htmlmini.Node) and c.tag in ("td", "th")]
        if len(cells) >= 2:
            k = clean(cells[0].text()) or ""
            if k.endswith(":") and len(k) < 24:
                out.setdefault(k[:-1].strip().lower(), clean(" ".join(c.text() for c in cells[1:])))
    return out


def _breadcrumb(doc: htmlmini.Node) -> tuple[list[str], list[str]]:
    """The location hierarchy above this page. Prefers the JSON-LD BreadcrumbList, whose
    names aren't truncated like the visible breadcrumb's ("Yosemite Valle…")."""
    for s in doc.find_all("script", attr={"type": "application/ld+json"}):
        try:
            d = json.loads("".join(c for c in s.children if isinstance(c, str)))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "BreadcrumbList":
            items = sorted(d.get("itemListElement") or [], key=lambda i: i.get("position", 0))
            items = [i for i in items if AREA_RE.search(i.get("item") or "") and clean(i.get("name"))]
            if items:
                return [clean(i["name"]) for i in items], [_abs(i["item"]) for i in items]
    # fallback: area links that precede the <h1>
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


def _grade_key(cls: str, g: str) -> str:
    # MP puts V-grades in rateYDS spans; call them vscale like OpenBeta does
    if cls in ("rateYDS", "rateHueco") and re.match(r"V[\d-]", g):
        return "vscale"
    return cls[4:].lower()


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
                g = _own_text(span) or clean(span.text())
                if g:
                    grades[_grade_key(c, g)] = g
                    parts.append(g)
    return (" ".join(dict.fromkeys(parts)) or None), grades


def _photos(doc: htmlmini.Node) -> list[Media]:
    """Photo cards pair a /photo/{id} link with an <img> of the file. The two ids often
    differ (e.g. an area page reusing a route's photo), so pair them per link, not by id.
    Images outside photo links (guidebook covers, ...) are ignored."""
    pages: dict[str, list] = {}   # photo page id -> [title, page url, IMG_RE match]
    for a in doc.find_all("a"):
        m = PHOTO_RE.search(a.get("href") or "")
        if not m:
            continue
        img = a.find("img")
        t = (img.get("alt") if img is not None else None) or a.get("title") or a.text()
        t = clean(re.sub(r"<br\s*/?>", " ", t or ""))
        f = IMG_RE.search((img.get("data-src") or img.get("src") or "") if img is not None else "")
        e = pages.setdefault(m[1], [None, _abs(a.get("href")), None])
        e[0] = e[0] or t   # the hero carousel's empty link comes before the photo card
        e[2] = e[2] or f
    out, seen = [], set()
    lic = "© uploader (Mountain Project user content)"
    for t, page, f in pages.values():
        if f and f[2] in seen:
            continue
        url = ""   # no <img> seen (e.g. 'Show more'): resolve_image() reads the photo page
        if f:
            seen.add(f[2])
            url = f"{BASE}/assets/photos/climb/{f[2]}_medium_{f[4]}.{f[5]}"
        out.append(Media(url=url, kind="image", page=page, title=t, source="mountainproject", license=lic))
    return out


def resolve_image(media: Media, prefer_large: bool = True) -> str | None:
    """Fill in media.url from its photo page if needed; optionally switch to the _large_
    variant (~4x the bytes of _medium_). Not probed first, since each request costs a
    Crawl-delay: harvest.download_all() falls back to _medium_ if _large_ is missing."""
    if not media.url and media.page:
        html = http.get_text(media.page)
        m = IMG_RE.search(html)
        if m:
            media.url = f"{BASE}/assets/photos/climb/{m[2]}_medium_{m[4]}.{m[5]}"
    if media.url and prefer_large:
        media.url = media.url.replace("_medium_", "_large_")
    return media.url or None


def _deinvert(name: str) -> str:
    """Area route tables list "Moffat Start, The"; the problem's own page says "The Moffat Start"."""
    m = re.fullmatch(r"(.+), (The|A|An)", name)
    return f"{m[2]} {m[1]}" if m else name


# ----------------------------------------------------------------------------
def parse_route(html: str, url: str) -> Climb:
    doc = htmlmini.parse(html)
    rid = (ROUTE_RE.search(url) or [None, "?"])[1]
    h1 = doc.find("h1")
    name = _own_text(h1) if h1 is not None else (htmlmini.meta(doc, "og:title") or "").replace("Climb ", "")
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
              protection=sec.get("protection"), images=_photos(doc),
              videos=find_videos(html, "mountainproject", url),
              cross_refs={"mp_id": rid}, raw={"details": det, "type": det.get("type")})
    c.boulder_name = c.guess_boulder_name()
    return c


def parse_area(html: str, url: str) -> tuple[Climb, list[tuple[str, str, str | None]], list[tuple[str, str]]]:
    """Returns (area record, [(route_url, name, grade)], [(child_area_url, name)])."""
    doc = htmlmini.parse(html)
    aid = (AREA_RE.search(url) or [None, "?"])[1]
    h1 = doc.find("h1")
    name = _own_text(h1) if h1 is not None else f"area {aid}"
    name = re.sub(r"\s+Suggest change$", "", name or "").strip()
    det = _details(doc)
    sec = _sections(doc)
    names, urls = _breadcrumb(doc)
    ll = parse_latlng(det.get("gps"))
    elev = None
    me = re.search(r"([\d,]+)\s*m\b", det.get("elevation") or "")
    mf = re.search(r"([\d,]+)\s*ft\b", det.get("elevation") or "")
    if me:
        elev = float(me[1].replace(",", ""))
    elif mf:
        elev = float(round(float(mf[1].replace(",", "")) * 0.3048))
    area = Climb(source="mountainproject", source_id=aid, url=url, name=name, kind="area",
                 lat=ll[0] if ll else None, lng=ll[1] if ll else None, coord_source="area" if ll else None,
                 elevation_m=elev, area_path=names + [name], area_urls=urls + [url],
                 description=sec.get("description"), location=sec.get("getting there"),
                 images=_photos(doc), videos=find_videos(html, "mountainproject", url),
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
                    routes[route_idx[rm[1]]] = (_abs(href), _deinvert(clean(a.text()) or rm[1]), grade)
                continue
            seen.add(rm[1])
            route_idx[rm[1]] = len(routes)
            routes.append((_abs(href), _deinvert(clean(a.text()) or rm[1]), grade))
        am = AREA_RE.search(href)
        if am and _abs(href) not in urls and am[1] != aid and am[1] not in seen and h1 is not None and _before(a, h1) is False:
            t = clean(a.text())
            if t and len(t) < 80 and "map" not in href:
                seen.add(am[1])
                children.append((_abs(href), t))
    return area, routes, children


def _stub(area: Climb, url: str, name: str, grade: str | None) -> Climb:
    """A problem summarised from its area's route table, without fetching its own page."""
    rid = ROUTE_RE.search(url)[1]
    boulder = bool(grade and grade.startswith("V"))
    return Climb(source="mountainproject", source_id=rid, url=url, name=name, kind="boulder" if boulder else "route",
                 grade=grade, lat=area.lat, lng=area.lng, coord_source="parent-area" if area.lat is not None else None,
                 elevation_m=area.elevation_m, area_path=area.area_path, area_urls=area.area_urls,
                 boulder_name=area.name if boulder else None, cross_refs={"mp_id": rid, "mp_area_id": area.source_id},
                 raw={"stub": True})


# ----------------------------------------------------------------------------
def fetch(url: str, include_children: bool = True, **_) -> list[Climb]:
    """Records for a route URL (one page, plus its area if the route has no GPS), or for an
    area URL: the area, plus with include_children a summary of each graded problem in the
    area's route table (raw['stub']=True; refetch the problem URL for photos/description).
    Fetching every problem page up front would take a minute each under MP's Crawl-delay."""
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
        if include_children:   # routes only linked from the description/comments may be on other rocks
            out += [_stub(area, *r) for r in routes if r[2]]
        return out
    raise ValueError("not a Mountain Project route or area URL")


def area_id(text: str) -> str | None:
    """The area id in a Mountain Project area URL, or a bare id; None if it's neither."""
    text = text.strip()
    if text.isdigit():
        return text
    m = AREA_RE.search(text)
    return m[1] if m and "mountainproject.com" in text else None


def export_area_csv(area_id: str | int, route_type: str = "boulder", sort: str = "area") -> list[Climb]:
    """Mountain Project's Route Finder CSV export for everything under an area.
    Coordinates are the *area* lat/lng (MP's export doesn't include route-level GPS).

    Args:
        sort: "area", or "popularity" for MP's own ranking, most popular first. The export stops
            at 1000 rows, so in a bigger area this also decides which climbs you get.
    """
    params = ("selectedIds={id}&type={t}&diffMinrock=800&diffMinboulder=20000&diffMinaid=70000&diffMinice=30000"
              "&diffMinmixed=50000&diffMaxrock=12400&diffMaxboulder=21700&diffMaxaid=75260&diffMaxice=38500"
              "&diffMaxmixed=60000&is_trad_climb=1&is_sport_climb=1&is_top_rope=1&stars=0&pitches=0&sort1={s}&sort2=rating")
    s = {"area": "area", "popularity": "popularity+desc"}[sort]
    return parse_area_csv(http.get_text(f"{BASE}/route-finder-export?" + params.format(id=area_id, t=route_type, s=s)))


def parse_area_csv(text: str) -> list[Climb]:
    """Records from a Route Finder export, in its order. They only summarise each climb
    (raw['stub']=True): its own page has the photos, videos and description."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        rurl = row.get("URL") or ""
        rid = (ROUTE_RE.search(rurl) or [0, rurl])[1]
        lat = row.get("Area Latitude"); lng = row.get("Area Longitude")
        # Location runs from the boulder up to the state; area_path is top-down everywhere else
        path = [p.strip() for p in (row.get("Location") or "").split(">")][::-1]
        v = re.search(r"\bV(?:-easy|\d[\d+/-]*)", row.get("Rating") or "")
        c = Climb(source="mountainproject", source_id=str(rid), url=rurl, name=row.get("Route") or rid,
                  kind="boulder" if "boulder" in (row.get("Route Type") or "").lower() else "route",
                  grade=row.get("Rating"), grades={"vscale": v[0]} if v else {},
                  rating=float(row["Avg Stars"]) if row.get("Avg Stars") not in (None, "", "-1") else None,
                  lat=float(lat) if lat else None, lng=float(lng) if lng else None, coord_source="area" if lat else None,
                  area_path=path, cross_refs={"mp_id": str(rid)}, raw={**row, "stub": True})
        c.boulder_name = c.guess_boulder_name()
        out.append(c)
    return out
