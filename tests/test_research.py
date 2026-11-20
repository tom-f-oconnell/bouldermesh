#!/usr/bin/env python3
"""Searching YouTube again for areas' climbs searched the old way (youtube.SEARCH_METHOD): the admin page's button,
what it searches with (the area's name), and the climbs it finds new videos of queued after everything else, to add them
and mesh again, without being taken as old. Offline: harvest, reconstruction and the search are stubbed.

    python3 tests/test_research.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="bouldermesh-research-")

import pipeline  # noqa: E402
import server  # noqa: E402
from climbs.common import Climb, Media  # noqa: E402

HEAD = 'Route,Location,URL,"Avg Stars","Your Stars","Route Type",Rating,Pitches,Length,"Area Latitude","Area Longitude"\n'


def row(rid: int, name: str, boulder: str) -> str:
    return (f'{name},"{boulder} > Main Area > Horse Flats > Angeles National Forest > California",'
            f'https://www.mountainproject.com/route/{rid}/{name.lower()},3.0,-1,Boulder,V{rid},,,34.0,-116.0\n')


def fake_fetch(url, include_children=False):
    rid = url.split("/")[-2]
    item = server.q("SELECT data FROM campaign_items WHERE source_id=? LIMIT 1", rid, one=True)
    data = json.loads(item["data"]) if item else {}
    return [Climb(source="mountainproject", source_id=rid, url=url, name=url.split("/")[-1].capitalize(), lat=34.0, lng=-116.0,
                  area_path=data.get("area_path") or [], boulder_name=data.get("boulder_name"),
                  images=[Media(url=f"http://x/{i}.jpg", kind="image") for i in range(3)])]


harvested: list[tuple[str, bool]] = []


def fake_harvest(c, project_dir, log, sources, add=False, **_):
    harvested.append((c.name, add))
    (project_dir / "photos").mkdir(parents=True, exist_ok=True)
    (project_dir / "photos" / f"{c.name}{len(harvested)}.jpg").write_bytes(b"jpg")
    (project_dir / "sources.json").write_text(json.dumps({"files": []}))
    return {"images": 20, "diversity": {"verdict": "good"}, "video_frames": 0, "queries": [], "youtube_checked": True}


def fake_reconstruct(images, work, out_dir, log, *_, **__):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mesh.ply").write_text("ply")
    return out_dir / "mesh.ply", False


searched: list[str] = []
results: dict[str, list] = {}


def fake_search(c, log, want=None, state=None, **_):
    words = server.youtube_mod.query(c)
    searched.append(words)
    if state is not None:
        state.update(checked=c.name not in ("Gamma",), words=words)
    return [Media(url=f"https://www.youtube.com/watch?v={v}", kind="video", title=c.name, source="youtube-search")
            for v in results.get(c.name, [])]


server.climbs_mod.fetch_climb_url = fake_fetch
server.harvest_mod.harvest = fake_harvest
pipeline.reconstruct = fake_reconstruct
server.pipeline.steps = lambda: []
server.place_climbs = lambda *a: None
server.CAMPAIGN_MIN_FREE = 0
server.init_db()
server.settings().update({"areas.meshes": "climbs"})

csv = HEAD + row(1, "Alpha", "Alpha Boulder") + row(2, "Beta", "Beta Boulder") + row(3, "Gamma", "Gamma Boulder")
camp = server.create_campaign("", "popularity", csv)
server.q("UPDATE campaigns SET name='Horse Flats' WHERE id=?", camp["id"], write=True)
for _ in range(20):
    if not server.next_campaign_step():
        break
items = lambda: {i["name"]: i for i in server.q("SELECT * FROM campaign_items WHERE campaign_id=?", camp["id"])}  # noqa: E731
assert {n: i["status"] for n, i in items().items()} == dict.fromkeys(("Alpha", "Beta", "Gamma"), "done"), items()

st = server.research_status()
assert st["stale"] == 3 and not st["disabled"], st
server.youtube_mod.search = fake_search
assert "3 climb" in server.admin_youtube_research({})["result"]
assert server.research_status()["disabled"].startswith("under way"), "pressed once"

results.update(Alpha=["aaaaaaaaaaa"])
for r in server.research_stale():
    server.research_one(r)
assert all(w.endswith(" Horse Flats") for w in searched), ("its area's name", searched)
got = items()
assert (got["Alpha"]["status"], got["Alpha"]["redo"], got["Alpha"]["old_rerun"]) == ("pending", "more+rebuild", 2), got["Alpha"]
assert (got["Beta"]["status"], got["Beta"]["redo"]) == ("done", None), "nothing new: left alone"
assert server.research_status()["stale"] == 1, "Gamma's search couldn't be made: still to go"
assert server.q("SELECT yt_tried FROM climbs WHERE name='Gamma'", one=True)["yt_tried"]

c = server.q("SELECT * FROM campaigns WHERE id=?", camp["id"], one=True)
assert [i["name"] for i in server.ordered_pending(c)] == [], "after everything else: not in its area's own list"
harvested.clear()
for _ in range(10):
    if not server.next_campaign_step():
        break
assert harvested == [("Alpha", True)], harvested
got = items()
assert (got["Alpha"]["status"], got["Alpha"]["old_rerun"], got["Alpha"]["redo"]) == ("done", 0, None), got["Alpha"]

searched.clear()
server.q("UPDATE climbs SET yt_tried=NULL", write=True)
fake_search.__defaults__ = None
server.youtube_mod.search = lambda c, log, want=None, state=None, **_: (state.update(checked=True, words="x"), [])[1]
for r in server.research_stale():
    server.research_one(r)
st = server.research_status()
assert st["stale"] == 0 and "every area's climb" in st["disabled"], st
try:
    server.admin_youtube_research({})
    raise AssertionError("nothing to do: refused")
except ValueError:
    pass
print("ALL RESEARCH TESTS PASSED")
