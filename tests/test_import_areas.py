#!/usr/bin/env python3
"""A climb imported on its own (the Climbs tab's Import, one climb's link only) and the areas: its meshes count as the
area's that lists it, linked yet or not, and an area's job that comes to it carries on from its harvest rather than
harvesting it again. Offline: nothing is fetched, and the harvest is stubbed."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = pathlib.Path(tempfile.mkdtemp(prefix="bouldermesh-import-"))
os.environ["DATA_DIR"] = str(tmp)

import server  # noqa: E402
from climbs.common import Climb  # noqa: E402

server.init_db()

# --- area links are refused by Import: an area is an area's job
for url, area in (("https://www.mountainproject.com/area/105720495/joshua-tree-national-park", True),
                  ("https://www.mountainproject.com/route/105721234/some-problem", False),
                  ("https://openbeta.io/areas/0b1c2d3e-0000-4000-8000-000000000000", True),
                  ("https://openbeta.io/climbs/0b1c2d3e-0000-4000-8000-000000000000", False)):
    assert server.is_area_url(url) == area, url
print("area links told apart OK")

# --- an imported climb, meshed, in an area's list not yet linked to it: the mesh is the area's
cid = server.upsert_climb(Climb(source="mountainproject", source_id="111", url="https://mp/route/111/a", name="Alpha",
                                kind="boulder", area_path=["Area", "Rock"]))
mid = server.new_id()
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo) VALUES(?,?,?,?,?,?,?)", mid, "Alpha", "mesh.ply", "upload",
         time.time(), 3, 0, write=True)
server.q("INSERT INTO mesh_links(mesh_id,climb_id,note,created) VALUES(?,?,?,?)", mid, cid, "test", time.time(), write=True)
row = {"id": mid, "climbs": [{"id": cid}]}
assert server.mesh_areas()(row)[0] == [], "no area lists it yet"
server.q("INSERT INTO campaigns(id,name,status,created) VALUES(?,?,?,?)", "camp1", "The Area", "stopped", time.time(), write=True)
for sid, name in (("111", "Alpha"), ("222", "Beta")):
    server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,data,status) VALUES(?,?,?,?,?,?)", "camp1", sid, 1, name,
             json.dumps(Climb(source="mountainproject", source_id=sid, url=f"https://mp/route/{sid}/x", name=name, kind="boulder",
                              area_path=["Area", "Rock"]).to_dict()), "pending", write=True)
assert server.mesh_areas()(row)[0] == ["The Area"], server.mesh_areas()(row)
print("an imported climb's mesh is the area's that lists it OK")

# --- the area's job comes to a climb harvested on its own (and not meshed): it carries on from that harvest
bid = server.upsert_climb(Climb(source="mountainproject", source_id="222", url="https://mp/route/222/b", name="Beta",
                                kind="boulder", area_path=["Area", "Rock"]))
own = server.new_id()
(server.PROJECTS / own / "photos").mkdir(parents=True)
server.q("INSERT INTO projects(id,name,created,kind,climb_id,status,harvest) VALUES(?,?,?,?,?,?,?)", own, "Beta (harvest)", time.time(),
         "harvest", bid, "harvested", json.dumps({"images": 3}), write=True)
calls = []


def fake_harvest(pid, *a, resume=False, **k):
    calls.append((pid, resume))
    server.q("UPDATE projects SET status='harvested' WHERE id=?", pid, write=True)


server.run_harvest = fake_harvest
camp = server.q("SELECT * FROM campaigns WHERE id='camp1'", one=True)
item = server.q("SELECT * FROM campaign_items WHERE campaign_id='camp1' AND source_id='222'", one=True)
server.run_campaign_item(camp, item)
assert calls == [(own, True)], calls
after = server.q("SELECT climb_id, project_id FROM campaign_items WHERE campaign_id='camp1' AND source_id='222'", one=True)
assert after["climb_id"] == bid and after["project_id"] == own, after
assert "taken up by the area The Area" in (server.PROJECTS / own / "job.log").read_text()
# and one already meshed: done, with its mesh, nothing harvested
calls.clear()
item = server.q("SELECT * FROM campaign_items WHERE campaign_id='camp1' AND source_id='111'", one=True)
server.run_campaign_item(camp, item)
done = server.q("SELECT status, mesh_id FROM campaign_items WHERE campaign_id='camp1' AND source_id='111'", one=True)
assert not calls and done["status"] == "done" and done["mesh_id"] == mid, (calls, done)
print("an area's job carries on from what was done for an imported climb OK")
print("ALL IMPORT AREAS TESTS PASSED")
