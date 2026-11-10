#!/usr/bin/env python3
"""Meshing a whole area: order, a mesh per climb, boulders' own meshes, stopping, restarts. Offline: the harvest and
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
outcome: dict[str, str] = {}      # climb name -> "poor", "boom", "few", "empty", "outage", "stop": what its harvest does
harvested: list[str] = []
videos = {"Gamma": 2}             # how many videos each climb's page has
started = threading.Event()
fetched: list[str] = []


pools: dict[str, list[str]] = {}     # climb or boulder name -> the projects its harvest pooled photos from
anchors: dict[str, list[str]] = {}   # climb name -> the projects its harvest matched web photos against


def fake_fetch(url, include_children=False):
    rid = url.split("/")[-2]
    fetched.append(rid)
    name = url.split("/")[-1].capitalize()
    item = server.q("SELECT data FROM campaign_items WHERE source_id=? LIMIT 1", rid, one=True)    # its page's breadcrumbs
    return [Climb(source="mountainproject", source_id=rid, url=url, name=name, kind="boulder", lat=34.0, lng=-116.0,
                  area_path=json.loads(item["data"])["area_path"] if item else [],
                  boulder_name=json.loads(item["data"])["boulder_name"] if item else None,
                  images=[Media(url=f"http://x/{i}.jpg", kind="image") for i in range(3)],
                  videos=[Media(url=f"http://y/{i}", kind="video") for i in range(videos.get(name, 0))])]


def fake_harvest(c, project_dir, log, sources, relevance_filter=True, tracker=None, pool_from=None, anchors_from=None, want=None):
    harvested.append(c.name)
    pools[c.name] = [p.name for p in pool_from or []]
    anchors[c.name] = [p.name for p in anchors_from or []]
    what = outcome.get(c.name) if sources else outcome.get("pooled")
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
        raise RuntimeError("OpenMVS crashed")
    if outcome.get(name.split(" (")[0]) == "few":
        raise RuntimeError("only 2 image(s) could be registered (need >= 6); the photos may not overlap enough for photogrammetry")
    if outcome.get(name.split(" (")[0]) == "empty":
        raise RuntimeError("the mesh has only 0 face(s) (need >= 1000): too few points were found in the photos to build a surface")
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

# --- popularity order: the export's own; a mesh per climb, whatever else is on its boulder ------------
c = server.create_campaign("", "popularity", CSV)
assert c["name"] == "Sector" and c["status"] == "running", c
run_all(c["id"])
assert harvested == ["Alpha", "Beta", "Gamma", "Alpha2", "Delta", "Delta2"], harvested   # Alpha2 has its own, on Alpha's rock
got = items(c["id"])
assert server.campaign_status(c["id"]) == "done" and all(i["status"] == "done" for i in got.values()), got
assert got["Alpha2"]["mesh_id"] != got["Alpha"]["mesh_id"] and got["Alpha2"]["note"] is None, got["Alpha2"]
assert anchors["Alpha2"] == [got["Alpha"]["project_id"]] and pools["Alpha2"] == [], "matched against Alpha's photos, not given them"
assert got["Delta2"]["mesh_id"] != got["Delta"]["mesh_id"]
assert server.q("SELECT COUNT(*) n FROM meshes", one=True)["n"] == 6
links = server.q("SELECT COUNT(*) n FROM mesh_links WHERE mesh_id=?", got["Alpha"]["mesh_id"], one=True)["n"]
assert links == 1, links
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
assert got["Beta"]["status"] == "failed" and "OpenMVS crashed" in got["Beta"]["note"], got["Beta"]
assert got["Gamma"]["status"] == "done", got["Gamma"]
assert got["Alpha2"]["status"] == "done" and "Alpha2" in harvested, got["Alpha2"]
assert got["Alpha"]["status"] == "skipped" and got["Alpha"]["mesh_id"] is None, "not linked to the mesh made for Alpha2"
assert not server.q("SELECT 1 FROM mesh_links WHERE climb_id=?", got["Alpha"]["climb_id"])
print("failures OK")

# --- a boulder's own mesh, from every photo of its climbs, apart from theirs ---------------------------------
fresh()
pools.clear()
outcome.update(Alpha="poor", Alpha2="poor")
c = server.create_campaign("", "popularity", CSV)
run_all(c["id"])
got = items(c["id"])
assert got["Alpha"]["status"] == got["Alpha2"]["status"] == "skipped", (got["Alpha"], got["Alpha2"])
assert [x["source_id"] for x in server.boulders_to_mesh(c["id"])] == [got["Alpha"]["source_id"]], "one per boulder, its top climb"
assert server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["again"]["boulders"] == 1
harvests = {got["Alpha"]["project_id"], got["Alpha2"]["project_id"]}
assert server.queue_boulders(c["id"]) == 1 and server.campaign_status(c["id"]) == "running"
assert server.boulders_to_mesh(c["id"]) == [] and server.queue_boulders(c["id"]) == 0, "queued once, not again by its other climb"
harvested.clear()
run_all(c["id"])
got = items(c["id"])
proj = server.q("SELECT * FROM projects WHERE kind='boulder'", one=True)
boulder = server.boulder_of(server.climb_row(got["Alpha"]["climb_id"])["area_path"])
assert proj["name"] == "Alpha Boulder (boulder)" and proj["status"] == "done" and proj["boulder"] == boulder, proj
assert proj["climb_id"] is None, "nobody's climb in particular"
assert set(pools["Alpha Boulder"]) == harvests and harvested == ["Alpha Boulder"], (pools, harvested)
mesh = server.q("SELECT * FROM meshes WHERE id=?", proj["mesh_id"], one=True)
assert mesh["boulder"] == boulder and (mesh["lat"], mesh["lng"]) == (34.0, -116.0), mesh
assert not server.q("SELECT 1 FROM mesh_links WHERE mesh_id=?", mesh["id"]), "linked to no climb as its own"
assert got["Alpha"]["status"] == got["Alpha2"]["status"] == "skipped" and got["Alpha"]["mesh_id"] is None, got["Alpha"]
assert "its boulder meshed from 20 photos of 2 of its 2 climbs" in got["Alpha"]["note"] and got["Alpha"]["redo"] is None, got["Alpha"]
record = json.loads((server.PROJECTS / proj["id"] / server.CLIMBS_FILE).read_text())
assert {x["name"]: x["projects"] for x in record["climbs"]} == {"Alpha": [got["Alpha"]["project_id"]], "Alpha2": [got["Alpha2"]["project_id"]]}
assert "no cameras were kept" in server.read_log(proj["id"]), "the stand-in keeps none, so no views"
info = server.climb_row(got["Alpha"]["climb_id"], full=True)
assert info["meshes"] == [] and info["n_links"] == 0 and info["n_boulder"] == 1, info
assert [m["id"] for m in info["boulder"]["meshes"]] == [mesh["id"]] and [x["name"] for x in info["boulder"]["climbs"]] == ["Alpha2"], info["boulder"]
assert server.boulders_to_mesh(c["id"]) == [] and server.queue_boulders(c["id"]) == 0, "meshed once"

# meshing it again replaces its mesh, which stays as an older version; the climbs' own meshes are left alone
outcome.clear()
code, status = server.mesh_boulder(boulder)
assert status == 202 and code["project_id"] == proj["id"], code
assert server.mesh_boulder(boulder)[1] == 409, "already queued"
job = server.jobs.get_nowait()
assert job == {"type": "boulder", "pid": proj["id"]}, job
server.run_job(job)
again = server.q("SELECT * FROM projects WHERE id=?", proj["id"], one=True)
assert again["status"] == "done" and again["mesh_id"] != mesh["id"], again
assert server.q("SELECT replaced_by FROM meshes WHERE id=?", mesh["id"], one=True)["replaced_by"] == again["mesh_id"]
assert "pooling its climbs' photos again" in server.read_log(proj["id"])
print("boulder meshes OK")

# --- a boulder's mesh harvests first its climbs an area hasn't reached; the area then meshes them from that ----
fresh()
pools.clear()
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()     # Alpha
server.stop_campaign(c["id"])
got = items(c["id"])
assert got["Alpha"]["status"] == "done" and got["Alpha2"]["status"] == "pending" and got["Alpha2"]["climb_id"] is None, got
assert [x["source_id"] for x in server.boulders_to_mesh(c["id"])] == [got["Alpha"]["source_id"]]
harvested.clear()
assert server.queue_boulders(c["id"]) == 1
run_all(c["id"])
got = items(c["id"])
assert harvested == ["Alpha2", "Alpha Boulder"], harvested
assert got["Alpha2"]["climb_id"] and got["Alpha2"]["project_id"] and got["Alpha2"]["status"] == "pending", got["Alpha2"]
assert server.q("SELECT status FROM projects WHERE id=?", got["Alpha2"]["project_id"], one=True)["status"] == "harvested"
assert set(pools["Alpha Boulder"]) == {got["Alpha"]["project_id"], got["Alpha2"]["project_id"]}
assert server.campaign_status(c["id"]) == "stopped", "it ran only what was queued"
harvested.clear()
server.q("UPDATE campaigns SET status='running' WHERE id=?", c["id"], write=True)
run_all(c["id"])
got = items(c["id"])
assert "Alpha2" not in harvested and got["Alpha2"]["status"] == "done" and got["Alpha2"]["mesh_id"], (harvested, got["Alpha2"])
print("boulder harvests OK")

# --- COLMAP placing too few is a skip, not a failure; skipped climbs the viewpoint check now lets through ---
fresh()
outcome.update(Gamma="few")
c = server.create_campaign("", "popularity", CSV)
run_all(c["id"])
got = items(c["id"])["Gamma"]
assert got["status"] == "skipped" and got["note"] == "COLMAP could place only 2 of its photos together", got
server.set_item(c["id"], got["source_id"], status="failed", note="as recorded before such failures were skipped")
assert server.settle_too_few_placed() == 1 and server.settle_too_few_placed() == 0, "once"
again = items(c["id"])["Gamma"]
assert again["status"] == "skipped" and again["note"] == "COLMAP could place only 2 of its photos together", again
fresh()
outcome.update(Gamma="empty")
c = server.create_campaign("", "popularity", CSV)
run_all(c["id"])
got = items(c["id"])["Gamma"]
assert got["status"] == "skipped" and got["note"] == "its photos made a mesh of only 0 faces" and not got["mesh_id"] and got["climb_id"], got
assert not server.q("SELECT 1 FROM mesh_links WHERE climb_id=?", got["climb_id"]), "nothing linked to a mesh that wasn't made"
fresh()
outcome.update(Gamma="poor")
c = server.create_campaign("", "popularity", CSV)
run_all(c["id"])
gamma = items(c["id"])["Gamma"]
assert gamma["status"] == "skipped" and server.triable(c["id"]) == [], "the fake's harvest has no view graph: nothing to judge again"
summary = json.loads(server.q("SELECT harvest FROM projects WHERE id=?", gamma["project_id"], one=True)["harvest"])
summary["diversity"] = {"verdict": "poor", "unique_images": 20, "estimated_registerable": 4, "view_graph": {"available": True}}
server.q("UPDATE projects SET harvest=? WHERE id=?", json.dumps(summary), gamma["project_id"], write=True)
assert [x["source_id"] for x in server.triable(c["id"])] == [gamma["source_id"]], "4 of 20 views is worth COLMAP's try now"
assert server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["again"]["triable"] == 1
outcome.clear()
assert server.queue_redo(c["id"], "try-skipped") == 1
run_all(c["id"])
got = items(c["id"])["Gamma"]
assert got["status"] == "done" and got["mesh_id"] and got["redo"] is None, got
print("viewpoint check OK")

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
assert c["id"] in server.busy_campaigns()
killed = time.time()
server.stop_campaign(c["id"], now=True)
for _ in range(500):
    if not server.busy_campaigns():
        break
    time.sleep(0.02)
assert not server.busy_campaigns() and time.time() - killed < 5, "tool wasn't killed"
got = items(c["id"])
assert server.campaign_status(c["id"]) == "stopped", server.campaign_status(c["id"])
assert got["Alpha"]["status"] == "done" and (server.MESHES / got["Alpha"]["mesh_id"] / "mesh.ply").exists(), got["Alpha"]   # kept
assert got["Beta"]["status"] == "pending" and "carries on" in got["Beta"]["note"] and got["Beta"]["project_id"], got["Beta"]
paused = got["Beta"]["project_id"]
assert server.q("SELECT status FROM projects WHERE id=?", paused, one=True)["status"] == "stopped", "paused, not busy or lost"
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
assert harvested == ["Alpha", "Beta", "Beta", "Gamma", "Alpha2", "Delta", "Delta2"], harvested   # Alpha isn't redone
assert got["Beta"]["project_id"] == paused, "the same project carries on"
print("resume OK")

# --- stop after this climb ---------------------------------------------------------------------
fresh()
c = server.create_campaign("", "popularity", CSV)
real = server.run_harvest


def stop_during(pid, sources, reconstruct, relevance_filter=True, resume=False):
    server.stop_campaign(c["id"], now=False)
    assert server.campaign_status(c["id"]) == "stopping"
    real(pid, sources, reconstruct, relevance_filter, resume)


server.run_harvest = stop_during
server.next_campaign_step()
server.run_harvest = real
got = items(c["id"])
assert server.campaign_status(c["id"]) == "stopped" and got["Alpha"]["status"] == "done", got["Alpha"]
assert got["Beta"]["status"] == "pending", got["Beta"]
print("stop after this climb OK")

# --- a server restart only pauses it: each climb carries on from where it got to
fresh()
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()


def interrupted(name: str, **project) -> str:
    """Leaves a climb as a restart would find it, in the middle of its project."""
    i = items(c["id"])[name]
    pid = server.new_id()
    (server.PROJECTS / pid / "photos").mkdir(parents=True)
    climb_id = server.upsert_climb(Climb.from_dict(json.loads(i["data"])))
    server.q("INSERT INTO projects(id,name,created,kind,climb_id,status,harvest) VALUES(?,?,?,?,?,?,?)", pid, f"{name} (harvest)", time.time(),
             "harvest", climb_id, project.get("status", "harvesting"), project.get("harvest"), write=True)
    server.set_item(c["id"], i["source_id"], status="running", project_id=pid, climb_id=climb_id)
    return pid


# in the middle of its harvest: that starts again, in the same project, from nothing
pid = interrupted("Beta")
(server.PROJECTS / pid / "photos" / "half_downloaded.jpg").write_bytes(b"x")
other = server.new_id()
server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,'running')", other, "someone's own job", time.time(), "user", write=True)
server.init_db()
got = items(c["id"])
assert server.campaign_status(c["id"]) == "running", "carries on by itself"
assert got["Alpha"]["status"] == "done" and (server.MESHES / got["Alpha"]["mesh_id"] / "mesh.ply").exists(), got["Alpha"]
assert got["Beta"]["status"] == "pending" and got["Beta"]["project_id"] == pid and "carrying on" in got["Beta"]["note"], got["Beta"]
assert server.q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "stopped"
assert server.q("SELECT status FROM projects WHERE id=?", other, one=True)["status"] == "interrupted", "jobs started by hand are lost"
harvested.clear()
server.next_campaign_step()
got = items(c["id"])
assert harvested == ["Beta"] and got["Beta"]["status"] == "done" and got["Beta"]["project_id"] == pid, (harvested, got["Beta"])
assert not (server.PROJECTS / pid / "photos" / "half_downloaded.jpg").exists()
assert "carrying on after an interruption" in server.read_log(pid), server.read_log(pid)

# in the middle of its reconstruction: no new harvest, and its finished steps survive
summary = json.dumps({"images": 20, "diversity": {"verdict": "good"}, "video_frames": 0, "queries": []})
pid = interrupted("Gamma", status="running", harvest=summary)
(server.PROJECTS / pid / "work" / "finished").mkdir(parents=True)
(server.PROJECTS / pid / "work" / "finished" / "matching").touch()
seen_finished = []
reconstruct = pipeline.reconstruct


def check_work(images, work, out_dir, log, use_gpu=False, internet=False, tracker=None):
    seen_finished.extend(p.name for p in (work / "finished").iterdir())
    return reconstruct(images, work, out_dir, log, use_gpu, internet, tracker)


pipeline.reconstruct = check_work
server.init_db()
harvested.clear()
server.next_campaign_step()
pipeline.reconstruct = reconstruct
got = items(c["id"])
assert harvested == [] and seen_finished == ["matching"], (harvested, seen_finished)
assert got["Gamma"]["status"] == "done" and got["Gamma"]["project_id"] == pid and got["Gamma"]["mesh_id"], got["Gamma"]

# in the moment after its mesh was saved: it isn't made twice
pid = interrupted("Alpha2", status="running", harvest=summary)
server.q("UPDATE campaign_items SET boulder=NULL WHERE campaign_id=? AND name='Alpha2'", c["id"], write=True)
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng) VALUES('bbb1','saved','mesh.ply',?,1,1,0,NULL,NULL)", f"project:{pid}", write=True)
server.init_db()
harvested.clear()
before = server.q("SELECT COUNT(*) n FROM meshes", one=True)["n"]
server.next_campaign_step()
got = items(c["id"])
assert got["Alpha2"]["status"] == "done" and got["Alpha2"]["mesh_id"] == "bbb1" and not harvested, got["Alpha2"]
assert server.q("SELECT COUNT(*) n FROM meshes", one=True)["n"] == before
assert server.q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "done"

# stopped stays stopped, and "after this climb" still stops
fresh()
c = server.create_campaign("", "popularity", CSV)
server.stop_campaign(c["id"])
server.init_db()
assert server.campaign_status(c["id"]) == "stopped"
server.next_campaign_step()
assert harvested == [], harvested
server.q("UPDATE campaigns SET status='stopping' WHERE id=?", c["id"], write=True)
server.init_db()
assert server.campaign_status(c["id"]) == "stopped"

# whichever the order, an interrupted climb goes first, and a project deleted meanwhile just means a new one
for order in ("rating", "data"):
    c2 = server.create_campaign("", order, CSV, start=False)
    server.set_item(c2["id"], "4", project_id="gone")
    assert server.ordered_pending(c2)[0]["name"] == "Alpha2", server.ordered_pending(c2)[0]
fresh()
c = server.create_campaign("", "popularity", CSV)
server.set_item(c["id"], "1", project_id="gone")
server.next_campaign_step()
got = items(c["id"])
assert harvested == ["Alpha"] and got["Alpha"]["status"] == "done" and got["Alpha"]["project_id"] != "gone", got["Alpha"]
print("restart OK")

# --- jobs started outside areas: a restart marks them interrupted, not failed, and Retry runs them again
fresh()


def own_project(status: str, job: dict | None = None) -> str:
    pid = server.new_id()
    (server.PROJECTS / pid / "photos").mkdir(parents=True)
    for i in range(3):
        (server.PROJECTS / pid / "photos" / f"p{i}.jpg").write_bytes(b"x")
    server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,?)", pid, f"mine {status}", time.time(), "user", status, write=True)
    if job:
        server.queue_job({**job, "pid": pid})
    return pid


ran = own_project("running", {"type": "reconstruct", "internet": False})
waited = own_project("queued", {"type": "reconstruct", "internet": False})
st = server.jobs_state()
assert [j["name"] for j in st["queued"]] == ["mine running", "mine queued"] and st["running"] == [] and st["stopping"] is None, st
with server.jobs.mutex:
    server.jobs.queue.clear()     # lost with the restart
server.init_db()
for pid, when in ((ran, "during this job"), (waited, "before this job started")):
    row = server.q("SELECT status, job FROM projects WHERE id=?", pid, one=True)
    assert row["status"] == "interrupted" and when in server.last_failure(pid, "INTERRUPTED: "), (row, server.read_log(pid))
assert json.loads(server.q("SELECT job FROM projects WHERE id=?", ran, one=True)["job"])["resume"], "it had started: Retry carries on with it"
assert server.retry_project(ran) == ({"queued": True}, 202) and server.retry_project(waited)[1] == 202
with server.jobs.mutex:
    again = list(server.jobs.queue)
    server.jobs.queue.clear()
assert again == [{"type": "reconstruct", "internet": False, "pid": ran, "resume": True},
                 {"type": "reconstruct", "internet": False, "pid": waited}], "as they were asked for"
server.q("UPDATE projects SET status='failed' WHERE id=?", ran, write=True)
assert server.retry_project(ran)[1] == 202 and server.jobs.queue[-1] == {"type": "retry", "pid": ran}, "a failure: tried again"
with server.jobs.mutex:
    server.jobs.queue.clear()
assert server.retry_project(waited)[1] == 409, "already queued"

# a stop waits for a job outside an area that looks like finishing in time, but not for one that doesn't
w = server.Worker("this server 2")
server._workers.append(w)
server.q("UPDATE projects SET status='running', progress=? WHERE id=?", json.dumps({"overall": 50.0, "steps": []}), ran, write=True)
w.job = {"job": {"type": "retry", "pid": ran}, "started": time.time() - 120, "from": 0.0}    # half done in 2 min
server.STOP_WAIT = 30


class FakeServer:
    def __init__(self):
        self.stopped = threading.Event()

    def shutdown(self):
        self.stopped.set()


said = []
real_print = print
server.print = lambda *a, **k: said.append(" ".join(map(str, a)))
fake = FakeServer()
stopper = threading.Thread(target=server.stop_when_idle, args=(fake,))
stopper.start()
stopper.join(5)
assert fake.stopped.is_set() and not stopper.is_alive(), "it didn't wait for it"
assert any("not waiting for mine running (retry, 50%, about 2 min left)" in x for x in said), said
assert server._stopping["waiting_for"] == []

server._stopping, said[:] = None, []
fake = FakeServer()
w.job["started"] = time.time() - 10     # half done in 10 s: about 10 s to go
stopper = threading.Thread(target=server.stop_when_idle, args=(fake,))
stopper.start()
time.sleep(0.5)
assert not fake.stopped.is_set() and "waiting up to 1 min" in said[0] and "mine running (retry, 50%" in said[0], said
before = len(said)
server.queue_job({"type": "reconstruct", "pid": waited, "internet": False})
taker = server.Worker("this server 3")
server._workers.append(taker)
threading.Thread(target=server.worker, args=(taker,), daemon=True).start()
time.sleep(2.5)
assert taker.job is None and server.jobs.qsize() == 1, "no new work while stopping"
w.job = None      # it finished
stopper.join(5)
assert fake.stopped.is_set() and any("1 queued job(s) won't start" in x and "Retry on its project" in x for x in said[before:]), said
taker.retired = True
with server.jobs.mutex:
    server.jobs.queue.clear()
server._stopping = None
server.print = real_print
server._workers[:] = [server._first]
print("interrupted jobs OK")

# --- new meshes get a cleanup, existing ones are caught up, and failing only costs that -----------
fresh()
cleaned, real_clean = [], server.meshclean.clean
server.meshclean.clean = lambda d, log=print, force=False: cleaned.append(d.name)
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
alpha = items(c["id"])["Alpha"]["mesh_id"]
assert cleaned == [alpha], (cleaned, alpha)


def broken(d, log=print, force=False):
    raise ValueError("not a GLB file")


server.meshclean.clean = broken
server.next_campaign_step()
got = items(c["id"])
assert got["Beta"]["status"] == "done" and got["Beta"]["mesh_id"], "a failed cleanup mustn't cost the mesh"
assert any("cleanup failed (not a GLB file)" in line for line in server.read_log(got["Beta"]["project_id"]).splitlines())
cleaned.clear()
server.meshclean.clean = lambda d, log=print, force=False: cleaned.append(d.name)
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng) VALUES('aaa1','old','mesh.glb','project:x',1,1,0,NULL,NULL)", write=True)
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng) VALUES('aaa2','done already','mesh.glb','project:y',2,1,0,NULL,NULL)", write=True)
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng) VALUES('aaa3','deleted','mesh.glb','project:z',3,1,0,NULL,NULL)", write=True)
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng) VALUES('aaa4','an upload','mesh.glb','upload',4,1,0,NULL,NULL)", write=True)
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng) VALUES('aaa5','untextured','mesh.ply','project:p',5,1,0,NULL,NULL)", write=True)
for mid, content in (("aaa2", {"removed": []}), ("aaa3", {"deleted": True})):
    (server.MESHES / mid).mkdir(parents=True)
    (server.MESHES / mid / "cleanup.json").write_text(json.dumps(content))
server.clean_existing_meshes()
assert "aaa1" in cleaned and not {"aaa2", "aaa3", "aaa4", "aaa5"} & set(cleaned), cleaned
server.meshclean.clean = real_clean
print("cleanup OK")

# --- existing meshes get a report on which way is down: from a model if it's still there
reports = []
real_save, real_load, real_check = server.orientation.save, server.orientation.load, server.orientation.check
server.orientation.save = lambda d, report, model=None: reports.append((d.name, report["iphone"]["status"], model is not None))
server.orientation.load = lambda d: {"x": 1} if d.name == "aaa2" else None
server.orientation.check = lambda model, photos: {"iphone": {"status": "used"}}
(server.PROJECTS / "x" / "work" / "aligned").mkdir(parents=True)
(server.PROJECTS / "x" / "work" / "aligned" / "images.bin").write_bytes(b"")
server.q("DELETE FROM meshes WHERE id NOT IN ('aaa1','aaa2','aaa3','aaa4')", write=True)
server.orient_existing_meshes()
assert ("aaa1", "not_applied", True) in reports, "already built: the reading is reported, not applied"
assert ("aaa3", "no_cameras", False) in reports and not [r for r in reports if r[0] in ("aaa2", "aaa4")], reports
server.orientation.save, server.orientation.load, server.orientation.check = real_save, real_load, real_check
print("orientation reports OK")

# --- the view the site polls -------------------------------------------------------------------
fresh()
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
server.next_campaign_step()
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
# --- trying failed climbs again ----------------------------------------------------------------
server.climbs_mod.fetch_climb_url = fake_fetch
server.harvest_mod.harvest = fake_harvest
pipeline.reconstruct = fake_reconstruct
jobctl.begin()


def photos(pid: str) -> None:
    for n in range(3):
        (server.PROJECTS / pid / "photos" / f"{n}.jpg").write_bytes(b"x")


def status(cid: str) -> tuple[str, int]:
    camp = server.q("SELECT status,only_retries FROM campaigns WHERE id=?", cid, one=True)
    return camp["status"], camp["only_retries"]


# a finished area: only what failed runs, without harvesting again, and its work is kept for it
fresh()
outcome["Gamma"] = "boom"
c = server.create_campaign("", "popularity", CSV)
run_all(c["id"])
assert status(c["id"]) == ("done", 0), status(c["id"])
got = items(c["id"])
assert got["Gamma"]["status"] == "failed" and "OpenMVS crashed" in got["Gamma"]["note"], got["Gamma"]
pid = got["Gamma"]["project_id"]
assert (server.PROJECTS / pid / "work" / "big.bin").exists(), "a failed climb keeps its work, to carry on from"
assert not (server.PROJECTS / got["Beta"]["project_id"] / "work").exists(), "a finished one doesn't"
assert server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["counts"]["failed"] == 1
outcome.clear()
harvested.clear()
assert server.retry_campaign(c["id"]) == 1
assert status(c["id"]) == ("running", 1), status(c["id"])
got = items(c["id"])
assert got["Gamma"]["status"] == "pending" and got["Gamma"]["retry"] == 1 and got["Gamma"]["note"] == "queued to try again", got["Gamma"]
assert server.q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "queued"
run_all(c["id"])
got = items(c["id"])
assert status(c["id"]) == ("done", 0), status(c["id"])
assert got["Gamma"]["status"] == "done" and got["Gamma"]["project_id"] == pid and got["Gamma"]["mesh_id"] and got["Gamma"]["retry"] == 0, got["Gamma"]
assert harvested == [], "its photos were already harvested"
assert not (server.PROJECTS / pid / "work").exists(), "finished now"
assert "-- trying again" in server.read_log(pid)
assert server.retry_campaign(c["id"]) == 0, "nothing left to try"
assert status(c["id"]) == ("done", 0)

# a stopped area: only what failed runs, and then it stops again, rather than going on down its list
fresh()
outcome["Alpha"] = "boom"
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
server.stop_campaign(c["id"])
assert items(c["id"])["Alpha"]["status"] == "failed" and status(c["id"]) == ("stopped", 0)
outcome.clear()
harvested.clear()
assert server.retry_campaign(c["id"]) == 1
run_all(c["id"])
assert harvested == [], "a climb that failed in reconstruction keeps its photos"
got = items(c["id"])
assert got["Alpha"]["status"] == "done" and status(c["id"]) == ("stopped", 0), (got["Alpha"], status(c["id"]))
assert got["Beta"]["status"] == "pending", "the rest of the list stays waiting"

# one that fails again is left failed, with no endless retrying
fresh()
outcome["Alpha"] = "boom"
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
server.stop_campaign(c["id"])
server.retry_campaign(c["id"])
run_all(c["id"])
got = items(c["id"])
assert got["Alpha"]["status"] == "failed" and got["Alpha"]["retry"] == 0 and "OpenMVS crashed" in got["Alpha"]["note"], got["Alpha"]
assert status(c["id"]) == ("stopped", 0)

# a running area: what failed goes next, and it carries on down its list afterwards
fresh()
outcome["Alpha"] = "boom"
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
outcome.clear()
harvested.clear()
assert server.retry_campaign(c["id"]) == 1
assert status(c["id"]) == ("running", 0), "it's still working through its whole list"
assert server.ordered_pending(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))[0]["name"] == "Alpha"
server.next_campaign_step()
assert items(c["id"])["Alpha"]["status"] == "done" and harvested == []
server.next_campaign_step()
assert harvested == ["Beta"], harvested

# stopping retries leaves them first in line for Resume, which then does the whole list
fresh()
outcome["Alpha"] = outcome["Beta"] = "boom"
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
server.next_campaign_step()
server.stop_campaign(c["id"])
outcome.clear()
assert server.retry_campaign(c["id"]) == 2
server.next_campaign_step()
server.stop_campaign(c["id"])
assert status(c["id"]) == ("stopped", 0), status(c["id"])
got = items(c["id"])
assert got["Alpha"]["status"] == "done" and got["Beta"]["status"] == "pending" and got["Beta"]["retry"] == 1, got["Beta"]
assert server.ordered_pending(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))[0]["name"] == "Beta"

# a restart during retries carries on with them, and still stops afterwards
fresh()
outcome["Alpha"] = "boom"
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
server.stop_campaign(c["id"])
outcome.clear()
server.retry_campaign(c["id"])
pid = items(c["id"])["Alpha"]["project_id"]
server.init_db()
assert server.q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "stopped", "not lost as a queued job would be"
assert status(c["id"]) == ("running", 1)
assert items(c["id"])["Alpha"]["retry"] == 1
run_all(c["id"])
assert items(c["id"])["Alpha"]["status"] == "done" and status(c["id"]) == ("stopped", 0), status(c["id"])

# one project of a stopped area
fresh()
outcome["Alpha"] = outcome["Beta"] = "boom"
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
server.next_campaign_step()
server.stop_campaign(c["id"])
outcome.clear()
pid = items(c["id"])["Beta"]["project_id"]
photos(pid)
assert server.retry_project("0" * 12) == ({"error": "not found"}, 404)
assert server.retry_project(pid) == ({"queued": True}, 202)
assert server.retry_project(pid)[1] == 409, "already queued"
assert status(c["id"]) == ("running", 1)
run_all(c["id"])
got = items(c["id"])
assert got["Beta"]["status"] == "done" and got["Alpha"]["status"] == "failed", "only the one asked for"
assert status(c["id"]) == ("stopped", 0)
assert server.retry_project(pid)[1] == 409, "it's done: there's nothing to retry"

# a project that isn't part of an area, which keeps its work from the failed run
fresh()
pid = "abc123abc123"
(server.PROJECTS / pid / "photos").mkdir(parents=True, exist_ok=True)
server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,'failed')", pid, "Solo", time.time(), "user", write=True)
assert server.retry_project(pid) == ({"error": "upload at least 3 overlapping photos"}, 400) or not pipeline.real_pipeline_ready()
photos(pid)
(server.PROJECTS / pid / "work").mkdir(exist_ok=True)
(server.PROJECTS / pid / "work" / "earlier.bin").write_bytes(b"x")
assert server.retry_project(pid) == ({"queued": True}, 202)
job = server.jobs.get_nowait()
assert job == {"type": "retry", "pid": pid}, job
server.run_retry(pid)
assert server.q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "done"
assert (server.PROJECTS / pid / "work" / "earlier.bin").exists(), "it carried on, rather than starting from scratch"
assert "-- trying again" in server.read_log(pid)

# reconstructing a failed climb's project by hand counts towards its area
fresh()
outcome["Alpha"] = "boom"
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
outcome.clear()
server.stop_campaign(c["id"])
server.run_reconstruct(items(c["id"])["Alpha"]["project_id"])
got = items(c["id"])["Alpha"]
assert got["status"] == "done" and got["mesh_id"] and got["note"] is None, got

# what a tool's failure says reaches the area's list
fresh()


def killed(images, work, out_dir, log, use_gpu=False, internet=False, tracker=None):
    work.mkdir(parents=True, exist_ok=True)
    raise server.diagnostics.ToolFailed("/usr/local/bin/OpenMVS/DensifyPointCloud", -15, "sent from outside this container, by uid 61876")


pipeline.reconstruct = killed
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
assert items(c["id"])["Alpha"]["note"] == ("DensifyPointCloud was killed with SIGTERM (signal 15), not by this app: "
                                            "sent from outside this container, by uid 61876"), items(c["id"])["Alpha"]
pipeline.reconstruct = fake_reconstruct

# databases from before retries get what they need
fresh()
with server.db() as con:
    con.execute("ALTER TABLE campaign_items DROP COLUMN retry")
    con.execute("ALTER TABLE campaigns DROP COLUMN only_retries")
server.init_db()
c = server.create_campaign("", "popularity", CSV)
assert status(c["id"]) == ("running", 0)
assert items(c["id"])["Alpha"]["retry"] == 0
print("retries OK")
# --- harvesting and rebuilding an area again ---------------------------------------------------
photo_sets: dict[str, list[str]] = {}    # the photos each climb's harvest finds
searched: dict[str, list[str]] = {}      # the videos a YouTube search finds of each climb


def harvest_with_photos(c, project_dir, log, sources, relevance_filter=True, tracker=None, pool_from=None, anchors_from=None, want=None):
    harvested.append(c.name)
    for n in photo_sets.get(c.name, ["a.jpg", "b.jpg", "c.jpg"]):
        (project_dir / "photos" / n).write_bytes(b"x")
    (project_dir / "sources.json").write_text(json.dumps({"climb": {}, "files": [    # frames of the videos it found, as a real one records
        {"file": f"yt_{i:08x}_000.jpg", "source": "youtube", "url": u} for i, u in enumerate(searched.get(c.name, []))]}))
    return {"images": 3, "video_frames": 1, "queries": [], "diversity": {"verdict": "poor" if outcome.get(c.name) == "poor" else "good"},
            "videos": [{"url": f"https://www.youtube.com/watch?v={c.name[:6]:x<11}", "found_by": "search", "title": c.name,
                        "description": "I'm 5'10\" with a +2 ape index", "frames": 1, "license": "YouTube standard licence (all rights reserved)"}]}


server.harvest_mod.harvest = harvest_with_photos
server.youtube_mod.search = lambda c, log, known=frozenset(): [Media(url=u, kind="video", title=c.name, source="youtube-search")
                                                              for u in searched.get(c.name, [])]
fresh()
for t in ("videos", "climb_videos"):
    server.q(f"DELETE FROM {t}", write=True)
outcome["Gamma"] = "poor"
c = server.create_campaign("", "popularity", CSV)
run_all(c["id"])
got = items(c["id"])
assert [got[n]["status"] for n in ("Alpha", "Beta", "Gamma", "Alpha2")] == ["done", "done", "skipped", "done"], got
v = server.q("SELECT * FROM videos WHERE title='Alpha'", one=True)
assert (v["height_cm"], v["ape_index_cm"], v["first_person"]) == (177.8, 5.1, 1), v
link = server.q("SELECT * FROM climb_videos WHERE url=?", v["url"], one=True)
assert link["climb_id"] == got["Alpha"]["climb_id"] and link["found_by"] == "search" and link["frames"] == 1, link
assert "description" not in json.loads(server.q("SELECT harvest FROM projects WHERE id=?", got["Alpha"]["project_id"], one=True)["harvest"])["videos"][0]
old = {n: got[n]["mesh_id"] for n in ("Alpha", "Beta")}

# harvesting again: every climb with photos of its own, from scratch, keeping meshes and logs
outcome.clear()
harvested.clear()
photo_sets.update({"Beta": ["a.jpg", "b.jpg", "d.jpg"], "Gamma": ["e.jpg", "f.jpg", "g.jpg"]})
(server.PROJECTS / got["Beta"]["project_id"] / "photos" / "stale.jpg").write_bytes(b"x")
assert server.queue_redo(c["id"], "harvest") == 6
assert status(c["id"]) == ("running", 1)
assert server.q("SELECT status FROM projects WHERE id=?", got["Alpha"]["project_id"], one=True)["status"] == "done"
run_all(c["id"])
got = items(c["id"])
assert sorted(harvested) == ["Alpha", "Alpha2", "Beta", "Delta", "Delta2", "Gamma"], harvested
assert status(c["id"]) == ("done", 0), status(c["id"])
assert got["Alpha"]["status"] == "done" and got["Alpha"]["changed"] == 0 and "the same photos" in got["Alpha"]["note"], got["Alpha"]
assert got["Beta"]["status"] == "done" and got["Beta"]["changed"] == 1 and got["Beta"]["mesh_id"] == old["Beta"], got["Beta"]
assert not (server.PROJECTS / got["Beta"]["project_id"] / "photos" / "stale.jpg").exists(), "from scratch"
assert got["Gamma"]["status"] == "skipped" and got["Gamma"]["changed"] == 1 and "Rebuild changed" in got["Gamma"]["note"], got["Gamma"]
assert got["Alpha"]["redo"] is None and got["Alpha"]["retry"] == 0
log = server.read_log(got["Beta"]["project_id"])
assert "-- harvesting again" in log and log.index("done") < log.index("-- harvesting again"), "its log is added to"
assert server.q("SELECT status FROM projects WHERE id=?", got["Beta"]["project_id"], one=True)["status"] == "done"
view = server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))
assert view["again"] == {"changed": 2, "queued": 0, "boulders": 1, "triable": 0}, view["again"]

# rebuilding the changed ones: a new mesh takes over the old one's climbs, which stays as an older version
assert server.queue_redo(c["id"], "rebuild-changed") == 2
run_all(c["id"])
got = items(c["id"])
assert got["Beta"]["status"] == "done" and got["Beta"]["mesh_id"] != old["Beta"] and got["Beta"]["changed"] == 0, got["Beta"]
assert "older version" in got["Beta"]["note"], got["Beta"]
assert server.q("SELECT replaced_by FROM meshes WHERE id=?", old["Beta"], one=True)["replaced_by"] == got["Beta"]["mesh_id"]
assert server.q("SELECT climb_id FROM mesh_links WHERE mesh_id=?", old["Beta"]) == [], "its climbs moved to the new one"
assert server.q("SELECT mesh_id FROM mesh_links WHERE climb_id=?", got["Beta"]["climb_id"])[0]["mesh_id"] == got["Beta"]["mesh_id"]
assert got["Gamma"]["status"] == "done" and got["Gamma"]["mesh_id"] and got["Gamma"]["note"] == "rebuilt", got["Gamma"]
assert got["Alpha"]["mesh_id"] == old["Alpha"], "unchanged, so not rebuilt"
assert not (server.PROJECTS / got["Beta"]["project_id"] / "work").exists()

# rebuilding all, each climb on a boulder its own
alpha2 = got["Alpha2"]["mesh_id"]
assert server.queue_redo(c["id"], "rebuild") == 6
run_all(c["id"])
got = items(c["id"])
assert got["Alpha"]["mesh_id"] != old["Alpha"] and got["Alpha2"]["mesh_id"] not in (alpha2, got["Alpha"]["mesh_id"]), (got["Alpha"], got["Alpha2"])
assert server.q("SELECT mesh_id FROM mesh_links WHERE climb_id=?", got["Alpha2"]["climb_id"])[0]["mesh_id"] == got["Alpha2"]["mesh_id"]
newest = got["Alpha"]["mesh_id"]

# going back to an older version
server.make_current(old["Alpha"])
assert server.q("SELECT replaced_by FROM meshes WHERE id=?", old["Alpha"], one=True)["replaced_by"] is None
assert server.q("SELECT replaced_by FROM meshes WHERE id=?", newest, one=True)["replaced_by"] == old["Alpha"]
assert server.q("SELECT mesh_id FROM projects WHERE id=?", got["Alpha"]["project_id"], one=True)["mesh_id"] == old["Alpha"]
assert {r["climb_id"] for r in server.q("SELECT climb_id FROM mesh_links WHERE mesh_id=?", old["Alpha"])} == {got["Alpha"]["climb_id"]}

# a rebuild that fails keeps the mesh it had
outcome["Delta"] = "boom"
delta_mesh = got["Delta"]["mesh_id"]
server.queue_redo(c["id"], "rebuild")
run_all(c["id"])
got = items(c["id"])
assert got["Delta"]["status"] == "done" and got["Delta"]["mesh_id"] == delta_mesh and "rebuilding failed" in got["Delta"]["note"], got["Delta"]
assert server.q("SELECT status,mesh_id FROM projects WHERE id=?", got["Delta"]["project_id"], one=True) == {"status": "done", "mesh_id": delta_mesh}
outcome.clear()

# looking for new videos only harvests again the climbs YouTube has new ones of
harvested.clear()
searched.update({"Alpha": ["https://www.youtube.com/watch?v=aaaaaaaaaaa"]})
server.youtube_mod.metadata = lambda url: {"url": url, "title": "t", "description": "", "license": "x"}
assert server.queue_redo(c["id"], "videos") == 6
run_all(c["id"])
got = items(c["id"])
assert harvested == ["Alpha"], harvested
assert got["Beta"]["note"] == "no new videos found" and got["Beta"]["status"] == "done", got["Beta"]
assert server.q("SELECT found_by FROM climb_videos WHERE url=?", "https://www.youtube.com/watch?v=aaaaaaaaaaa", one=True)["found_by"] == "search"

# looking for new videos and rebuilding each climb that gets new photos straight away
harvested.clear()
searched.update({"Beta": ["https://www.youtube.com/watch?v=bbbbbbbbbbb"]})
photo_sets["Beta"] = ["a.jpg", "b.jpg", "h.jpg"]
beta_mesh, alpha_mesh = items(c["id"])["Beta"]["mesh_id"], items(c["id"])["Alpha"]["mesh_id"]
assert server.queue_redo(c["id"], "videos+rebuild") == 6
run_all(c["id"])
got = items(c["id"])
assert harvested == ["Beta"], harvested
assert got["Beta"]["mesh_id"] != beta_mesh and got["Beta"]["changed"] == 0 and "harvested again" in got["Beta"]["note"] \
    and "rebuilt" in got["Beta"]["note"] and "Rebuild changed" not in got["Beta"]["note"], got["Beta"]
assert got["Alpha"]["mesh_id"] == alpha_mesh and got["Alpha"]["note"] == "no new videos found", got["Alpha"]

# a restart carries on with what's queued again: a rebuild that had started carries on from its work
server.queue_redo(c["id"], "rebuild")
gamma = items(c["id"])["Gamma"]
server.init_db()
assert server.q("SELECT status FROM projects WHERE id=?", gamma["project_id"], one=True)["status"] != "stopped", \
    "a project only queued again isn't marked as stopped"
server.set_item(c["id"], gamma["source_id"], status="running", redo_started=1)
(server.PROJECTS / gamma["project_id"] / "work").mkdir(exist_ok=True)
(server.PROJECTS / gamma["project_id"] / "work" / "kept.bin").write_bytes(b"x")
server.init_db()
seen_work = []
real = pipeline.reconstruct
pipeline.reconstruct = lambda images, work, *a, **k: (seen_work.append((pathlib.Path(work) / "kept.bin").exists()), real(images, work, *a, **k))[1]
run_all(c["id"])
pipeline.reconstruct = real
assert True in seen_work, "the interrupted rebuild carried on from its work"
assert items(c["id"])["Gamma"]["status"] == "done" and status(c["id"])[0] == "done"
print("harvesting and rebuilding again OK")

# --- more than one worker at once -----------------------------------------------------------------------
fresh()
c = server.create_campaign("", "popularity", CSV)
w1, w2, w3 = server.Worker("one"), server.Worker("two", remote=True), server.Worker("three")
server._workers.extend([w1, w2, w3])
with server._claim_lock:
    picks = [server._claim(w) for w in (w1, w2, w3)]
assert [p[1]["name"] for p in picks] == ["Alpha", "Beta", "Gamma"], "Alpha2 waits: its boulder is Alpha's, being harvested"
assert [w.doing for w in (w1, w2, w3)] == ["Alpha", "Beta", "Gamma"] and server.busy_campaigns() == {c["id"]}
assert {i["name"] for i in server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["running"]} == {"Alpha", "Beta", "Gamma"}
server.stop_campaign(c["id"], now=True)
assert all(w.control._stop.is_set() for w in (w1, w2, w3)) and not server._first.control._stop.is_set(), "stops every worker on it, and only those"
assert server.campaign_status(c["id"]) == "stopped"
server._workers[:] = [server._first]

# two running areas share the workers
fresh()
c1 = server.create_campaign("", "popularity", CSV)
c2 = server.create_campaign("", "rating", CSV.replace("Alpha", "Zeta").replace("route/1/", "route/101/"))
w1, w2 = server.Worker("one"), server.Worker("two")
server._workers.extend([w1, w2])
with server._claim_lock:
    a, b = server._claim(w1), server._claim(w2)
assert {a[0]["id"], b[0]["id"]} == {c1["id"], c2["id"]}, "one on each"
server._workers[:] = [server._first]

# a remote worker that can't get a machine: the climb waits, and so does the worker
fresh()
c = server.create_campaign("", "popularity", CSV)
real_harvest = server.run_harvest


def no_machine(*a, **k):
    raise server.compute.ComputeUnavailable("EC2 wouldn't start an instance: no capacity")


server.run_harvest = no_machine
w = server.Worker("remote 1", remote=True)
server._workers.append(w)
server._here.worker = w
assert server.next_campaign_step()
got = items(c["id"])["Alpha"]
assert got["status"] == "pending" and "waiting for a machine" in got["note"] and got["project_id"], got
assert w.wait_until > time.time() and "no capacity" in w.error and w.campaign is None
assert server.q("SELECT status FROM projects WHERE id=?", got["project_id"], one=True)["status"] == "stopped"
server._here.worker = None
server.run_harvest = real_harvest
server._workers[:] = [server._first]
server.stop_campaign(c["id"])

# the admin page's settings start and retire workers
server.settings().update({"compute.local_workers": 2})
server.configure_workers()
assert [x.name for x in server._workers if not x.retired] == ["this server", "this server 2"], [x.name for x in server._workers]
server.settings().update({"compute.local_workers": 1, "compute.provider": "ssh", "compute.ssh_hosts": ["me@10.0.0.9"], "compute.remote_workers": 2})
server.configure_workers()
state = server.compute_state()
assert [x["name"] for x in state["workers"]] == ["this server", "remote 1", "remote 2"] and state["pool"]["limit"] == 2, state
server.settings().update({"compute.provider": "none"})
server.configure_workers()
assert [x["name"] for x in server.compute_state()["workers"]] == ["this server"] and server.compute_state()["pool"] is None
# machines that connect here for work (pullwork.py): workers for them, and none to start; with none meshing here, a
# reconstruction asked for here goes to them too, and waits while none has asked for work
server.pullwork.POLL = 0.1
server.settings().update({"compute.provider": "pull", "compute.remote_workers": 1, "compute.local_workers": 0})
server.configure_workers()
state = server.compute_state()
assert [x["name"] for x in state["workers"]] == ["this server", "remote 1"] and state["pool"] is None, state
assert server._executor is server.MACHINES and not state["local_meshing"] and state["machines"]["machines"] == []
pid = server.new_id()
(server.PROJECTS / pid / "photos").mkdir(parents=True)
server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,?)", pid, "by hand", time.time(), "upload", "queued", write=True)
for provider, why in (("pull", "no machine has asked"), ("none", "meshes nothing itself")):
    server.settings().update({"compute.provider": provider})
    server.configure_workers()
    try:
        server.run_reconstruct(pid)
        raise AssertionError("should have waited for a machine")
    except server.compute.ComputeUnavailable as e:
        assert why in str(e), e
    assert server.q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "stopped"
server.settings().update({"compute.local_workers": 1})
server.configure_workers()
assert server._local_meshing and [x["name"] for x in server.compute_state()["workers"]] == ["this server"]
print("several workers OK")

# a route page's GPS far from its area's is a mistake on the page: the area's are kept
area = Climb(source="mountainproject", source_id="555", url="https://www.mountainproject.com/route/555/x", name="Far Off",
             lat=33.81573, lng=-116.64921, coord_source="area", raw={"stub": True})
fid = server.upsert_climb(area)
page = Climb(source="mountainproject", source_id="555", url=area.url, name="Far Off", lat=32.64058, lng=-116.99956, coord_source="route")
assert server.upsert_climb(page) == fid
row = server.climb_row(fid, full=True)
assert (row["lat"], row["lng"], row["coord_source"]) == (33.81573, -116.64921, "area"), row
assert row["data"]["raw"]["route_gps_ignored"] == [32.64058, -116.99956], row["data"]["raw"]
server.upsert_climb(Climb(source="mountainproject", source_id="555", url=area.url, name="Far Off", lat=33.8158, lng=-116.6493, coord_source="route"))
assert server.climb_row(fid)["coord_source"] == "route", "a route's own GPS near its area's is better"
assert fid not in server.coord_doubts(fresh=True)
server.upsert_climb(page)
assert "used here instead" in server.coord_doubts()[fid]["why"], "flagged, to be put right on its page"
on = lambda sid, lat, lng: server.upsert_climb(Climb(source="mountainproject", source_id=sid, url=f"https://x/{sid}", name=sid,  # noqa: E731
                                                     lat=lat, lng=lng, coord_source="route", boulder_name="Big Rock", area_path=["CA", "Big Rock"]))
ids = [on("b1", 34.0, -116.0), on("b2", 34.0001, -116.0001), on("b3", 34.03, -116.0)]
doubts = server.coord_doubts(fresh=True)
assert ids[2] in doubts and ids[0] not in doubts and ids[1] not in doubts, doubts
assert "3.3 km from where most of the climbs on Big Rock are" == doubts[ids[2]]["why"], doubts[ids[2]]
server.q("UPDATE climbs SET coord_source='manual' WHERE id=?", ids[2], write=True)
assert ids[2] not in server.coord_doubts(fresh=True), "coordinates set by hand are taken as right"
server.q("INSERT INTO campaign_items(campaign_id, source_id, rank, name, data, status) VALUES('zz', '777', 1, 'Old', ?, 'skipped')",
         json.dumps({"lat": 33.81573, "lng": -116.64921}), write=True)
old = server.upsert_climb(Climb(source="mountainproject", source_id="777", url="https://x/777", name="Old", lat=33.8158, lng=-116.6493, coord_source="route"))
server.q("UPDATE climbs SET lat=32.64058, lng=-116.99956 WHERE id=?", old, write=True)     # as saved before importing checked
assert server.settle_far_coordinates() == 1 and server.settle_far_coordinates() == 0, "once, then left alone"
row = server.climb_row(old, full=True)
assert (row["lat"], row["lng"], row["coord_source"]) == (33.81573, -116.64921, "area") and row["data"]["lat"] == 33.81573, row
assert "(32.64058, -116.99956)" in server.coord_doubts()[old]["why"], "still flagged, with what its page said"
print("route coordinates OK")

# --- finding beta: its own queue, asked-for first, waiting out YouTube's refusals, stopped all at once -----------
fresh()
for i, mid in enumerate(("aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3", "aaaaaaaaaaa4")):
    server.q("INSERT INTO meshes(id,name,filename,source,created) VALUES(?,?,?,?,?)", mid, f"m{i}", "mesh.glb", f"project:p{i}", i,
             write=True)
    (server.MESHES / mid).mkdir(parents=True, exist_ok=True)
videos, server.beta_mod.videos = server.beta_mod.videos, lambda d: {} if d.name == "aaaaaaaaaaa4" else {"u": {}}
server.beta_mod.save(server.MESHES / "aaaaaaaaaaa3", {"status": "done", "videos": []})
assert server.beta_findable() == ["aaaaaaaaaaa2", "aaaaaaaaaaa1"], "newest first; not one already done, nor one with no videos"
assert [server.queue_beta(m) for m in server.beta_findable()] == [True, True]
assert server.queue_beta("aaaaaaaaaaa3", ahead=True) and not server.queue_beta("aaaaaaaaaaa3"), "once"
assert server.beta_queue() == ["aaaaaaaaaaa3", "aaaaaaaaaaa2", "aaaaaaaaaaa1"], "the one asked for on its own goes first"
assert server.jobs_state()["beta"] == {"running": None, "queued": 3, "youtube": None}
refusal = {"since": 0, "until": time.time() + 3600, "level": 0, "error": "not a bot"}


def refused(mesh_dir, photos, log):
    server.beta_mod.save(mesh_dir, {"status": "queued", "videos": [], "waiting": refusal["until"], "resume": True})
    raise server.youtube_mod.Refused(refusal)
find, server.beta_mod.find = server.beta_mod.find, refused
server.run_beta("aaaaaaaaaaa3")
assert server.beta_mod.load(server.MESHES / "aaaaaaaaaaa3")["status"] == "queued", "waiting, not failed"
assert server.stop_beta() == 3 and server.beta_queue() == [], "stopping unqueues them all"
assert {server.beta_mod.load(server.MESHES / m)["status"] for m in ("aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3")} == {"stopped"}
server.beta_mod.videos, server.beta_mod.find = videos, find
print("beta queue OK")
print("ALL CAMPAIGN TESTS PASSED")
