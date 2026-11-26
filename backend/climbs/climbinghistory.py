"""climbing-history.org adapter: one climb's page, and its lists of the hardest climbs.

What's read
-----------
* A climb's page (/climb/<id>/<name>): its name, grade, crag, where the crag is (country down to district), first
  ascent, the other sites' pages for it (8a.nu, UKClimbing, ...), and every link in its notes and its ascents'
  references. The links are where its videos are: YouTube ones (and plain vimeo.com/<id> ones) become its videos,
  those of its ascents first, in the page's order (the first ascent, then repeats), then those of its own notes.
  A YouTube link to a moment in a video (?t=439) keeps that as the video's start (Media.start).
* Instagram posts and reels are only listed (raw["instagram"], with whose ascent each was given for): Instagram
  shows them to nobody who isn't signed in, and its terms forbid collecting them, so they aren't fetched.
* Its position, from the site's own map (/api/v1/map_config, what the page's map asks): the climb's if it has
  one, else its crag's (coord_source "route" or "area").
* A list of the hardest climbs (/hardest?discipline=boulder&grade=8C%2B): each row's rank, climb, grade, crag and
  first ascent, 50 a page. Its climbs come as summaries (raw["stub"]): each one's page has the rest.

The site's terms (Sept 2025) let anyone browse it and ask only that automated traffic not load its servers
unreasonably; its robots.txt disallows neither climbs nor lists. Requests go through the shared client: cached, and
seconds apart. Only facts are kept (names, grades, positions, links): its contributors' text stays on its pages,
which every climb here links back to.

A crag holds many boulders, so a climb's lowest area (its crag) is never one boulder: the server marks it a group, and
its climbs are meshed on their own, or together where their photos match (a stand start and its sit start).
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from . import htmlmini
from .common import Climb, HttpError, Media, clean, http

SOURCE = "climbinghistory"
BASE = "https://climbing-history.org"
CLIMB_RE = re.compile(r"climbing-history\.org/climb/(\d+)(?:/([^/?#\s]*))?", re.I)
LIST_RE = re.compile(r"climbing-history\.org/hardest/?(?:[?#]|$)", re.I)
CRAG_RE = re.compile(r"/crag/(\d+)")
PER_PAGE = 50           # a list's rows a page
MAX_PAGES = 40          # of a list, at most: 2000 climbs
# the bouldering grades its pages give (Fontainebleau), as the V grades people search YouTube with
FONT_V = {"4": "V0", "4+": "V0", "5": "V1", "5+": "V2", "6A": "V3", "6A+": "V3", "6B": "V4", "6B+": "V4", "6C": "V5",
          "6C+": "V5", "7A": "V6", "7A+": "V7", "7B": "V8", "7B+": "V8", "7C": "V9", "7C+": "V10", "8A": "V11",
          "8A+": "V12", "8B": "V13", "8B+": "V14", "8C": "V15", "8C+": "V16", "9A": "V17", "9A+": "V18"}
YOUTUBE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^\"'\s<]*&)?v=|shorts/|embed/|live/)"
                     r"|youtu\.be/)([A-Za-z0-9_-]{11})([^\"'\s<]*)")
VIMEO = re.compile(r"https?://(?:www\.)?vimeo\.com/(\d+)")
INSTAGRAM = re.compile(r"https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/([A-Za-z0-9_-]+)")


def source_id(climb_id: str | int) -> str:
    """A climb's id here: the site's number with "ch" before it, so that it's never one of Mountain Project's."""
    return f"ch{climb_id}"


def climb_url(climb_id: str | int, slug: str | None = None) -> str:
    """A climb's page, its name in the link percent-encoded ("la-révolutionnaire")."""
    return f"{BASE}/climb/{climb_id}" + (f"/{quote(unquote(slug), safe='()-_.~')}" if slug else "")


def grades(text: str | None, boulder: bool = True) -> tuple[str | None, dict]:
    """A page's grade as shown here, and by scale: a boulder's Fontainebleau grade with its V grade before it ("V17 9A",
    as Mountain Project's read), anything else as it is. A grade between two ("8C+/9A") goes by its first."""
    text = clean(text)
    if not text:
        return None, {}
    if not boulder:
        return text, {}
    v = re.search(r"\bV\d+\b", text, re.I)
    if v:
        return text, {"vscale": v[0].upper()}
    m = re.match(r"([3-9][ABC]?\+?)", text.upper())
    if m and m[1] in FONT_V:
        return f"{FONT_V[m[1]]} {text}", {"vscale": FONT_V[m[1]], "font": text}
    return text, {"font": text}


def _start(rest: str) -> float | None:
    """The moment a YouTube link points to (t=439, t=7m19s, start=439), in seconds; None without one."""
    m = re.search(r"[?&#](?:t|start)=([0-9hms]+)", rest or "")
    if not m:
        return None
    if m[1].isdigit():
        return float(m[1])
    parts = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", m[1])
    if not parts or not any(parts.groups()):
        return None
    return float(int(parts[1] or 0) * 3600 + int(parts[2] or 0) * 60 + int(parts[3] or 0))


def _own_text(n: htmlmini.Node) -> str:
    """A node's own text, without its children's ("Burden of Dreams", not the grade and crag beside it)."""
    return clean(unescape("".join(c for c in n.children if isinstance(c, str)))) or ""


def _info(doc: htmlmini.Node) -> dict[str, htmlmini.Node]:
    """The page's "Quick Info" rows, by label in lower case: each one's value cell."""
    out: dict[str, htmlmini.Node] = {}
    for tr in doc.find_all("tr", cls="climber-info-item"):
        tds = [c for c in tr.children if isinstance(c, htmlmini.Node) and c.tag == "td"]
        if len(tds) == 2:
            out.setdefault(tds[0].text().lower(), tds[1])
    return out


def _ascents(doc: htmlmini.Node) -> tuple[list[dict], dict[str, dict]]:
    """A page's ascents in its order, [{climber, climber_url, date, style, note, done}], and each by its row's id (which
    its notes' row carries too)."""
    out, by_row = [], {}
    for tr in doc.find_all("tr", cls="collapsible-row"):
        if "ascent" not in tr.classes:
            continue
        tds = [c for c in tr.children if isinstance(c, htmlmini.Node) and c.tag == "td"]
        if len(tds) < 4:
            continue
        who = tds[1].find("a")
        a = {"climber": tds[1].text() or None, "style": tds[2].text() or None,
             "date": tds[3].get("data-sort-value") or tds[3].text() or None, "done": "dnf" not in tr.classes,
             "climber_url": BASE + who.get("href") if who is not None and (who.get("href") or "").startswith("/climber/") else None}
        out.append(a)
        by_row[tr.get("data-row-id") or ""] = a
    for small in doc.find_all("small", cls="ascent-subtitle"):
        tr, prev = small, None
        while tr is not None and tr.tag != "tr":
            tr = tr.parent
        if tr is None or tr.parent is None:
            continue
        for sib in tr.parent.children:      # the ascent's own row is the one before its subtitle's
            if sib is tr:
                break
            if isinstance(sib, htmlmini.Node) and "collapsible-row" in sib.classes:
                prev = sib
        if prev is not None and (prev.get("data-row-id") or "") in by_row:
            by_row[prev.get("data-row-id")]["note"] = small.text()
    return out, by_row


def parse_climb(html: str, url: str) -> Climb:
    """A climb's page as a record, without its position (locate())."""
    doc = htmlmini.parse(html)
    m = CLIMB_RE.search(url)
    cid = m[1] if m else "?"
    h1 = doc.find("h1")
    span = h1.find("span") if h1 is not None else None
    name = (_own_text(span) if span is not None else "").rstrip("| ").strip()
    if not name:
        title = doc.find("title")
        name = re.sub(r"\s*\|\s*Climbing History\s*$", "", title.text() if title is not None else "") or f"climb {cid}"
    info = _info(doc)
    kind_said = info["type"].text() if "type" in info else ""
    kind = "boulder" if "boulder" in kind_said.lower() or not kind_said else "route"
    grade, by_scale = grades(info["grade"].text() if "grade" in info else None, kind == "boulder")
    crag_a = info["crag"].find("a") if "crag" in info else None
    crag = crag_a.text() if crag_a is not None else (info["crag"].text() if "crag" in info else None)
    crag_url = BASE + crag_a.get("href") if crag_a is not None and (crag_a.get("href") or "").startswith("/") else None
    above = [a.text() for h6 in doc.find_all("h6") for a in h6.find_all("a") if (a.get("href") or "").startswith("/crags/location/")]
    above = list(dict.fromkeys(above))
    country = re.sub(r"[^\w\s().,'-]+", "", info["country"].text()).strip() if "country" in info else None
    if not above and country:
        above = [country]
    fa_cell = info.get("first ascent")
    fa = None
    if fa_cell is not None:
        who = ", ".join(a.text() for a in fa_cell.find_all("a")) or _own_text(fa_cell)
        when = fa_cell.find("span")
        fa = clean(f"{who}, {when.text().strip('()')}" if when is not None and who else who or (when.text() if when is not None else ""))
    ascents, by_row = _ascents(doc)
    links = {a.text(): a.get("href") for d in doc.find_all("div", cls="external-links") for a in d.find_all("a") if a.get("href")}
    aka = next((em.text() for sp in doc.find_all("span", cls="text-muted") if sp.text().startswith("Also known as")
                for em in sp.find_all("em")), None)

    # every link out of the page's own text, with the ascent it was given for: those in an ascent's notes, then the rest
    body = h1.parent if h1 is not None and h1.parent is not None else doc
    found: list[tuple[dict | None, str]] = []
    for a in body.find_all("a"):
        href = a.get("href") or ""
        if not href.startswith("http"):
            continue
        row, n = None, a.parent
        while n is not None:
            if n.tag == "tr" and "ascent-detail-row" in n.classes:
                row = by_row.get(n.get("data-row-id") or "")
                break
            if n.tag == "div" and "external-links" in n.classes:
                href = ""
                break
            n = n.parent
        if href:
            found.append((row, href))
    found.sort(key=lambda f: f[0] is None)      # stable: ascents' first, in the page's order
    videos, instagram, seen = [], [], set()
    for asc, href in found:
        who = f"{asc['climber']}{', ' + asc['date'] if asc.get('date') else ''}" if asc and asc.get("climber") else None
        yt, vm, ig = YOUTUBE.match(href), VIMEO.match(href), INSTAGRAM.match(href)
        if yt:
            start = _start(yt[2])
            if yt[1] in seen:       # given twice: for two ascents, or at two moments (the first one's start stands)
                continue
            seen.add(yt[1])
            videos.append(Media(url=f"https://www.youtube.com/watch?v={yt[1]}", kind="video", page=url, source=SOURCE,
                                credit=asc.get("climber") if asc else None, title=who, start=start))
        elif vm and vm[0] not in seen:
            seen.add(vm[0])
            videos.append(Media(url=vm[0], kind="video", page=url, source=SOURCE, credit=asc.get("climber") if asc else None,
                                title=who))
        elif ig and ig[1] not in seen:
            seen.add(ig[1])
            instagram.append({"url": href.split("?")[0], "climber": asc.get("climber") if asc else None,
                              "date": asc.get("date") if asc else None, **({"climber_url": asc["climber_url"]} if asc and asc.get("climber_url") else {})})
    crag_id = CRAG_RE.search(crag_url or "")
    return Climb(source=SOURCE, source_id=source_id(cid), url=url, name=name, kind=kind, grade=grade, grades=by_scale, fa=fa,
                 area_path=above + ([crag] if crag else []), area_urls=[crag_url] if crag_url else [],
                 search_place=crag, videos=videos, cross_refs={"climbinghistory_id": cid, **({"8a": links["8a.nu"]} if "8a.nu" in links else {})},
                 raw={"crag": crag, "crag_id": crag_id[1] if crag_id else None, "country": country, "aka": aka,
                      "rock": info["rock type"].text() if "rock type" in info else None, "links": links,
                      "ascents": ascents, "instagram": instagram, "has_map": f"map_config?climb_id={cid}" in html})


def _position(query: str, fresh: bool = False) -> tuple[float, float] | None:
    """What the site's map shows for a climb or crag (map_config?<query>), or None if it has no position."""
    try:
        got = http.get_json(f"{BASE}/api/v1/map_config?{query}", **({"ttl": 0} if fresh else {}))
    except HttpError as e:
        if e.status in (400, 404):      # "This climb has no location to map"
            return None
        raise
    lat, lng = got.get("latitude"), got.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)) or abs(lat) > 90 or abs(lng) > 180 or (lat == 0 and lng == 0):
        return None
    return float(lat), float(lng)


def locate(c: Climb, fresh: bool = False) -> None:
    """Give a climb read from its page its position: its own where its page has a map, else its crag's. A failure
    here only leaves it without one (raw["position_error"])."""
    cid = c.cross_refs.get("climbinghistory_id")
    try:
        ll = _position(f"climb_id={cid}", fresh) if cid and c.raw.get("has_map") else None
        if ll:
            c.lat, c.lng, c.coord_source = ll[0], ll[1], "route"
        elif c.raw.get("crag_id"):
            ll = _position(f"crag_id={c.raw['crag_id']}", fresh)
            if ll:
                c.lat, c.lng, c.coord_source = ll[0], ll[1], "area"
    except Exception as e:  # noqa: BLE001 - its page is still worth having
        c.raw["position_error"] = f"{type(e).__name__}: {e}"


def fetch(url: str, include_children: bool = True, fresh: bool = False, **_) -> list[Climb]:
    """The record for a climb's link.

    Args:
        fresh: ask the site again rather than use a copy cached in the last 30 days (its ascents and their links
            are added to as the climb is repeated).

    Raises:
        ValueError: for a link that isn't a climb's (a list of its hardest climbs is an area's job: list_key()).
    """
    m = CLIMB_RE.search(url)
    if not m:
        raise ValueError("not a climbing-history.org climb link (climbing-history.org/climb/<number>/<name>)")
    page = climb_url(m[1], m[2])
    c = parse_climb(http.get_text(page, **({"ttl": 0} if fresh else {})), page)
    locate(c, fresh)
    return [c]


# ------------------------------------------------------------------ lists of the hardest climbs
def list_query(url: str) -> dict | None:
    """What a link to a list of the hardest climbs asks for, {discipline, grade, country}, or None if it isn't one."""
    if not LIST_RE.search(url or ""):
        return None
    qs = parse_qs(urlparse(url if "://" in url else "https://" + url).query)
    one = lambda k, d="": (qs.get(k) or [d])[0].strip()  # noqa: E731
    return {"discipline": one("discipline", "boulder").lower(), "grade": one("grade"), "country": one("country").upper()}


def list_key(url: str) -> str | None:
    """A list's id here, the same however its link was written ("ch:hardest:boulder:8C+:"); None for any other link."""
    ask = list_query(url)
    return f"ch:hardest:{ask['discipline']}:{ask['grade']}:{ask['country']}" if ask else None


def list_url(key: str, page: int = 1) -> str:
    """The link of a list's page, from its id (list_key())."""
    _, _, discipline, grade, country = (key.split(":") + ["", "", ""])[:5]
    ask = {"discipline": discipline, **({"grade": grade} if grade else {}), **({"country": country} if country else {}),
           **({"page": page} if page > 1 else {})}
    return f"{BASE}/hardest?{urlencode(ask)}"


def parse_list(html: str) -> tuple[str | None, list[Climb]]:
    """A list page's title, and its rows in order as summaries of their climbs (raw["stub"]; raw["rank"] as the page
    numbers them)."""
    doc = htmlmini.parse(html)
    h1 = doc.find("h1")
    out = []
    for tr in doc.find_all("tr"):
        tds = [c for c in tr.children if isinstance(c, htmlmini.Node) and c.tag == "td"]
        a = tds[1].find("a") if len(tds) >= 5 else None
        m = CLIMB_RE.search("climbing-history.org" + (a.get("href") or "")) if a is not None else None
        if not m:
            continue
        boulder = True
        grade, by_scale = grades(tds[2].text(), boulder)
        crag_a = tds[3].find("a")
        crag_id = CRAG_RE.search(crag_a.get("href") or "") if crag_a is not None else None
        who = ", ".join(x.text() for x in tds[4].find_all("a"))
        when = tds[4].find("span")
        rank = tds[0].text()
        out.append(Climb(source=SOURCE, source_id=source_id(m[1]), url=climb_url(m[1], m[2]), name=a.text(), kind="boulder",
                         grade=grade, grades=by_scale, search_place=crag_a.text() if crag_a is not None else None,
                         fa=clean(f"{who}, {when.text().strip('()')}" if when is not None and who else who) or None,
                         cross_refs={"climbinghistory_id": m[1]},
                         raw={"stub": True, "rank": int(rank) if rank.isdigit() else None,
                              "crag": crag_a.text() if crag_a is not None else None, "crag_id": crag_id[1] if crag_id else None,
                              "ascents": int(tds[5].text()) if len(tds) > 5 and tds[5].text().isdigit() else None}))
    return (h1.text() if h1 is not None else None), out


def fetch_list(key: str, fresh: bool = False) -> tuple[str, list[Climb]]:
    """A list of the hardest climbs, whole, hardest first as the site ranks them: its name, and its climbs' summaries.
    A request a page of 50, up to MAX_PAGES.

    Raises:
        ValueError: for a list that isn't of boulder problems, the only kind meshed.
    """
    parts = key.split(":")
    if len(parts) < 3 or parts[2] != "boulder":
        raise ValueError("only its lists of boulder problems are meshed (discipline=boulder in the link)")
    title, out, seen = None, [], set()
    for page in range(1, MAX_PAGES + 1):
        t, rows = parse_list(http.get_text(list_url(key, page), **({"ttl": 0} if fresh else {})))
        title = title or t
        new = [c for c in rows if c.source_id not in seen]
        seen |= {c.source_id for c in new}
        out += new
        if len(rows) < PER_PAGE or not new:
            break
    grade, country = (parts + ["", ""])[3:5]
    name = (title or "Hardest boulder problems") + (f", {grade} and harder" if grade else "") + (f" ({country})" if country and not title else "")
    return name, out
