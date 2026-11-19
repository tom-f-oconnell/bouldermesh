"""Which Mountain Project area a GPS position is in, from a local index of areas and where they are.

Mountain Project has no API, nor any search by position, and its robots.txt asks for 60 s between pages, so areas
are looked up in an index kept here (DATA/areas.sqlite), filled ahead of time from two sources:

  openbeta  OpenBeta's whole tree (bulkAreas, a page of 2000 areas a request: ~40 minutes for all of it). It was
            seeded from Mountain Project, and most of its lowest areas carry their MP id; areas above them often
            don't. Almost all of it is in the US.
  mp        Mountain Project's own pages, from a root area down (crawl_mp()): one page a minute, so for a region at
            a time. It's the check on OpenBeta's copy (compare()), and the only source outside the US.

match() finds the lowest areas with climbs nearest the position, goes up from each to the area holding them (the
area of boulders, for a boulder) and from there one more, to what an upload is put in: the parent of the area holding
the boulders. Sibling areas of one place (MP often has "X" for its routes and "X Bouldering" for its problems) give
several candidates; the one listing the most boulder problems is chosen, and the rest are kept, for its uploader to
pick from instead.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path

NEAR_KM = 2.0       # the lowest areas with climbs this close to a position are candidates; none, and it's in no area
CLOSE_KM = 0.2      # and those within this of the nearest (or twice as far as it), the places it may be in
MOST = 5            # candidates kept, nearest first
UP = 2              # levels from a lowest area (a boulder, as a rule) to where an upload is put
TOP = 2             # never higher than this many levels from the top (a country or US state, and the next)
EARTH_KM = 6371.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS areas(
  src TEXT, id TEXT,            -- openbeta: its uuid; mp: its MP id
  mp_id TEXT,                   -- its Mountain Project id, if known ('' or NULL if not)
  name TEXT, path TEXT,         -- names from the top down, itself last (JSON)
  ids TEXT,                     -- ids (of src) from the top down, itself last (JSON)
  lat REAL, lng REAL, leaf INTEGER,
  boulders INTEGER, routes INTEGER,   -- climbs under it: boulder problems, and the rest
  checked REAL,
  children TEXT,                -- src mp: its child areas' ids (JSON), so that a crawl stopped part way carries on
  PRIMARY KEY(src, id));
CREATE INDEX IF NOT EXISTS areas_lat ON areas(lat);
CREATE INDEX IF NOT EXISTS areas_mp ON areas(mp_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    if "children" not in {r[1] for r in con.execute("PRAGMA table_info(areas)")}:
        con.execute("ALTER TABLE areas ADD COLUMN children TEXT")
    return con


def km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def put(con: sqlite3.Connection, rows: list[dict]) -> int:
    """Add or replace index rows ({src, id, mp_id, name, path, ids, lat, lng, leaf, boulders, routes})."""
    now = time.time()
    con.executemany("INSERT OR REPLACE INTO areas(src,id,mp_id,name,path,ids,lat,lng,leaf,boulders,routes,checked,children) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(r["src"], str(r["id"]), str(r.get("mp_id") or "") or None, r["name"], json.dumps(r["path"]),
                      json.dumps([str(i) for i in r["ids"]]), r.get("lat"), r.get("lng"), int(bool(r.get("leaf"))),
                      int(r.get("boulders") or 0), int(r.get("routes") or 0), now,
                      json.dumps(r["children"]) if r.get("children") is not None else None) for r in rows])
    con.commit()
    return len(rows)


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["path"], d["ids"] = json.loads(d["path"] or "[]"), json.loads(d["ids"] or "[]")
    return d


def near(con: sqlite3.Connection, lat: float, lng: float, radius_km: float, leaf: bool | None = None) -> list[dict]:
    """Areas within radius_km of a position, nearest first, each with its "km"."""
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(0.01, math.cos(math.radians(lat))))
    sql = "SELECT * FROM areas WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?" + ("" if leaf is None else " AND leaf=?")
    args = [lat - dlat, lat + dlat, lng - dlng, lng + dlng] + ([] if leaf is None else [int(leaf)])
    out = []
    for r in con.execute(sql, args):
        d = _row(r)
        d["km"] = km(lat, lng, d["lat"], d["lng"])
        if d["km"] <= radius_km:
            out.append(d)
    return sorted(out, key=lambda d: d["km"])


def get(con: sqlite3.Connection, src: str, id: str) -> dict | None:
    r = con.execute("SELECT * FROM areas WHERE src=? AND id=?", (src, str(id))).fetchone()
    return _row(r) if r else None


def by_mp(con: sqlite3.Connection, mp_id: str) -> dict | None:
    """An area by its MP id: Mountain Project's own record if the index has it, else OpenBeta's."""
    r = con.execute("SELECT * FROM areas WHERE (src='mp' AND id=?) OR (src='openbeta' AND mp_id=?) ORDER BY src='mp' DESC LIMIT 1",
                    (str(mp_id), str(mp_id))).fetchone()
    return _row(r) if r else None


def plain(name: str | None) -> str:
    """A name as both sources would agree on it: OpenBeta has "Wall, The" and "*X Bouldering*" for MP's "The Wall" and
    "* X Bouldering"."""
    n = re.sub(r"[*]", "", name or "").strip().lower()
    m = re.fullmatch(r"(.*), (the|a|an)", n)
    if m:
        n = f"{m[2]} {m[1]}"
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def up_from(leaf: dict) -> int:
    """Where in a lowest area's ids (an index) the area an upload by it goes in is."""
    return min(len(leaf["ids"]) - 1, max(TOP, len(leaf["ids"]) - 1 - UP))


def candidates(con: sqlite3.Connection, lat: float, lng: float) -> list[dict]:
    """The areas a position may be in, at most MOST, none holding another: [{src, id, mp_id, name, path, boulders,
    routes, km (to the nearest of its lowest areas), up (ids above it), via (that lowest area: {src, id, mp_id, name,
    depth})}]. Mountain Project's own record of a place is used where the index has it; OpenBeta's otherwise."""
    leaves = [a for a in near(con, lat, lng, NEAR_KM, leaf=True) if a["boulders"] or a["routes"]]
    if not leaves:
        return []
    d0 = leaves[0]["km"]
    close = [a for a in leaves if a["km"] <= max(d0 + CLOSE_KM, 2 * d0)]
    # one source for the lot: Mountain Project's, where it has the nearest
    src = "mp" if close[0]["src"] == "mp" else "openbeta"
    close = [a for a in close if a["src"] == src] or close
    out: dict[str, dict] = {}
    for a in close:
        i = up_from(a)
        aid = a["ids"][i]
        if aid in out:
            continue
        area = get(con, a["src"], aid) or {"src": a["src"], "id": aid, "mp_id": None, "name": a["path"][i], "path": a["path"][:i + 1],
                                           "boulders": 0, "routes": 0}
        out[aid] = {k: area.get(k) for k in ("src", "id", "mp_id", "name", "path", "boulders", "routes")} | {
            "km": round(a["km"], 3), "up": a["ids"][:i], "via": {"src": a["src"], "id": a["id"], "mp_id": a["mp_id"], "name": a["name"],
                                                                 "depth": len(a["ids"]) - 1 - i}}
    # an area holding another candidate is the same place, said more coarsely (Buttermilk Country, of Buttermilks Main)
    above = {u for c in out.values() for u in c["up"]}
    return [c for c in out.values() if c["id"] not in above][:MOST]


def mp_chain(con: sqlite3.Connection, cand: dict, fetch_page=None) -> tuple[str | None, list[str]]:
    """A candidate's Mountain Project id, and the MP ids above it, top first, as far as they're known: from the index's
    own record of Mountain Project (src mp) if it has one, or else that of the candidate's lowest area (via), going up as
    many levels, by the index or else by that area's page (fetch_page(mp_id) -> (names, ids) of its breadcrumb, top
    first, itself not included). The name there has to agree, or else one of that name further up is taken."""
    if cand.get("src") == "mp":
        return cand["id"], [str(i) for i in (get(con, "mp", cand["id"]) or {}).get("ids", [])[:-1]]
    via = cand["via"]
    if not via.get("mp_id"):
        return cand.get("mp_id"), []
    depth = via["depth"]
    own = by_mp(con, via["mp_id"])
    if own and own["src"] == "mp":
        names, ids = own["path"][:-1], own["ids"][:-1]
    elif fetch_page:
        names, ids = fetch_page(via["mp_id"])
    else:
        return cand.get("mp_id"), []
    if cand.get("mp_id") in ids:
        return cand["mp_id"], ids[:ids.index(cand["mp_id"])]
    if 1 <= depth <= len(ids) and plain(names[-depth]) == plain(cand["name"]):
        return ids[-depth], ids[:-depth]
    for k, n in enumerate(names):   # the trees differ in depth here: the one of that name
        if plain(n) == plain(cand["name"]):
            return ids[k], ids[:k]
    return cand.get("mp_id"), []


def bouldering(c: dict) -> bool:
    return (c.get("boulders") or 0) >= (c.get("routes") or 0) and (c.get("boulders") or 0) > 0


def match(con: sqlite3.Connection, lat: float, lng: float, fetch_page=None) -> dict:
    """What a position is in: {choice, choices (all, the choice first), why}; choice None if no area with climbs is
    within NEAR_KM. Of areas close by, those listing more boulder problems than routes come first (where Mountain
    Project has one area for a place's routes and another for its problems), and of those, the nearest. The choice
    has its "mp_id" and the Mountain Project ids above it ("mp_up", top first) filled in, as far as they're known, and so
    do the others (fetch_page is cached, and their lowest areas are often on one page's breadcrumb)."""
    cands = candidates(con, lat, lng)
    if not cands:
        return {"choice": None, "choices": [], "why": f"no area with climbs within {NEAR_KM:g} km in the index"}
    cands.sort(key=lambda c: (not bouldering(c), c["km"]))
    for c in cands:
        try:
            c["mp_id"], c["mp_up"] = mp_chain(con, c, fetch_page)
        except Exception as e:     # a page that couldn't be read: known by name only, for now
            c["mp_up"], c["mp_error"] = [], f"{type(e).__name__}: {e}"
    best = cands[0]
    why = f"{best['name']}: {UP} levels above {best['via']['name']}, an area of climbs {best['km'] * 1000:.0f} m away"
    if len(cands) > 1:
        why += (f"; of {len(cands)} areas close by, " + ("the nearest of those listing more boulder problems than routes"
                                                        if bouldering(best) else "the nearest"))
    return {"choice": best, "choices": cands, "why": why}


# ------------------------------------------------------------------------------------------------------- filling it
OB_FIELDS = "uuid area_name pathTokens ancestors totalClimbs metadata { lat lng mp_id leaf } aggregate { byDiscipline { bouldering { total } } }"


def _ob_row(a: dict) -> dict | None:
    md = a.get("metadata") or {}
    if md.get("lat") is None or md.get("lng") is None:
        return None
    # a climb counts under each of its disciplines (a problem may be "tr" too): the rest of its total are routes
    boulders = (((a.get("aggregate") or {}).get("byDiscipline") or {}).get("bouldering") or {}).get("total") or 0
    return {"src": "openbeta", "id": a["uuid"], "mp_id": md.get("mp_id") or None, "name": a.get("area_name") or "?",
            "path": list(a.get("pathTokens") or []), "ids": list(a.get("ancestors") or [a["uuid"]]),
            "lat": md["lat"], "lng": md["lng"], "leaf": bool(md.get("leaf")),
            "boulders": boulders, "routes": max(0, (a.get("totalClimbs") or 0) - boulders)}


def crawl_openbeta(con: sqlite3.Connection, gql, only: list[str] | None = None, page: int = 2000, log=print,
                   tries: int = 4, wait: float = 20.0) -> tuple[int, list[str]]:
    """Put OpenBeta's areas in the index: each country's, a page of `page` at a time, each of its children's trees
    kept as it's done. Its API fails now and then (502s): a page is tried `tries` times, `wait` s and more apart, and a
    tree that still fails is left out and named, to try again later.

    Args:
        gql: openbeta.gql (query, variables, use_cache) -> data.
        only: country names (as OpenBeta has them: "USA", "Canada") to do; None for all.

    Returns:
        How many areas were put in, and the trees left out ("Country / Child").
    """
    def ask(query, variables):
        for k in range(tries):
            try:
                return gql(query, variables, False)
            except Exception as e:
                if k == tries - 1:
                    raise
                log(f"openbeta: {type(e).__name__}: {e}; trying again in {wait * (k + 1):.0f} s")
                time.sleep(wait * (k + 1))

    countries = ask("{ countries { uuid area_name children { uuid area_name } } }", None).get("countries") or []
    n, failed = 0, []
    for c in countries:
        if only and c["area_name"] not in only:
            continue
        try:
            top = (ask(f"query($id: ID) {{ area(uuid: $id) {{ {OB_FIELDS} }} }}", {"id": c["uuid"]}) or {}).get("area")
            n += put(con, [r for r in [_ob_row(top)] if r] if top else [])
        except Exception as e:
            log(f"openbeta: {c['area_name']} itself left out: {type(e).__name__}: {e}")
            failed.append(c["area_name"])
        for child in c.get("children") or []:
            rows, offset = [], 0
            try:
                while True:
                    got = ask(f"query($a: [String!]!, $l: Int, $o: Int) {{ bulkAreas(ancestors: $a, limit: $l, offset: $o) {{ {OB_FIELDS} }} }}",
                              {"a": [c["uuid"], child["uuid"]], "l": page, "o": offset}).get("bulkAreas") or []
                    rows += [r for r in map(_ob_row, got) if r]
                    offset += len(got)
                    if len(got) < page:
                        break
            except Exception as e:
                log(f"openbeta: {c['area_name']} / {child['area_name']} left out: {type(e).__name__}: {e}")
                failed.append(f"{c['area_name']} / {child['area_name']}")
                continue
            n += put(con, rows)
            log(f"openbeta: {c['area_name']} / {child['area_name']}: {offset} areas")
    return n, failed


def mp_row(area, routes: list, children: list) -> dict:
    """An index row from a parsed MP area page (mountainproject.parse_area())."""
    ids = [re.search(r"/area/(\d+)", u)[1] for u in area.area_urls]
    graded = [g for _, _, g in routes if g]     # the area's own table; links from its description may be elsewhere
    boulders = sum(1 for g in graded if re.match(r"V(\d|-easy|\?)", g))
    return {"src": "mp", "id": area.source_id, "mp_id": area.source_id, "name": area.name, "path": area.area_path, "ids": ids,
            "lat": area.lat, "lng": area.lng, "leaf": not children, "boulders": boulders, "routes": len(graded) - boulders,
            "children": [re.search(r"/area/(\d+)", u)[1] for u, _ in children]}


def crawl_mp(con: sqlite3.Connection, root: str, read_area, max_pages: int = 100, fresh_days: float = 30, log=print) -> int:
    """Put Mountain Project's areas under root (an area id) in the index, a page each, children before their siblings'
    children (so that what's done when it stops is whole near the top). Areas checked in the last fresh_days are
    gone through again from the index, without their pages: so it carries on where it stopped. Counts of climbs above
    the lowest areas are their lowest areas' sums.

    Args:
        read_area: mountainproject's (url) -> parse_area() result, through its throttle (a page a minute).
        max_pages: stop after reading this many pages (None: no limit).
    """
    todo, read, seen = [str(root)], 0, set()
    while todo:
        aid = todo.pop(0)
        if aid in seen:
            continue
        seen.add(aid)
        r = con.execute("SELECT checked, children FROM areas WHERE src='mp' AND id=?", (aid,)).fetchone()
        if r and r["checked"] and r["children"] is not None and time.time() - r["checked"] < fresh_days * 86400:
            todo += json.loads(r["children"])
            continue
        if max_pages is not None and read >= max_pages:
            log(f"mp: stopped after {read} pages; {len(todo) + 1} areas still to read")
            break
        area, routes, children = read_area(f"https://www.mountainproject.com/area/{aid}")
        read += 1
        row = mp_row(area, routes, children)
        put(con, [row])
        log(f"mp: {' / '.join(area.area_path[-3:])}: {len(children)} areas, {len(routes)} climbs")
        todo += row["children"]
    sum_up(con, "mp")
    return read


def sum_up(con: sqlite3.Connection, src: str) -> None:
    """Areas above the lowest of a source take their climbs' counts from the lowest under them."""
    totals: dict[str, list[int]] = {}
    for r in con.execute("SELECT ids, boulders, routes FROM areas WHERE src=? AND leaf=1", (src,)):
        for a in json.loads(r["ids"])[:-1]:
            t = totals.setdefault(a, [0, 0])
            t[0] += r["boulders"]
            t[1] += r["routes"]
    con.executemany("UPDATE areas SET boulders=?, routes=? WHERE src=? AND id=? AND leaf=0",
                    [(b, r, src, a) for a, (b, r) in totals.items()])
    con.commit()


def compare(con: sqlite3.Connection, far_km: float = 1.0) -> dict:
    """Mountain Project's own record of areas (src mp) against OpenBeta's copy of them, by MP id: {mp (areas read from
    MP), matched, missing (MP areas OpenBeta has no copy of), moved (copies over far_km off), renamed, moved_to (copies
    whose parent differs)}; each list of {mp_id, name, ...}."""
    mp = [_row(r) for r in con.execute("SELECT * FROM areas WHERE src='mp'")]
    ob = {r["mp_id"]: _row(r) for r in con.execute("SELECT * FROM areas WHERE src='openbeta' AND mp_id IS NOT NULL")}
    ob_by_uuid = {}
    out = {"mp": len(mp), "matched": 0, "missing": [], "moved": [], "renamed": [], "moved_to": []}
    for a in mp:
        o = ob.get(a["id"])
        if not o:
            out["missing"].append({"mp_id": a["id"], "name": a["name"], "path": a["path"], "leaf": a["leaf"]})
            continue
        out["matched"] += 1
        if a["lat"] is not None and o["lat"] is not None and (d := km(a["lat"], a["lng"], o["lat"], o["lng"])) > far_km:
            out["moved"].append({"mp_id": a["id"], "name": a["name"], "km": round(d, 2)})
        if plain(a["name"]) != plain(o["name"]):
            out["renamed"].append({"mp_id": a["id"], "name": a["name"], "openbeta": o["name"]})
        if len(a["ids"]) > 1 and len(o["ids"]) > 1:
            parent = o["ids"][-2]
            if parent not in ob_by_uuid:
                ob_by_uuid[parent] = get(con, "openbeta", parent)
            p = ob_by_uuid[parent]
            if p and p.get("mp_id") and p["mp_id"] != a["ids"][-2]:
                out["moved_to"].append({"mp_id": a["id"], "name": a["name"], "mp_parent": a["ids"][-2], "openbeta_parent": p["mp_id"]})
    return out
