#!/usr/bin/env python3
"""Meshing a whole area: order, one mesh per boulder, stopping, restarts. Offline: the harvest and
reconstruction are stubbed, but stopping kills a real running process."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = tempfile.mkdtemp(prefix="bouldermesh-campaign-")
os.environ["DATA_DIR"] = tmp

import pipeline  # noqa: E402
import server  # noqa: E402
from climbs.common import Climb, Media, jobctl  # noqa: E402

HEAD = 'Route,Location,URL,"Avg Stars","Your Stars","Route Type",Rating,Pitches,Length,"Area Latitude","Area Longitude"\n'


def row(rid: int, name: str, stars: float, boulder: str) -> str:
    return (f'{name},"{boulder} > Sector > * JT Bouldering > Joshua Tree National Park > California",'
            f'https://www.mountainproject.com/route/{rid}/{name.lower()},{stars},-1,Boulder,V{rid},,,34.0,-116.0\n')


CSV = HEAD + "".join([row(1, "Alpha", 3.0, "Alpha Boulder"), row(2, "Beta", 3.9, "Beta Boulder"), row(3, "Gamma", 2.0, "Gamma Boulder"),
                      row(4, "Alpha2", 2.5, "Alpha Boulder"), row(5, "Delta", 1.0, "Delta Boulders"), row(6, "Delta2", 1.0, "Delta Boulders")])
outcome: dict[str, str] = {}      # climb name -> "poor", "boom", "outage", "stop": what its harvest does
harvested: list[str] = []
videos = {"Gamma": 2}             # how many videos each climb's page has
started = threading.Event()
fetched: list[str] = []


def fake_fetch(url, include_children=False):
    rid = url.split("/")[-2]
    fetched.append(rid)
    name = url.split("/")[-1].capitalize()
    return [Climb(source="mountainproject", source_id=rid, url=url, name=name, kind="boulder", lat=34.0, lng=-116.0,
                  images=[Media(url=f"http://x/{i}.jpg", kind="image") for i in range(3)],
                  videos=[Media(url=f"http://y/{i}", kind="video") for i in range(videos.get(name, 0))])]


def fake_harvest(c, project_dir, log, sources, relevance_filter=True, tracker=None):
    harvested.append(c.name)
    what = outcome.get(c.name)
    if what == "outage":
        raise RuntimeError("no network")
    if what == "stop":
        started.set()
        jobctl.run(["sleep", "60"])   # a real tool, which stopping has to kill
    return {"images": 20, "diversity": {"verdict": "poor" if what == "poor" else "good"}, "video_frames": 0, "queries": []}


def fake_reconstruct(images, work, out_dir, log, use_gpu=False, internet=False, tracker=None):
    work.mkdir(parents=True, exist_ok=True)
    (work / "big.bin").write_bytes(b"x" * 10)
    name = server.q("SELECT name FROM projects WHERE id=?", pathlib.Path(images).parent.name, one=True)["name"]
    if outcome.get(name.split(" (")[0]) == "boom":
        raise RuntimeError("only 2 image(s) could be registered")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mesh.ply").write_text("ply")
    return out_dir / "mesh.ply", False


server.climbs_mod.fetch_climb_url = fake_fetch
server.harvest_mod.harvest = fake_harvest
pipeline.reconstruct = fake_reconstruct
server.pipeline.steps = lambda: []
server.init_db()


def run_all(camp_id: str, limit: int = 20) -> None:
    for _ in range(limit):
        if server.campaign_status(camp_id) != "running":
            return
        server.next_campaign_step()


def items(camp_id: str) -> dict[str, dict]:
    return {i["name"]: i for i in server.q("SELECT * FROM campaign_items WHERE campaign_id=?", camp_id)}


def fresh() -> None:
    for t in ("campaigns", "campaign_items", "projects", "meshes", "mesh_links", "climbs"):
        server.q(f"DELETE FROM {t}", write=True)
    outcome.clear()
    harvested.clear()
    fetched.clear()
    server._outages.clear()


# --- bad input --------------------------------------------------------------------------------
for bad, why in ((("Joshua Tree",), "enter a Mountain Project"), (("", "fastest", CSV), "order must be")):
    try:
        server.create_campaign(*bad)
        raise AssertionError(bad)
    except ValueError as e:
        assert why in str(e), e
try:
    server.create_campaign("", "popularity", HEAD)
    raise AssertionError("empty list")
except ValueError as e:
    assert "no boulder problems" in str(e), e

# --- popularity order: the export's own; one mesh per boulder ----------------------------------
c = server.create_campaign("", "popularity", CSV)
assert c["name"] == "Sector" and c["status"] == "running", c
run_all(c["id"])
assert harvested == ["Alpha", "Beta", "Gamma", "Delta", "Delta2"], harvested   # Alpha2 shares Alpha's rock; "Boulders" don't share
got = items(c["id"])
assert server.campaign_status(c["id"]) == "done" and all(i["status"] == "done" for i in got.values()), got
assert got["Alpha2"]["mesh_id"] == got["Alpha"]["mesh_id"] and got["Alpha2"]["note"] == "same boulder as Alpha", got["Alpha2"]
assert got["Delta2"]["mesh_id"] != got["Delta"]["mesh_id"]
assert server.q("SELECT COUNT(*) n FROM meshes", one=True)["n"] == 5
links = server.q("SELECT COUNT(*) n FROM mesh_links WHERE mesh_id=?", got["Alpha"]["mesh_id"], one=True)["n"]
assert links == 2, links
pdir = server.PROJECTS / got["Alpha"]["project_id"]
assert (pdir / "photos").is_dir() and not (pdir / "work").exists(), list(pdir.iterdir())
assert (server.MESHES / got["Alpha"]["mesh_id"] / "mesh.ply").exists()
print("popularity OK")

# --- stops before filling the disk ----------------------------------------------------------------
fresh()
server.CAMPAIGN_MIN_FREE, floor = float("inf"), server.CAMPAIGN_MIN_FREE
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
server.CAMPAIGN_MIN_FREE = floor
camp = server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True)
assert camp["status"] == "stopped" and "disk" in camp["note"] and not harvested, camp
print("disk OK")

# --- rating order -----------------------------------------------------------------------------
fresh()
c = server.create_campaign("", "rating", CSV)
assert [i["name"] for i in server.ordered_pending(c)][:3] == ["Beta", "Alpha", "Alpha2"], server.ordered_pending(c)
print("rating OK")

# --- data order: most photos and videos, reading few pages ahead -------------------------------
fresh()
server.DATA_WINDOW, window = 3, server.DATA_WINDOW
c = server.create_campaign("", "data", CSV)
server.q("UPDATE campaigns SET status='running' WHERE id=?", c["id"], write=True)
server.next_campaign_step()
assert fetched == ["1", "2", "3"], fetched                 # only the window's pages
assert harvested == ["Gamma"], harvested                   # the one with videos
server.next_campaign_step()
assert fetched == ["1", "2", "3", "4"] and harvested == ["Gamma", "Alpha"], (fetched, harvested)   # one more page per climb
server.DATA_WINDOW = window
print("data OK")

# --- failures don't end the run ----------------------------------------------------------------
fresh()
outcome.update(Alpha="poor", Beta="boom")
c = server.create_campaign("", "popularity", CSV)
run_all(c["id"])
got = items(c["id"])
assert got["Alpha"]["status"] == "skipped" and "viewpoints" in got["Alpha"]["note"] and not got["Alpha"]["mesh_id"], got["Alpha"]
assert got["Beta"]["status"] == "failed" and "only 2 image(s)" in got["Beta"]["note"], got["Beta"]
assert got["Gamma"]["status"] == "done", got["Gamma"]
assert got["Alpha2"]["status"] == "done" and "Alpha2" in harvested, got["Alpha2"]   # its boulder has no mesh yet: worth trying
print("failures OK")

# --- an outage stops the run rather than failing the whole list --------------------------------
fresh()
outcome.update({n: "outage" for n in ("Alpha", "Beta", "Gamma", "Alpha2", "Delta", "Delta2")})
server.OUTAGE_LIMIT, limit = 3, server.OUTAGE_LIMIT
c = server.create_campaign("", "popularity", CSV)
run_all(c["id"])
got = items(c["id"])
camp = server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True)
assert camp["status"] == "stopped" and "no network" in camp["note"], camp
assert harvested == ["Alpha", "Beta", "Gamma"] and all(i["status"] == "pending" for i in got.values()), (harvested, got)
server.OUTAGE_LIMIT = limit
print("outage OK")

# --- stop now, for real, on the worker thread --------------------------------------------------
fresh()
outcome["Beta"] = "stop"
c = server.create_campaign("", "popularity", CSV)
stop_worker = threading.Event()


def loop():
    while not stop_worker.is_set():
        server.next_campaign_step()
        time.sleep(0.01)


t = threading.Thread(target=loop, daemon=True)
t.start()
assert started.wait(20), "never got to Beta"
time.sleep(0.2)
assert server._campaign_running == c["id"]
killed = time.time()
server.stop_campaign(c["id"], now=True)
for _ in range(500):
    if server._campaign_running is None:
        break
    time.sleep(0.02)
assert server._campaign_running is None and time.time() - killed < 5, "tool wasn't killed"
got = items(c["id"])
assert server.campaign_status(c["id"]) == "stopped", server.campaign_status(c["id"])
assert got["Alpha"]["status"] == "done" and (server.MESHES / got["Alpha"]["mesh_id"] / "mesh.ply").exists(), got["Alpha"]   # kept
assert got["Beta"]["status"] == "pending" and "starts again" in got["Beta"]["note"] and not got["Beta"]["project_id"], got["Beta"]
assert server.q("SELECT COUNT(*) n FROM projects WHERE name LIKE 'Beta%'", one=True)["n"] == 0   # no half-made project left
time.sleep(0.2)
assert harvested == ["Alpha", "Beta"], harvested                  # and nothing else started
print("stop now OK")

# --- resume picks up where it left off ---------------------------------------------------------
outcome.clear()
server.q("UPDATE campaigns SET status='running' WHERE id=?", c["id"], write=True)
for _ in range(500):
    if server.campaign_status(c["id"]) == "done":
        break
    time.sleep(0.02)
stop_worker.set()
t.join(5)
got = items(c["id"])
assert server.campaign_status(c["id"]) == "done" and got["Beta"]["status"] == "done", got
assert harvested == ["Alpha", "Beta", "Beta", "Gamma", "Delta", "Delta2"], harvested   # Alpha isn't redone
print("resume OK")

# --- stop after this climb ---------------------------------------------------------------------
fresh()
c = server.create_campaign("", "popularity", CSV)
real = server.run_harvest


def stop_during(pid, sources, reconstruct, relevance_filter=True):
    server.stop_campaign(c["id"], now=False)
    assert server.campaign_status(c["id"]) == "stopping"
    real(pid, sources, reconstruct, relevance_filter)


server.run_harvest = stop_during
server.next_campaign_step()
server.run_harvest = real
got = items(c["id"])
assert server.campaign_status(c["id"]) == "stopped" and got["Alpha"]["status"] == "done", got["Alpha"]
assert got["Beta"]["status"] == "pending", got["Beta"]
print("stop after this climb OK")

# --- a server restart: finished work stays, nothing heavy restarts by itself -------------------
fresh()
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
sid = items(c["id"])["Beta"]["source_id"]
pid = server.new_id()
(server.PROJECTS / pid / "photos").mkdir(parents=True)
server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,'running')", pid, "Beta (harvest)", time.time(), "harvest", write=True)
server.set_item(c["id"], sid, status="running", project_id=pid)
server.init_db()
got = items(c["id"])
camp = server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True)
assert camp["status"] == "stopped" and "restart" in camp["note"], camp
assert got["Alpha"]["status"] == "done" and got["Beta"]["status"] == "pending", got
assert (server.MESHES / got["Alpha"]["mesh_id"] / "mesh.ply").exists()
harvested.clear()
server.next_campaign_step()
assert harvested == [], harvested                                  # stays stopped until asked
server.q("UPDATE campaigns SET status='running' WHERE id=?", c["id"], write=True)
server.next_campaign_step()
got = items(c["id"])
assert harvested == ["Beta"] and got["Beta"]["status"] == "done" and got["Beta"]["project_id"] != pid, (harvested, got["Beta"])
assert not (server.PROJECTS / pid).exists() and not server.q("SELECT id FROM projects WHERE id=?", pid), "partial attempt left behind"
print("restart OK")

# --- the view the site polls -------------------------------------------------------------------
v = server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))
assert v["counts"]["done"] == 2 and v["counts"]["pending"] == 4 and [i["name"] for i in v["worked"]] == ["Beta", "Alpha"], v
assert [i["name"] for i in v["next"]][:2] == ["Gamma", "Alpha2"] and v["current"] is None, v
json.dumps(v)
print("view OK")

# --- stopping a campaign that isn't on a climb leaves the job control alone, so can't hit another job
fresh()
c = server.create_campaign("", "popularity", CSV, start=False)
assert server.campaign_status(c["id"]) == "stopped"
jobctl.begin()
server.stop_campaign(c["id"])
jobctl.check()
print("ALL CAMPAIGN TESTS PASSED")
