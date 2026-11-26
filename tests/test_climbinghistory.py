#!/usr/bin/env python3
"""climbing-history.org: its climb pages and lists of the hardest read from fixtures, a climb imported by its link, and a
list meshed as an area's job (its hardest N, in the list's order). Offline: pages, the harvest and the reconstruction are
stubbed.  Run: python3 tests/test_climbinghistory.py"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="bouldermesh-climbinghistory-")
os.environ["YOUTUBE_OFFLINE"] = "1"

import pipeline  # noqa: E402
import server  # noqa: E402
from climbs import adapter_for_url, climbinghistory as ch, youtube  # noqa: E402
from climbs.common import Climb, HttpError, Media  # noqa: E402

BASE = "https://climbing-history.org"


# --- a climb's page, trimmed from the live ones (Nov 2026) to the structures the parser depends on --------------------
def ascent(row: int, who: str, date: str, note: str, refs: str, dnf: bool = False) -> str:
    return f"""<tr class="ascent {'dnf' if dnf else ''} collapsible-row collapsed" data-row-id="{row}">
 <td class="collapse-chevron-col"><i></i></td><td><a href="/climber/1/x">{who}</a></td>
 <td class="nowrap">Boulder | {'did not finish' if dnf else 'worked'}</td><td data-sort-value="{date}">shown otherwise</td><td>9A</td></tr>
<tr class="ascent ascent-subtitle-row "><td></td><td colspan="4"><small class="ascent-subtitle text-muted">{note}</small></td></tr>
<tr class="ascent ascent-detail-row " data-row-id="{row}"><td></td><td colspan="4"><div class="ascent-notes"><h3>References</h3>{refs}</div></td></tr>"""


def link(url: str) -> str:
    return f'<p><a href="{url}" rel="noopener noreferrer">{url}</a></p>'


def page(cid: int, name: str, grade: str, crag: str, crag_id: int, above: list[str], ascents: str, notes: str = "", has_map: bool = False) -> str:
    crumbs = " / ".join(f'<a href="/crags/location/{"/".join(above[:i + 1])}" class="text-muted">{a}</a>' for i, a in enumerate(above))
    info = lambda: f"""<table class="table">{f'<tr class="climber-info-item"><td>Grade</td><td> {grade} </td></tr>' if grade else ''}
 <tr class="climber-info-item"><td>Type</td><td>Boulder problem</td></tr>
 <tr class="climber-info-item"><td>Crag</td><td><a href="/crag/{crag_id}/{crag.lower()}">{crag}</a></td></tr>
 <tr class="climber-info-item"><td>Country</td><td>{above[0]} 🇫🇮</td></tr>
 <tr class="climber-info-item"><td>Rock type</td><td><a href="/glossary/rock-types#granite">Granite</a></td></tr>
 <tr class="climber-info-item"><td colspan="2"><strong>History</strong></td></tr>
 <tr class="climber-info-item"><td>First ascent</td><td><a href="/climber/602/n">Nalle Hukkataival</a> <span class="text-muted">(24th Oct 2016)</span></td></tr>
</table>"""  # noqa: E731
    return f"""<html><head><title>{name} {grade} | Climbing History</title></head><body>
<nav><a href="https://discord.gg/x">Discord</a><a href="https://www.youtube.com/watch?v=NAVNAVNAVNA">not the climb's</a></nav>
<div class="container"><h1><div class="climb-title-row"><span>
  {name} <small class="fw-normal">| {grade or 'Ungraded'} Boulder problem at <a href="/crag/{crag_id}/{crag.lower()}">{crag}</a></small></span>
  <span><a href="/changes?climb_id={cid}">Change Log</a></span></div></h1>
<h6 class="text-muted">{crumbs}</h6>
<div class="external-links"><a href="https://www.ukclimbing.com/logbook/c/x-1" target="_blank">UKClimbing.com</a> |
 <a href="https://www.8a.nu/crags/bouldering/finland/lappnor/routes/{name.lower().replace(' ', '-')}/" target="_blank">8a.nu</a></div>
<h4>Quick Info</h4><div class="collapse" id="quick_info_mobile">{info()}</div>
<div class="flow-root"><div class="climber-info-box-right"><h3>Quick Info</h3>{info()}</div>
<span class="text-muted">Also known as <em>The Lappnor Project</em>.</span>
<p>Its contributors' account of it, which isn't kept.</p>{notes}</div>
{'<h3>Map</h3><script>fetch("/api/v1/map_config?climb_id=%d")</script>' % cid if has_map else ''}
<h3>Pics + Vids</h3><div class="media-list" data-media-filter="climb_id={cid}"><span>Loading pics and vids...</span></div>
<h3>Ascents</h3><table class="table table-sm"><tbody data-sort-group>{ascents}</tbody></table>
</div><footer><a href="https://latticetraining.com">Lattice</a></footer></body></html>"""


BURDEN = page(826, "Burden of Dreams", "9A", "Lappnor", 17691, ["Finland", "Uusimaa", "Loviisa"], "".join([
    ascent(1700, "Nalle Hukkataival", "2016-10-24", "First ascent.", link("https://www.ukclimbing.com/news/2016/10/x") + "<p>[2] Send footy: </p>"
           + link("https://www.instagram.com/p/DNIgOzIs8zE/") + link("https://www.youtube.com/watch?v=jX2exjenOqE")
           + "<ul><li>" + link("https://www.instagram.com/p/d34fcpSEr-/") + "</li></ul>"),
    ascent(5860, "Will Bosi", "2023-04-12", "Second ascent. 14 sessions.", link("https://youtu.be/SlUw8X7xuq0")
           + link("https://www.instagram.com/reel/CrB1L4ctXrR/?igsh=abc") + link("https://vimeo.com/ondemand/burdenofordreams")
           + link("https://vimeo.com/138381117")),
    ascent(19203, "Makoto Yamauchi", "2026-04-04", "", link("https://www.youtube.com/live/U2dMcEgWtpc?t=2900")
           + link("https://www.youtube.com/watch?v=jX2exjenOqE&t=30s") + link("https://m.youtube.com/watch?app=desktop&v=FIV4ZyKaHmA&t=1m5s")),
    ascent(8850, "Ryuichi Murai", "2024-01-01", "", link("https://www.youtube.com/shorts/XXW0pTnkQjc"), dnf=True)]),
    notes="<h3>References</h3>" + link("https://www.instagram.com/p/CrB1L4ctXrR/") + link("https://youtu.be/NOTEnoteNOT"))

c = ch.parse_climb(BURDEN, f"{BASE}/climb/826/burden-of-dreams")
assert (c.source, c.source_id, c.name, c.kind) == ("climbinghistory", "ch826", "Burden of Dreams", "boulder"), c
assert c.grade == "V17 9A" and c.grades == {"vscale": "V17", "font": "9A"}, (c.grade, c.grades)
assert c.area_path == ["Finland", "Uusimaa", "Loviisa", "Lappnor"] and c.area_urls == [f"{BASE}/crag/17691/lappnor"], c.area_path
assert c.fa == "Nalle Hukkataival, 24th Oct 2016" and c.search_place == "Lappnor", (c.fa, c.search_place)
# its ascents' videos first, in the page's order, then its own notes'; none twice; never the site's own links
assert [(v.url[-11:], v.credit, v.start) for v in c.videos if "youtube" in v.url] == [
    ("jX2exjenOqE", "Nalle Hukkataival", None), ("SlUw8X7xuq0", "Will Bosi", None), ("U2dMcEgWtpc", "Makoto Yamauchi", 2900.0),
    ("FIV4ZyKaHmA", "Makoto Yamauchi", 65.0), ("XXW0pTnkQjc", "Ryuichi Murai", None), ("NOTEnoteNOT", None, None)], c.videos
assert [v.url for v in c.videos if "vimeo" in v.url] == ["https://vimeo.com/138381117"], "a film to pay for isn't a video to fetch"
assert c.videos[0].title == "Nalle Hukkataival, 2016-10-24" and all(v.page == c.url and v.source == "climbinghistory" for v in c.videos)
# Instagram is listed, with whose ascent each post was given for, and isn't among its videos
assert c.raw["instagram"] == [
    {"url": "https://www.instagram.com/p/DNIgOzIs8zE/", "climber": "Nalle Hukkataival", "date": "2016-10-24"},
    {"url": "https://www.instagram.com/p/d34fcpSEr-/", "climber": "Nalle Hukkataival", "date": "2016-10-24"},
    {"url": "https://www.instagram.com/reel/CrB1L4ctXrR/", "climber": "Will Bosi", "date": "2023-04-12"}], c.raw["instagram"]
assert not any("instagram" in v.url for v in c.videos)
assert c.raw["aka"] == "The Lappnor Project" and c.raw["crag_id"] == "17691" and c.raw["country"] == "Finland" and not c.raw["has_map"], c.raw
assert c.cross_refs["8a"].startswith("https://www.8a.nu/") and set(c.raw["links"]) == {"UKClimbing.com", "8a.nu"}, c.cross_refs
assert [(a["climber"], a["done"], a.get("note")) for a in c.raw["ascents"]] == [
    ("Nalle Hukkataival", True, "First ascent."), ("Will Bosi", True, "Second ascent. 14 sessions."), ("Makoto Yamauchi", True, ""),
    ("Ryuichi Murai", False, "")], c.raw["ascents"]
assert not c.description, "its contributors' text stays on its page"
kept = {v.url[-11:]: v.start for v in Climb.from_dict(json.loads(json.dumps(c.to_dict()))).videos}
assert kept["U2dMcEgWtpc"] == 2900.0 and kept["jX2exjenOqE"] is None, "a video's start survives being kept"
quoted = ch.parse_climb(page(1, "Maxwell&#39;s Demon Low", "8C+", "Secret Garden", 2, ["United States", "Colorado"], ""), f"{BASE}/climb/1/x")
assert quoted.name == "Maxwell's Demon Low", quoted.name
print("climb page OK")

# --- grades, links and moments ------------------------------------------------------------------------------------
assert ch.grades("8C+/9A") == ("V16 8C+/9A", {"vscale": "V16", "font": "8C+/9A"}) and ch.grades("7C+")[0] == "V10 7C+"
assert ch.grades("V15") == ("V15", {"vscale": "V15"}) and ch.grades(None) == (None, {}) and ch.grades("9b+", boulder=False) == ("9b+", {})
assert ch.climb_url(1547, "la-révolutionnaire") == f"{BASE}/climb/1547/la-r%C3%A9volutionnaire"
assert ch.climb_url(1549, "hypoth%C3%A8se-(assis)") == f"{BASE}/climb/1549/hypoth%C3%A8se-(assis)", "encoded once"
assert (ch._start("?t=439"), ch._start("&t=1h2m3s"), ch._start("?si=abc"), ch._start("&start=12")) == (439.0, 3723.0, None, 12.0)
assert adapter_for_url("https://climbing-history.org/climb/826/burden-of-dreams") is ch
assert adapter_for_url("climbing-history.org/climb/826") is ch and adapter_for_url("https://example.org/climb/826") is None
key = ch.list_key("https://climbing-history.org/hardest?discipline=boulder&grade=8C%2B")
assert key == "ch:hardest:boulder:8C+:" == ch.list_key("climbing-history.org/hardest?grade=8C%2B&discipline=boulder&page=2"), key
assert ch.list_key(f"{BASE}/climb/826/burden-of-dreams") is None and ch.list_key("https://www.mountainproject.com/area/1/x") is None
assert ch.list_url(key) == f"{BASE}/hardest?discipline=boulder&grade=8C%2B" and ch.list_url("ch:hardest:boulder:8C:FI", 2).endswith("grade=8C&country=FI&page=2")
print("grades and links OK")


# --- a list of the hardest ------------------------------------------------------------------------------------------
def list_row(rank: int, cid: int, name: str, grade: str, crag: str) -> str:
    return f"""<tr><td class="text-muted">{rank}</td><td><a href="/climb/{cid}/{name.lower().replace(' ', '-')}">{name}</a></td><td>{grade}</td>
 <td><a href="/crag/{cid + 9000}/{crag.lower()}">{crag}</a></td>
 <td><a href="/climber/1/x">Someone</a> <span class="text-muted">(2020)</span></td><td>{rank}</td></tr>"""


def list_page(rows: list[tuple]) -> str:
    return ('<html><body><h1>Hardest Boulder Problems in the World</h1><p class="text-muted">88 climbs</p><table class="table"><thead><tr>'
            "<th></th><th>Climb</th><th>Grade</th><th>Crag</th><th>First Ascent</th><th># Ascents</th></tr></thead><tbody>"
            + "".join(list_row(*r) for r in rows) + "</tbody></table></body></html>")


title, rows = ch.parse_list(list_page([(1, 6431, "Exodia", "9A+", "Barbara"), (2, 826, "Burden of Dreams", "9A", "Lappnor")]))
assert title == "Hardest Boulder Problems in the World" and [(r.source_id, r.name, r.grade, r.raw["rank"], r.raw["crag"]) for r in rows] == [
    ("ch6431", "Exodia", "V18 9A+", 1, "Barbara"), ("ch826", "Burden of Dreams", "V17 9A", 2, "Lappnor")], rows
assert rows[0].raw["stub"] and rows[0].area_path == [] and rows[0].url == f"{BASE}/climb/6431/exodia" and rows[0].fa == "Someone, 2020"
pages = {1: list_page([(i, 100 + i, f"Climb {i}", "9A", "Crag") for i in range(1, 51)]),
         2: list_page([(i, 100 + i, f"Climb {i}", "8C+", "Crag") for i in range(51, 58)])}
asked: list[str] = []
real_get_text, real_get_json = ch.http.get_text, ch.http.get_json
ch.http.get_text = lambda url, **kw: asked.append(url) or pages[int(url.split("page=")[1]) if "page=" in url else 1]
name, listed = ch.fetch_list(key)
assert name == "Hardest Boulder Problems in the World, 8C+ and harder" and len(listed) == 57 and len(asked) == 2, (name, len(listed), asked)
try:
    ch.fetch_list("ch:hardest:sport:9b+:")
    raise AssertionError("a list of routes")
except ValueError as e:
    assert "boulder" in str(e), e
print("list OK")

# --- a climb's position: its own where its page has a map, else its crag's ---------------------------------------------
positions = {"climb_id=1919": {"latitude": 46.43064, "longitude": 8.84853, "title": "Alphane", "zoom": 13},
             "crag_id=17691": {"latitude": 60.42053, "longitude": 26.125323, "title": "Lappnor", "zoom": 13}}
maps: list[str] = []


def fake_json(url, **kw):
    ask = url.split("map_config?")[1]
    maps.append(ask)
    if ask not in positions:
        raise HttpError(url, 404, "NOT FOUND")
    return positions[ask]


ch.http.get_json = fake_json
web = {f"{BASE}/climb/826/burden-of-dreams": BURDEN,
       f"{BASE}/climb/1919/alphane": page(1919, "Alphane", "9A", "Chironico", 3879, ["Switzerland", "Ticino"], ascent(
           1, "Shawn Raboutou", "2022-04-06", "First ascent.", link("https://youtu.be/Rx6GPpO37C4?t=2564")), has_map=True),
       f"{BASE}/climb/3456/pneuma": page(3456, "Pneuma", "", "Lohja", 34182, ["Finland", "Uusimaa"], "")}
ch.http.get_text = lambda url, **kw: asked.append(url) or web[url]
burden = ch.fetch(f"{BASE}/climb/826/burden-of-dreams")[0]
assert (burden.lat, burden.lng, burden.coord_source) == (60.42053, 26.125323, "area") and maps == ["crag_id=17691"], (burden.lat, maps)
alphane = ch.fetch("climbing-history.org/climb/1919/alphane")[0]
assert (alphane.lat, alphane.lng, alphane.coord_source) == (46.43064, 8.84853, "route") and alphane.videos[0].start == 2564.0, alphane
pneuma = ch.fetch(f"{BASE}/climb/3456/pneuma")[0]
assert pneuma.lat is None and pneuma.grade is None and pneuma.area_path == ["Finland", "Uusimaa", "Lohja"], pneuma
try:
    ch.fetch(f"{BASE}/hardest?discipline=boulder")
    raise AssertionError("a list isn't a climb")
except ValueError as e:
    assert "climb link" in str(e), e
print("positions OK")

# --- imported by its link: its crag is a group, never one boulder; its search names its crag ------------------------
server.CAMPAIGN_MIN_FREE = 0
server.init_db()
cid = server.upsert_climb(burden)
row = server.climb_row(cid, full=True)
assert row["source"] == "climbinghistory" and row["url"] == f"{BASE}/climb/826/burden-of-dreams" and row["lat"] == 60.42053, row
leaf = server.boulder_of(row["area_path"])
assert server.leaf_kind(leaf) == "group" and row["boulder"]["kind"] == "group" and row["boulder"]["url"] == f"{BASE}/crag/17691/lappnor", row["boulder"]
assert not server.pools_whole(leaf) and server.joins_pools(leaf)
obj = server.climb_obj(cid)
assert obj.search_place == "Lappnor" and youtube.query(obj) == "Burden of Dreams 9A Lappnor", youtube.query(obj)
# a title with its Font grade is of it, as one with its V grade is; one that only names it isn't, nor another grade's
assert youtube.matches(obj, "Burden of Dreams 9A - Will Bosi") and youtube.matches(obj, "BURDEN OF DREAMS (V17)")
assert youtube.matches(obj, "Burden of Dreams", "Nalle's project in Finland") and youtube.matches(obj, "Burden of Dreams | Lappnor")
assert not youtube.matches(obj, "Burden of Dreams") and not youtube.matches(obj, "Burden of Dreams 8A replica")
american = Climb(source="climbinghistory", source_id="ch1", url="", name="Return of the Sleepwalker", grade="V17 9A",
                 grades={"vscale": "V17", "font": "9A"}, area_path=["United States", "Nevada", "Clark County", "Black Velvet Canyon"],
                 search_place="Black Velvet Canyon", raw={"country": "United States"})
assert youtube.query(american) == "Return of the Sleepwalker V17 Black Velvet Canyon", youtube.query(american)
mp_climb = Climb(source="mountainproject", source_id="1", url="", name="Burden of Dreams", grade="V17 9A", grades={"font": "9A"},
                 area_path=["International", "Europe", "Finland", "Lappnor"])
assert not youtube.matches(mp_climb, "Burden of Dreams 9A - Will Bosi"), "Mountain Project's climbs are matched as they were"
assert server.is_area_url(f"{BASE}/hardest?discipline=boulder&grade=8C%2B") and not server.is_area_url(f"{BASE}/climb/826/burden-of-dreams")
print("import OK")

# --- a list as an area's job: its hardest N, in its order ------------------------------------------------------------
harvested: list[str] = []
seen_videos: dict[str, int] = {}


def fake_harvest(c, project_dir, log, sources, relevance_filter=True, tracker=None, pool_from=None, anchors_from=None, want=None,
                 want_search=None, add=False, refresh=False, rejudge=False):
    harvested.append(c.name)
    seen_videos[c.name] = len(c.videos)
    assert not c.raw.get("stub") and c.area_path, "its page was read before it's harvested"
    return {"images": 20, "diversity": {"verdict": "good"}, "video_frames": 0, "queries": []}


def fake_reconstruct(images, work, out_dir, log, use_gpu=False, internet=False, tracker=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mesh.ply").write_text("ply")
    return out_dir / "mesh.ply", False


server.harvest_mod.harvest = fake_harvest
pipeline.reconstruct = fake_reconstruct
server.pipeline.steps = lambda: []
LIST = [(1, 1919, "Alphane", "9A", "Chironico"), (2, 826, "Burden of Dreams", "9A", "Lappnor"), (3, 3456, "Pneuma", "8C+", "Lohja"),
        (4, 7000, "Missing", "8C+", "Nowhere")]
lists: list[bool] = []
ch.http.get_text = lambda url, **kw: (lists.append(kw.get("ttl") == 0) or list_page(LIST)) if "/hardest" in url else (asked.append(url) or web[url])


def run_all(camp_id: str, limit: int = 20) -> None:
    for _ in range(limit):
        if server.campaign_status(camp_id) != "running":
            return
        server.next_campaign_step()


def items(camp_id: str) -> dict[str, dict]:
    return {i["name"]: i for i in server.q("SELECT * FROM campaign_items WHERE campaign_id=?", camp_id)}


for bad, why in ((f"{BASE}/climb/826/burden-of-dreams", "one climb's page"), (f"{BASE}/hardest?discipline=sport&grade=9b%2B", "boulder problems")):
    try:
        server.create_campaign(bad)
        raise AssertionError(bad)
    except ValueError as e:
        assert why in str(e), e
del asked[:]
assert server.settings().get("areas.meshes") == "boulders", "as the live site has it: a crag's climbs harvested, then meshed alone or with those their photos match"
camp = server.create_campaign(f"{BASE}/hardest?discipline=boulder&grade=8C%2B", "rating", max_meshes=2, without_youtube=True)
assert camp["name"] == "Hardest Boulder Problems in the World, 8C+ and harder" and camp["area_id"] == key and camp["ord"] == "list", camp
assert not asked, "no climb's page is read to list them"
got = items(camp["id"])
assert [got[n]["rank"] for n in ("Alphane", "Burden of Dreams", "Pneuma", "Missing")] == [1, 2, 3, 4] and got["Alphane"]["boulder"] is None, got
view = server.campaign_view(camp)
assert view["list_url"] == f"{BASE}/hardest?discipline=boulder&grade=8C%2B" and view["counts"]["pending"] == 2 and view["counts"]["beyond"] == 2, view["counts"]
assert server.runs_said(camp) == ["up to 2 hardest"], server.runs_said(camp)
run_all(camp["id"])
got = items(camp["id"])
assert harvested == ["Alphane", "Burden of Dreams"], harvested      # the hardest two, each read from its page as its turn came
assert asked == [f"{BASE}/climb/1919/alphane", f"{BASE}/climb/826/burden-of-dreams"], asked    # (from the cache, outside tests)
assert got["Alphane"]["status"] == got["Burden of Dreams"]["status"] == "done" and got["Pneuma"]["status"] == "pending", got
assert got["Burden of Dreams"]["climb_id"] == cid, "the climb imported by its link, not a second one"
assert json.loads(got["Alphane"]["boulder"]) == ["Switzerland", "Ticino", "Chironico"] and server.leaf_kind(got["Alphane"]["boulder"]) == "group"
assert not json.loads(got["Alphane"]["data"])["raw"].get("stub") and seen_videos == {"Alphane": 1, "Burden of Dreams": 7}, seen_videos
assert server.campaign_status(camp["id"]) == "done" and server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", camp["id"], one=True))["made"] == 2
assert server.q("SELECT COUNT(*) n FROM meshes", one=True)["n"] == 2 and server.q("SELECT lat FROM climbs WHERE id=?", got["Alphane"]["climb_id"], one=True)["lat"] == 46.43064
print("hardest N OK")

# --- raising N goes on down the list; a page that can't be read fails only its climb; a page without a grade keeps the list's
again = server.create_campaign(f"{BASE}/hardest?grade=8C%2B&discipline=boulder", max_meshes=4, without_youtube=True)
assert again["again"] and again["id"] == camp["id"] and again["status"] == "running" and lists == [False], (again, lists)
run_all(camp["id"])
got = items(camp["id"])
assert harvested == ["Alphane", "Burden of Dreams", "Pneuma"], harvested
assert got["Pneuma"]["status"] == "done" and got["Missing"]["status"] == "failed" and "couldn't read its page" in got["Missing"]["note"], got["Missing"]
assert server.q("SELECT grade FROM climbs WHERE id=?", got["Pneuma"]["climb_id"], one=True)["grade"] == "V16 8C+", "its page gives none"
print("raising N OK")

# --- checking the list again: a climb added among the hardest is ranked where the list has it, and meshed -----------
LIST.insert(0, (1, 6431, "Exodia", "9A+", "Barbara"))
LIST[:] = [(i + 1, *r[1:]) for i, r in enumerate(LIST)]
web[f"{BASE}/climb/6431/exodia"] = page(6431, "Exodia", "9A+", "Barbara", 33631, ["Italy", "Abruzzo"], "")
again = server.create_campaign(f"{BASE}/hardest?discipline=boulder&grade=8C%2B", max_meshes=4, without_youtube=True, check_new=True)
assert again["added"] == 1 and lists == [False, True], (again["added"], lists)
got = items(camp["id"])
assert [got[n]["rank"] for n in ("Exodia", "Alphane", "Burden of Dreams", "Pneuma", "Missing")] == [1, 2, 3, 4, 5], {n: i["rank"] for n, i in got.items()}
run_all(camp["id"])
assert harvested[-1] == "Exodia" and items(camp["id"])["Exodia"]["status"] == "done", harvested
print("checking the list again OK")

# --- looking for new videos reads its page again, for what its newer ascents link to ---------------------------------
web[f"{BASE}/climb/1919/alphane"] = page(1919, "Alphane", "9A", "Chironico", 3879, ["Switzerland", "Ticino"], ascent(
    1, "Shawn Raboutou", "2022-04-06", "First ascent.", link("https://youtu.be/Rx6GPpO37C4?t=2564")) + ascent(
    2, "Aidan Roberts", "2022-10-19", "Second ascent.", link("https://www.youtube.com/watch?v=zilINHowP2g")), has_map=True)
a = items(camp["id"])["Alphane"]
new = server.new_videos(a["climb_id"], a["project_id"], lambda s: None)
assert new == ["https://www.youtube.com/watch?v=zilINHowP2g"], new
assert len(server.climb_obj(a["climb_id"]).videos) == 2
print("new videos OK")

# --- the area's "Check for new photos" reads each climb's page afresh ------------------------------------------------
reads: list[tuple[str, bool]] = []
ch.http.get_text = lambda url, **kw: reads.append((url, kw.get("ttl") == 0)) or (list_page(LIST) if "/hardest" in url else web[url])
del harvested[:]
queued = server.queue_redo(camp["id"], "new")
assert queued == 4, queued      # the four that have been through and have photos of their own; Missing never was read
for _ in range(10):
    server.next_campaign_step()
assert sorted(harvested) == ["Alphane", "Burden of Dreams", "Exodia", "Pneuma"], harvested
assert sorted(u.rsplit("/", 1)[1] for u, afresh in reads if afresh) == ["alphane", "burden-of-dreams", "exodia", "pneuma"], reads
assert all(i["status"] == "done" for n, i in items(camp["id"]).items() if n != "Missing"), items(camp["id"])
print("check for new OK")

# --- the same climb on Mountain Project: told by its name and where it is, and given the page's videos ---------------
assert server.twin_name("Bloody Sunday SDS") == server.twin_name("bloody sunday (sit start)") == "bloody sunday sit"
assert server.twin_name("The Ice Knife sit") == "ice knife sit" and server.twin_name("Révolutionnaire") == "revolutionnaire"
HEAD = 'Route,Location,URL,"Avg Stars","Your Stars","Route Type",Rating,Pitches,Length,"Area Latitude","Area Longitude"\n'


def mp_row(rid: int, name: str, where: str, lat: float, lng: float) -> str:
    return f'{name},"{where}",https://www.mountainproject.com/route/{rid}/x,3.5,-1,Boulder,V16,,,{lat},{lng}\n'


web[f"{BASE}/climb/1116/return-of-the-sleepwalker"] = page(1116, "Return of the Sleepwalker", "9A", "Black Velvet Canyon", 4226,
                                                            ["United States", "Nevada", "Clark County"], ascent(
    1, "Daniel Woods", "2021-03-31", "First ascent.", link("https://www.youtube.com/watch?v=IoEeHwd5GTs") + link("https://youtu.be/8awesjyAGTg")))
web[f"{BASE}/climb/5000/karoshi"] = page(5000, "Karoshi", "8C+", "Hueco Tanks", 777, ["United States", "Texas"], ascent(
    1, "Someone", "2020-01-01", "", link("https://youtu.be/KAROSHIvid1")))
positions["crag_id=4226"] = {"latitude": 36.034941, "longitude": -115.462274}
positions["crag_id=777"] = {"latitude": 31.92, "longitude": -106.04}
red_rocks = server.create_campaign("", "popularity", HEAD + mp_row(501, "Return Of The Sleepwalker", "Wet Dream Boulder > Black Velvet Canyon > Red Rocks > Nevada", 36.03, -115.46)
                                   + mp_row(502, "Sleepwalker", "Wet Dream Boulder > Black Velvet Canyon > Red Rocks > Nevada", 36.03, -115.46)
                                   + mp_row(503, "Burden of Dreams", "Some Boulder > Joshua Tree > California", 34.0, -116.0), start=False)
rots = ch.fetch(f"{BASE}/climb/1116/return-of-the-sleepwalker")[0]
rots_id = server.upsert_climb(rots)
same = server.q("SELECT * FROM same_climbs")
assert [(r["ch"], r["mp"]) for r in same] == [("ch1116", "501")] and "km apart" in same[0]["how"] and same[0]["km"] < 1, same
assert server.twin_find(burden) is None, "a climb of the same name half the world away isn't it"
assert server.twins_of(server.climb_row(rots_id))[0]["url"] == "https://www.mountainproject.com/route/501", "on an area's list, not read yet"
# the Mountain Project climb, once read, has the page's videos after its own; its card names the other, and the other it
mp_id = server.upsert_climb(Climb(source="mountainproject", source_id="501", url="https://www.mountainproject.com/route/501/x",
                                  name="Return Of The Sleepwalker", lat=36.03, lng=-115.46, coord_source="area",
                                  area_path=["Nevada", "Red Rocks", "Black Velvet Canyon", "Wet Dream Boulder"],
                                  videos=[Media(url="https://www.youtube.com/watch?v=MPSownVIDEO", kind="video"),
                                          Media(url="https://www.youtube.com/watch?v=IoEeHwd5GTs", kind="video")]))
assert [v.url[-11:] for v in server.climb_obj(mp_id).videos] == ["MPSownVIDEO", "IoEeHwd5GTs", "8awesjyAGTg"], server.climb_obj(mp_id).videos
assert len(server.climb_obj(rots_id).videos) == 2, "the climbing-history.org climb keeps its own"
assert server.climb_row(mp_id, full=True)["twins"] == [{"id": rots_id, "name": "Return of the Sleepwalker", "url": rots.url,
                                                        "source": "climbinghistory", "how": same[0]["how"]}]
assert server.climb_row(rots_id, full=True)["twins"][0]["id"] == mp_id
# an area's climb that's been through is queued, after everything else, to add the videos it hasn't and mesh again
pid = server.new_id()
(server.PROJECTS / pid / "photos").mkdir(parents=True)
server.q("INSERT INTO projects(id,name,created,kind,climb_id,status,harvest) VALUES(?,?,?,?,?,'done',?)", pid, "x (harvest)", 0, "harvest", mp_id,
         json.dumps({"images": 5, "videos": [{"url": "https://www.youtube.com/watch?v=IoEeHwd5GTs"}]}), write=True)
server.set_item(red_rocks["id"], "501", status="done", climb_id=mp_id, project_id=pid)
assert server.twin_noted(rots)["mp"] == "501"
it = server.q("SELECT status, redo, old_rerun, note, was FROM campaign_items WHERE campaign_id=? AND source_id='501'", red_rocks["id"], one=True)
assert (it["status"], it["redo"], it["old_rerun"], it["was"]) == ("pending", "more+rebuild", 2, "done") and "1 video" in it["note"], it
assert server.q("SELECT redo FROM campaign_items WHERE campaign_id=? AND source_id='502'", red_rocks["id"], one=True)["redo"] is None
# a climb imported before Mountain Project's area is listed is found when it is
karoshi = server.upsert_climb(ch.fetch(f"{BASE}/climb/5000/karoshi")[0])
assert not server.q("SELECT 1 FROM same_climbs WHERE ch='ch5000'")
server.create_campaign("", "popularity", HEAD + mp_row(601, "Karoshi", "Some Boulder > North Mountain > Hueco Tanks > Texas", 31.925, -106.045), start=False)
assert server.q("SELECT mp FROM same_climbs WHERE ch='ch5000'", one=True)["mp"] == "601"
print("the same climb on Mountain Project OK")

ch.http.get_text, ch.http.get_json = real_get_text, real_get_json
print("ALL CLIMBING-HISTORY TESTS PASSED")
