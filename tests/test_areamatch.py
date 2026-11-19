#!/usr/bin/env python3
"""Uploads put in areas by their photos' GPS (areamatch.py, server.match_area()): offline, on a small made-up tree of
areas as OpenBeta and Mountain Project have it, with no page read from either.

    python3 tests/test_areamatch.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = pathlib.Path(tempfile.mkdtemp(prefix="bouldermesh-areamatch-"))
os.environ["DATA_DIR"] = str(tmp)

from PIL import Image  # noqa: E402

import areamatch  # noqa: E402

# A place with one area for its routes and another for its problems (as Mountain Project often has), 30 m apart, and a
# second place of problems 1.5 km away. OpenBeta's copy: ids are uuids here, MP ids only on its lowest areas.
LAT, LNG = 34.0, -116.0
M = 1 / 111_000     # degrees of latitude a metre


def ob(id, path, ids, lat, lng, leaf, boulders, routes, mp=None):
    return {"src": "openbeta", "id": id, "mp_id": mp, "name": path[-1], "path": path, "ids": ids, "lat": lat, "lng": lng,
            "leaf": leaf, "boulders": boulders, "routes": routes}


ROWS = [
    ob("usa", ["USA"], ["usa"], 39, -98, False, 900, 9000),
    ob("ca", ["USA", "California"], ["usa", "ca"], 37, -120, False, 500, 5000),
    ob("park", ["USA", "California", "The Park"], ["usa", "ca", "park"], LAT, LNG, False, 60, 400),
    # its routes: The Park > Hidden Area > Campground > The Wall (a crag of routes)
    ob("hidden", ["USA", "California", "The Park", "Hidden Area"], ["usa", "ca", "park", "hidden"], LAT, LNG, False, 1, 300),
    ob("camp", ["USA", "California", "The Park", "Hidden Area", "Campground"], ["usa", "ca", "park", "hidden", "camp"], LAT, LNG, False, 1, 300),
    ob("wall", ["USA", "California", "The Park", "Hidden Area", "Campground", "Wall, The"], ["usa", "ca", "park", "hidden", "camp", "wall"],
       LAT + 30 * M, LNG, True, 1, 300, mp="2001"),
    # its problems: The Park > *Park Bouldering* > Hidden Bouldering > Sector A > Egg Boulder
    ob("pb", ["USA", "California", "The Park", "*Park Bouldering*"], ["usa", "ca", "park", "pb"], LAT, LNG, False, 59, 0),
    ob("hb", ["USA", "California", "The Park", "*Park Bouldering*", "Hidden Bouldering"], ["usa", "ca", "park", "pb", "hb"], LAT, LNG, False, 40, 0),
    ob("sa", ["USA", "California", "The Park", "*Park Bouldering*", "Hidden Bouldering", "Sector A"], ["usa", "ca", "park", "pb", "hb", "sa"],
       LAT, LNG, False, 40, 0),
    ob("egg", ["USA", "California", "The Park", "*Park Bouldering*", "Hidden Bouldering", "Sector A", "Egg Boulder"],
       ["usa", "ca", "park", "pb", "hb", "sa", "egg"], LAT, LNG, True, 12, 0, mp="3001"),
    # a rock right in The Park: two levels up is The Park itself, which holds the other two, so it's no choice of its own
    ob("pebble", ["USA", "California", "The Park", "Pebble"], ["usa", "ca", "park", "pebble"], LAT + 5 * M, LNG, True, 1, 0, mp="5001"),
    # 1.5 km north: Far Bouldering > Far Sector > Far Boulder
    ob("fb", ["USA", "California", "The Park", "*Park Bouldering*", "Far Bouldering"], ["usa", "ca", "park", "pb", "fb"], LAT + 1500 * M, LNG, False, 19, 0),
    ob("fs", ["USA", "California", "The Park", "*Park Bouldering*", "Far Bouldering", "Far Sector"], ["usa", "ca", "park", "pb", "fb", "fs"],
       LAT + 1500 * M, LNG, False, 19, 0),
    ob("far", ["USA", "California", "The Park", "*Park Bouldering*", "Far Bouldering", "Far Sector", "Far Boulder"],
       ["usa", "ca", "park", "pb", "fb", "fs", "far"], LAT + 1500 * M, LNG, True, 19, 0, mp="4001"),
]
# Mountain Project's breadcrumbs (top first, the area itself not included), as its pages have them
CRUMBS = {"5001": (["California", "The Park"], ["10", "11"]),
          "3001": (["California", "The Park", "* Park Bouldering", "Hidden Bouldering", "Sector A"], ["10", "11", "12", "13", "14"]),
          "2001": (["California", "The Park", "Hidden Area", "Campground"], ["10", "11", "20", "21"]),
          "4001": (["California", "The Park", "* Park Bouldering", "Far Bouldering", "Far Sector"], ["10", "11", "12", "15", "16"])}
pages: list[str] = []


def page(mp_id):
    pages.append(mp_id)
    return CRUMBS[mp_id]


con = areamatch.connect(tmp / "index.sqlite")
areamatch.put(con, ROWS)

assert areamatch.plain("Wall, The") == areamatch.plain("The Wall") and areamatch.plain("*Park Bouldering*") == areamatch.plain("* Park Bouldering")

# By the problems' boulder: its area is two levels up (the parent of Sector A, which holds the boulders); the routes'
# crag, 30 m off, is the other choice, after it, as it lists more routes than problems
m = areamatch.match(con, LAT, LNG, page)
names = [c["name"] for c in m["choices"]]
assert names == ["Hidden Bouldering", "Hidden Area"], (names, m["why"])
assert m["choice"]["mp_id"] == "13" and m["choice"]["mp_up"] == ["10", "11", "12"], m["choice"]
assert m["choices"][1]["mp_id"] == "20" and m["choices"][1]["mp_up"] == ["10", "11"], m["choices"][1]
assert "boulder problems" in m["why"], m["why"]
# nearer the routes' crag than the boulder still goes to the problems' area: they're close enough to be one place
m = areamatch.match(con, LAT + 28 * M, LNG, page)
assert [c["name"] for c in m["choices"]] == ["Hidden Bouldering", "Hidden Area"], m["choices"]
# the far boulder alone, with no other close by
m = areamatch.match(con, LAT + 1495 * M, LNG, page)
assert [c["name"] for c in m["choices"]] == ["Far Bouldering"] and m["choice"]["mp_id"] == "15", m["choices"]
# nothing within 2 km
assert areamatch.match(con, LAT - 5000 * M, LNG, page)["choice"] is None
# offline: the index has no Mountain Project ids above the lowest areas, so the choice has none
assert areamatch.match(con, LAT, LNG)["choice"]["mp_id"] is None
print("match OK")

# Mountain Project's own pages: read down from The Park, and checked against OpenBeta's copy
MP_TREE = {"11": ("The Park", ["California", "The Park"], ["10", "11"], LAT, LNG, ["12", "20"], []),
           "12": ("* Park Bouldering", ["California", "The Park", "* Park Bouldering"], ["10", "11", "12"], LAT, LNG, ["13"], []),
           "13": ("Hidden Bouldering", ["California", "The Park", "* Park Bouldering", "Hidden Bouldering"], ["10", "11", "12", "13"], LAT, LNG, ["3001", "3002"], []),
           "3001": ("Egg Boulder", None, None, LAT, LNG, [], [("V3", 6), ("V5", 6)]),
           "3002": ("New Boulder", None, None, LAT + 10 * M, LNG, [], [("V1", 2)]),      # added since OpenBeta's copy
           "20": ("Hidden Area", ["California", "The Park", "Hidden Area"], ["10", "11", "20"], LAT, LNG, ["21"], []),
           "21": ("Campground", ["California", "The Park", "Hidden Area", "Campground"], ["10", "11", "20", "21"], LAT, LNG, ["2001"], []),
           "2001": ("The Wall", None, None, LAT + 2 * 1000 * M, LNG, [], [("5.10a", 300), ("V0", 1)])}   # 2 km off in OpenBeta's copy


def read_area(url):
    aid = url.rsplit("/", 1)[1]
    name, path, ids, lat, lng, kids, routes = MP_TREE[aid]
    if path is None:
        parent = next(k for k, v in MP_TREE.items() if aid in v[5])
        path, ids = MP_TREE[parent][1] + [name], MP_TREE[parent][2] + [aid]
    area = types.SimpleNamespace(source_id=aid, name=name, area_path=path,
                                 area_urls=[f"https://www.mountainproject.com/area/{i}/x" for i in ids], lat=lat, lng=lng)
    rs = [(f"https://www.mountainproject.com/route/{aid}{k}/x", "r", g) for g, n in routes for k in range(n)]
    return area, rs, [(f"https://www.mountainproject.com/area/{k}/x", MP_TREE[k][0]) for k in kids]


assert areamatch.crawl_mp(con, "11", read_area, max_pages=3, log=lambda s: None) == 3
assert areamatch.crawl_mp(con, "11", read_area, max_pages=None, log=lambda s: None) == 5, "a run again reads only what's left"
hb = areamatch.get(con, "mp", "13")
assert (hb["boulders"], hb["routes"]) == (14, 0) and not hb["leaf"], hb
assert (areamatch.get(con, "mp", "2001")["boulders"], areamatch.get(con, "mp", "2001")["routes"]) == (1, 300)
diff = areamatch.compare(con)
assert sorted(x["mp_id"] for x in diff["missing"]) == ["11", "12", "13", "20", "21", "3002"], diff["missing"]
assert [x["mp_id"] for x in diff["moved"]] == ["2001"] and [x["mp_id"] for x in diff["renamed"]] == [], diff
# where Mountain Project's own record is nearest, it's used, and needs no page
pages.clear()
m = areamatch.match(con, LAT + 10 * M, LNG, page)
assert m["choice"]["src"] == "mp" and m["choice"]["name"] == "* Park Bouldering" and m["choice"]["mp_id"] == "12", m["choice"]
assert m["choice"]["mp_up"] == ["10", "11"] and not pages, (m["choice"], pages)
print("Mountain Project's own pages OK")

# The OpenBeta crawl: a country's top area, and each of its children's trees, a page at a time
calls = []


def gql(query, variables, cache):
    calls.append(variables)
    if "countries" in query:
        return {"countries": [{"uuid": "usa", "area_name": "USA", "children": [{"uuid": "ca", "area_name": "California"}]},
                              {"uuid": "fr", "area_name": "France", "children": []}]}
    node = lambda id, path, ids, leaf=False, b=0: {"uuid": id, "area_name": path[-1], "pathTokens": path, "ancestors": ids,
                                                  "totalClimbs": b + 1, "metadata": {"lat": 1.0, "lng": 2.0, "mp_id": "", "leaf": leaf},
                                                  "aggregate": {"byDiscipline": {"bouldering": {"total": b}}}}
    if "area(uuid" in query:
        return {"area": node("usa", ["USA"], ["usa"])}
    rows = [node("ca", ["USA", "California"], ["usa", "ca"]), node("x", ["USA", "California", "X"], ["usa", "ca", "x"], True, 3)]
    return {"bulkAreas": rows[variables["o"]:variables["o"] + variables["l"]]}


con2 = areamatch.connect(tmp / "ob.sqlite")
assert areamatch.crawl_openbeta(con2, gql, only=["USA"], page=1, log=lambda s: None) == (3, [])
x = areamatch.get(con2, "openbeta", "x")
assert x["leaf"] and (x["boulders"], x["routes"]) == (3, 1) and x["mp_id"] is None, x
# a page that fails is tried again; a tree that keeps failing is left out and named, and the rest goes on
flaky = {"n": 0}


def gql_flaky(query, variables, cache):
    if "bulkAreas" in query:
        flaky["n"] += 1
        if flaky["n"] <= 2:
            raise RuntimeError("HTTP 502")
    return gql(query, variables, cache)


con3 = areamatch.connect(tmp / "ob3.sqlite")
assert areamatch.crawl_openbeta(con3, gql_flaky, only=["USA"], page=1, log=lambda s: None, tries=3, wait=0) == (3, [])
assert areamatch.crawl_openbeta(con3, lambda q, v, c: gql(q, v, c) if "bulkAreas" not in q else 1 / 0, only=["USA"], page=1,
                                log=lambda s: None, tries=2, wait=0) == (1, ["USA / California"])
print("OpenBeta crawl OK")

# The server: an upload with GPS in its photos' EXIF is put in the area here that holds its place, or else in
# Mountain Project's; several close by are kept for its uploader to choose from; moderators may approve it only once a
# person has said where it is
import server  # noqa: E402

server.init_db()
server.AREA_INDEX = tmp / "server-index.sqlite"
areamatch.put(areamatch.connect(server.AREA_INDEX), ROWS)


def gps_photo(path, lat, lng):
    im = Image.new("RGB", (32, 32), (120, 110, 100))
    exif = Image.Exif()
    dms = lambda v: (int(abs(v)), int(abs(v) * 60 % 60), round(abs(v) * 3600 % 60, 4))
    exif[0x8825] = {1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lng >= 0 else "W", 4: dms(lng)}
    im.save(path, exif=exif)


def project(name, lat=None, lng=None, user=None):
    pid = server.new_id()
    (server.PROJECTS / pid / "photos").mkdir(parents=True)
    for k in range(3):
        if lat is None:
            Image.new("RGB", (32, 32)).save(server.PROJECTS / pid / "photos" / f"{k}.jpg")
        else:
            gps_photo(server.PROJECTS / pid / "photos" / f"{k}.jpg", lat + k * 2 * M, lng)
    server.q("INSERT INTO projects(id,name,created,kind,user_id,approved) VALUES(?,?,?,?,?,?)", pid, name, 1.0, "user", user, 0 if user else None, write=True)
    return pid


pos = server.project_position(project("where", LAT + 2 * M, LNG))
assert abs(pos["lat"] - (LAT + 4 * M)) < 1e-5 and abs(pos["lng"] - LNG) < 1e-5 and pos["n"] == 3, pos

pages.clear()
a = project("in the park", LAT, LNG, user="u1")
rec = server.match_area(a, fetch_page=page)
assert rec["auto"] and rec["name"] == "Hidden Bouldering" and rec["campaign"] is None and rec["mp_id"] == "13", rec
assert [c["name"] for c in rec["choices"]] == ["Hidden Bouldering", "Hidden Area"], rec["choices"]
v = server.area_view(server.q("SELECT area FROM projects WHERE id=?", a, one=True)["area"])
assert v["ask"] and v["name"] == "Hidden Bouldering" and v["url"].endswith("/area/13"), v
assert server.match_area(a, fetch_page=page) == rec, "done once"

# the site lists The Park (MP id 11): both choices are in it, so that's the one area, and nobody need be asked
server.q("INSERT INTO campaigns(id,name,area_id,ord,created,status) VALUES('camp1','The Park','11','popularity',1,'stopped')", write=True)
rec = server.match_area(a, again=True, fetch_page=page)
assert rec["campaign"] == "camp1" and len(rec["choices"]) == 1, rec
v = server.area_view(server.q("SELECT area FROM projects WHERE id=?", a, one=True)["area"])
assert v["name"] == "The Park" and v["campaign"] == "camp1" and not v["ask"], v
proj = server.q("SELECT * FROM projects WHERE id=?", a, one=True)
assert proj["area_campaign"] == "camp1" and server.areas_of_project(proj) == [], "not its moderators' until it's looked over"
assert [r["id"] for r in server.area_reviews()] == [a]
server.review_area(a)
assert server.areas_of_project(server.q("SELECT * FROM projects WHERE id=?", a, one=True)) == ["camp1"]
assert server.area_reviews() == []
print("an upload in an area listed here OK")

# a place the site doesn't list: its uploader picks the other close by, and that's no longer automatic
server.q("DELETE FROM campaigns WHERE id='camp1'", write=True)
b = project("by the wall", LAT + 28 * M, LNG, user="u1")
rec = server.match_area(b, fetch_page=page)
assert rec["auto"] and len(rec["choices"]) == 2
rec = server.choose_area(b, 1, {"name": "someone"})
assert not rec["auto"] and rec["name"] == "Hidden Area" and rec["mp_id"] == "20" and rec["chosen_by"] == "someone", rec
assert server.match_area(b, again=True, fetch_page=page)["name"] == "Hidden Area", "a person's choice stays"
assert server.capture_status([b], server.OWNER)[b]["area"]["name"] == "Hidden Area"
try:
    server.choose_area(b, 5, None)
    raise AssertionError("no such choice")
except ValueError:
    pass
# meshes made from it are listed in that area
server.q("INSERT INTO meshes(id,name,filename,source,created) VALUES('m1','x','x.glb',?,1)", f"project:{b}", write=True)
assert server.mesh_areas()({"id": "m1", "source": f"project:{b}", "climbs": []})[0] == ["Hidden Area"]

# none: no GPS in its photos; and a capture adding to a mesh needs none
c = project("no gps")
rec = server.match_area(c)
assert not rec.get("name") and "GPS" in rec["why"] and server.area_view(json.dumps(rec))["name"] is None, rec
d = project("adds", LAT, LNG)
(server.PROJECTS / d / server.CAPTURE_FILE).write_text(json.dumps({"mesh": "m1", "shots": []}))
assert server.match_area(d) is None
print("server OK")
