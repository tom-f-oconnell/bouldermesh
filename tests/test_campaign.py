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


def fake_harvest(c, project_dir, log, sources, relevance_filter=True, tracker=None, pool_from=None, anchors_from=None, want=None,
                 want_search=None):
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
server.settings().update({"areas.meshes": "climbs"})    # each climb meshed on its own, but in "a boulder's climbs meshed together"


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
# a climb that only shares a mesh is shown as via the climb it was made for, not whichever climb an area linked it
# through, which may only have shared it too
alpha, alpha2 = got["Alpha"]["climb_id"], got["Alpha2"]["climb_id"]
server.link_mesh(again["mesh_id"], alpha, server.SHARED + "Gamma")
assert server.climb_row(alpha)["shared_of"] == "Gamma", "with no climb of its own, the note's"
server.link_mesh(again["mesh_id"], alpha2, "auto-linked from reconstruction project")
assert server.climb_row(alpha)["shared_of"] == "Alpha2", server.climb_row(alpha)
assert [x["made_for"] for x in server.climb_row(alpha, full=True)["boulder"]["shared"]] == ["Alpha2"]
server.q("DELETE FROM mesh_links WHERE mesh_id=?", again["mesh_id"], write=True)
print("boulder meshes OK")

# --- a boulder's climbs harvested, and meshed together as their boulder (areas.meshes "boulders", the default) ---------
fresh()
pools.clear()
server.q("DELETE FROM projects", write=True)
server.settings().update({"areas.meshes": "boulders"})
reconstructed = []
real_rec = pipeline.reconstruct


def counting_rec(images, *a, **k):
    reconstructed.append(server.q("SELECT name FROM projects WHERE id=?", pathlib.Path(images).parent.name, one=True)["name"])
    return real_rec(images, *a, **k)


pipeline.reconstruct = counting_rec
c = server.create_campaign("", "popularity", CSV)
run_all(c["id"], limit=30)
got = items(c["id"])
proj = server.q("SELECT * FROM projects WHERE kind='boulder'", one=True)
assert proj and proj["name"] == "Alpha Boulder (boulder)" and proj["status"] == "done", proj
assert sorted(reconstructed) == ["Alpha Boulder (boulder)", "Beta (harvest)", "Delta (harvest)", "Delta2 (harvest)", "Gamma (harvest)"], \
    reconstructed     # Alpha and Alpha2 only harvested; Delta's area holds several boulders, so its climbs are meshed alone
assert got["Alpha"]["status"] == got["Alpha2"]["status"] == "done" and got["Alpha"]["mesh_id"] == got["Alpha2"]["mesh_id"] == proj["mesh_id"], \
    (got["Alpha"], got["Alpha2"])
assert "meshed with its boulder" in got["Alpha"]["note"] and "meshed with its boulder" in got["Alpha2"]["note"], got["Alpha"]["note"]
assert set(pools["Alpha Boulder"]) == {got["Alpha"]["project_id"], got["Alpha2"]["project_id"]}, pools
assert server.q("SELECT status FROM projects WHERE id=?", got["Alpha"]["project_id"], one=True)["status"] == "harvested", "no mesh of its own"
assert got["Beta"]["mesh_id"] and got["Beta"]["mesh_id"] != proj["mesh_id"] and server.campaign_status(c["id"]) == "done"
assert server.climb_row(got["Alpha2"]["climb_id"])["n_boulder"] == 1
# volunteers fetching more for one of them later: harvested again with new photos, and its boulder meshed again, as a
# new version, not the climb on its own


def with_a_frame(c_, project_dir, *a, **k):
    (project_dir / "photos" / "yt_00000000_000.jpg").write_bytes(b"frame")
    return fake_harvest(c_, project_dir, *a, **k)


server.harvest_mod.harvest = with_a_frame
server.reharvest_with_videos(got["Alpha2"]["project_id"])
assert items(c["id"])["Alpha2"]["redo"] == "harvest+rebuild" and server.campaign_status(c["id"]) == "running"
reconstructed.clear()
run_all(c["id"], limit=10)
server.harvest_mod.harvest = fake_harvest
got = items(c["id"])
again = server.q("SELECT * FROM projects WHERE kind='boulder'", one=True)
assert reconstructed == ["Alpha Boulder (boulder)"] and again["mesh_id"] != proj["mesh_id"], reconstructed
assert got["Alpha"]["mesh_id"] == got["Alpha2"]["mesh_id"] == again["mesh_id"] and got["Alpha2"]["status"] == "done", got["Alpha2"]
# a boulder whose pooled photos aren't enough: its climbs are skipped with it
fresh()
server.q("DELETE FROM projects", write=True)
outcome["pooled"] = "poor"
c = server.create_campaign("", "popularity", HEAD + "".join(line for line in CSV.splitlines(True)[1:] if line.split(",")[0] in ("Alpha", "Alpha2")))
run_all(c["id"], limit=10)
got = items(c["id"])
assert got["Alpha"]["status"] == got["Alpha2"]["status"] == "skipped" and not got["Alpha"]["mesh_id"], (got["Alpha"], got["Alpha2"])
assert "not enough different viewpoints" in got["Alpha2"]["note"] and server.campaign_status(c["id"]) == "done"
pipeline.reconstruct = real_rec
outcome.clear()
server.settings().update({"areas.meshes": "climbs"})
print("a boulder's climbs meshed together OK")

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


def stop_during(pid, sources, reconstruct, relevance_filter=True, resume=False, wait=False, need_youtube=False):
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


def harvest_with_photos(c, project_dir, log, sources, relevance_filter=True, tracker=None, pool_from=None, anchors_from=None, want=None,
                        want_search=None):
    harvested.append(c.name)
    for n in photo_sets.get(c.name, ["a.jpg", "b.jpg", "c.jpg"]):
        (project_dir / "photos" / n).write_bytes(b"x")
    (project_dir / "sources.json").write_text(json.dumps({"climb": {}, "files": [    # frames of the videos it found, as a real one records
        {"file": f"yt_{i:08x}_000.jpg", "source": "youtube", "url": u} for i, u in enumerate(searched.get(c.name, []))]}))
    return {"images": 3, "video_frames": 1, "queries": [], "diversity": {"verdict": "poor" if outcome.get(c.name) == "poor" else "good"},
            "videos": [{"url": f"https://www.youtube.com/watch?v={c.name[:6]:x<11}", "found_by": "search", "title": c.name,
                        "description": "I'm 5'10\" with a +2 ape index", "frames": 1, "license": "YouTube standard licence (all rights reserved)"}]}


server.harvest_mod.harvest = harvest_with_photos
server.youtube_mod.search = lambda c, log, known=frozenset(), **kw: [Media(url=u, kind="video", title=c.name, source="youtube-search")
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

# --- a climb waiting for volunteers' YouTube searches and videos is meshed once, when they've come ---------
fresh()
coming: dict[str, int] = {}     # project -> what volunteers still have to fetch for it
plan: list[int] = []            # what each harvest leaves them to fetch
real_pending = server.VOLUNTEERS.pending
server.VOLUNTEERS.pending = lambda purpose, ref: coming.get(ref, 0) if purpose == "harvest" else 0
meshed: list[str] = []


def asking(c_, project_dir, *a, **k):
    coming[project_dir.name] = plan.pop(0) if plan else 0
    return fake_harvest(c_, project_dir, *a, **k)


def counting(images, *a, **k):
    meshed.append(pathlib.Path(images).parent.name)
    return fake_reconstruct(images, *a, **k)


server.harvest_mod.harvest, pipeline.reconstruct = asking, counting
plan[:] = [1, 2, 0]     # a search, then the two videos it found, then nothing
only = lambda name: HEAD + "".join(line for line in CSV.splitlines(True)[1:] if line.startswith(name + ","))  # noqa: E731
c = server.create_campaign("", "popularity", only("Alpha"))
run_all(c["id"])
it = items(c["id"])["Alpha"]
pid = it["project_id"]
assert it["status"] == "waiting" and it["waiting_since"] and "1 YouTube search" in it["note"] and not meshed, it
assert server.q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "waiting"
assert server.campaign_status(c["id"]) == "running", "an area isn't done while its climbs wait"
assert "1 climb(s) waiting for volunteers" in server.q("SELECT note FROM campaigns WHERE id=?", c["id"], one=True)["note"]
assert server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["counts"] == {"waiting": 1}
coming[pid] = 0
server.volunteer_ready("harvest", pid)      # the search's results came
it = items(c["id"])["Alpha"]
assert it["status"] == "pending" and it["redo"] == "harvest+rebuild" and it["retry"] == 1, it
run_all(c["id"])
it = items(c["id"])["Alpha"]
assert it["status"] == "waiting" and "2 YouTube" in it["note"] and not meshed and harvested.count("Alpha") == 2, it
coming[pid] = 0
server.volunteer_ready("harvest", pid)      # and then its videos
run_all(c["id"])
it = items(c["id"])["Alpha"]
assert it["status"] == "done" and it["mesh_id"] and meshed == [pid] and harvested.count("Alpha") == 3, (it, meshed)
assert server.campaign_status(c["id"]) == "done" and not server.q("SELECT note FROM campaigns WHERE id=?", c["id"], one=True)["note"]
# one that has waited long enough is meshed with what's come, and doesn't wait again
fresh()
meshed.clear()
plan[:] = [1, 1]
c = server.create_campaign("", "popularity", only("Beta"))
run_all(c["id"])
assert items(c["id"])["Beta"]["status"] == "waiting"
server.q("UPDATE campaign_items SET waiting_since=?", time.time() - server.VOLUNTEER_WAIT - 60, write=True)
run_all(c["id"])
it = items(c["id"])["Beta"]
assert it["status"] == "done" and len(meshed) == 1 and harvested.count("Beta") == 2, (it, meshed)
# what came while it was still being harvested, too late for that harvest: given again as it starts waiting, so it's
# harvested again then, not a day later
fresh()
meshed.clear()
calls: list[str] = []
resettled: list[str] = []


def came_during(c_, project_dir, *a, **k):
    calls.append(project_dir.name)
    out = asking(c_, project_dir, *a, **k)
    if len(calls) == 1:     # its search came back from a volunteer while it was still being harvested
        assert server.volunteer_ready("harvest", project_dir.name) is False, "being harvested: not now"
        out.update(youtube_checked=False, unsearched=f"{c_.name} V1 Joshua Tree")
    return out


real_resettle = server.VOLUNTEERS.resettle
server.VOLUNTEERS.resettle = lambda purpose, ref: (resettled.append(ref), not coming.get(ref) and server.volunteer_ready(purpose, ref))[1]
server.harvest_mod.harvest = came_during
try:
    c = server.create_campaign("", "popularity", only("Beta"))
    run_all(c["id"])
    it = items(c["id"])["Beta"]
    assert it["status"] == "done" and len(meshed) == 1 and len(calls) == 2 and resettled == [it["project_id"]], (it, meshed, calls)
finally:
    server.VOLUNTEERS.resettle = real_resettle
server.VOLUNTEERS.pending, server.harvest_mod.harvest, pipeline.reconstruct = real_pending, fake_harvest, fake_reconstruct
print("waiting for volunteers OK")

# --- a climb isn't meshed until YouTube has been searched for it, unless its area says to go without ---------------------
fresh()
meshed.clear()
searched_yet = {"Alpha": False}


def maybe_searched(c_, project_dir, *a, **k):
    out = fake_harvest(c_, project_dir, *a, **k)
    if not searched_yet.get(c_.name, True):
        out.update(youtube_checked=False, unsearched=f"{c_.name} V1 Joshua Tree")
    return out


server.harvest_mod.harvest, pipeline.reconstruct = maybe_searched, counting
c = server.create_campaign("", "popularity", only("Alpha"))
assert server.q("SELECT without_youtube FROM campaigns WHERE id=?", c["id"], one=True)["without_youtube"] == 0, "off to start"
run_all(c["id"])
it = items(c["id"])["Alpha"]
assert it["status"] == "waiting" and "waiting for its YouTube search" in it["note"] and not meshed, it
assert server.campaign_status(c["id"]) == "running"
server.q("UPDATE campaign_items SET waiting_since=?", time.time() - server.VOLUNTEER_WAIT - 60, write=True)
run_all(c["id"])
it = items(c["id"])["Alpha"]
assert it["status"] == "waiting" and not meshed and it["waiting_since"] > time.time() - 60, "tried again, still waiting, a day more"
assert server.want_skipped()["harvest"] == 1, "the admin page's button asks for the search that was never asked"
server.VOLUNTEERS.cancel()
searched_yet["Alpha"] = True        # a volunteer's search came
server.volunteer_ready("harvest", it["project_id"])
run_all(c["id"])
it = items(c["id"])["Alpha"]
assert it["status"] == "done" and meshed == [it["project_id"]], (it, meshed)
# an area told to mesh without searching: a climb waiting only for that goes ahead
fresh()
meshed.clear()
searched_yet["Beta"] = False
c = server.create_campaign("", "popularity", only("Beta"))
run_all(c["id"])
assert items(c["id"])["Beta"]["status"] == "waiting" and not meshed
camp = server.set_campaign_options(c["id"], {"without_youtube": True})
assert camp["without_youtube"] == 1 and server.campaign_view(camp)["without_youtube"] == 1
run_all(c["id"])
it = items(c["id"])["Beta"]
assert it["status"] == "done" and len(meshed) == 1, (it, meshed)
c2 = server.create_campaign("", "popularity", only("Gamma"), without_youtube=True)
searched_yet["Gamma"] = False
run_all(c2["id"])
assert items(c2["id"])["Gamma"]["status"] == "done" and len(meshed) == 2, "started that way"
server.harvest_mod.harvest, pipeline.reconstruct = fake_harvest, fake_reconstruct
print("searching YouTube before meshing OK")

# --- over the cap on the data's size, areas take no new climb and volunteers get nothing, until there's room ----------
fresh()
c = server.create_campaign("", "popularity", only("Alpha"))
old = server.PROJECTS / "deadbeef0001"
(old / "rejected").mkdir(parents=True)
(old / "rejected" / "x.jpg").write_bytes(b"x" * 300_000)
server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,?)", old.name, "old", time.time(), "harvest", "done", write=True)
assert not server.full(), "no cap, nothing to be full"
server.settings().update({"storage.cap_gb": (server.measure_usage()["total"] - 1000) / 2**30})
assert "out of room" in server.full() and server._usage["by"]["rejected photos"] == 300_000
w = server.Worker("capped")
with server._claim_lock:
    assert server._claim(w) is None
assert server.q("SELECT note FROM campaigns WHERE id=?", c["id"], one=True)["note"].startswith("paused: the server is out of room")
assert server.campaign_status(c["id"]) == "running", "paused, not stopped"
erin = server.VOLUNTEERS.add("Erin")
v = server.VOLUNTEERS.volunteer(erin["token"])
server.VOLUNTEERS.want("video", "ggggggggggg", "harvest", "x")
got = server.VOLUNTEERS.work(v, "203.0.113.30")
assert "out of room" in got["why"] and got["wait"] == server.volunteers_mod.PAUSE_WAIT, got
assert "1 project(s)" in server.admin_clear_rejected({})["result"] and not (old / "rejected").exists()
assert not server.full(), "measured again after"
with server._claim_lock:
    assert server._claim(w)[1]["name"] == "Alpha"
assert not server.q("SELECT note FROM campaigns WHERE id=?", c["id"], one=True)["note"]
assert "task" in server.VOLUNTEERS.work(v, "203.0.113.30")
# your phone is told when the room left is under storage.warn_percent of the cap (or storage.warn_gb), and at the cap; once
told, real_send = [], server.NOTIFIER.send
server.NOTIFIER.send = lambda title, message, **kw: told.append((title, message)) or True
try:
    server.settings().update({"storage.cap_gb": 10.0, "storage.warn_percent": 1.0})
    assert server.warn_room() == 0.1 * 2**30
    server._usage.update(total=9.95 * 2**30, per_climb=20 * 2**20, per_mesh=30 * 2**20)
    server.check_room()
    server.check_room()
    assert len(told) == 1 and told[0][0] == "BoulderMesh is nearly out of room" and "about 1 climb's worth" in told[0][1], told
    assert server.room_left()["warn_climbs"] == 2.048
    server._usage.update(total=11 * 2**30)
    server.check_room()
    assert len(told) == 1, "told once, until there's room again"
    server._usage.update(total=9.8 * 2**30)    # more than half again as much as the warning's room
    server.check_room()
    server._usage.update(total=11 * 2**30)
    server.check_room()
    assert told[-1][0] == "BoulderMesh is out of room" and len(told) == 2
    server.settings().update({"storage.warn_gb": 2.5})
    assert server.warn_room() == 2.5 * 2**30, "the GB set overrides the %"
    assert server.settings().get("storage.warn_percent") == 1.0
    # and when a worker or downloader joins or leaves (presence.py)
    server.came_went("machine")({"id": 1, "name": "hal", "client": "script 5"}, "here", "")
    server.came_went("volunteer")({"id": 2, "name": "erin"}, "gone", "not heard from since 09:41")
    for _ in range(50):
        if len(told) == 4:
            break
        time.sleep(0.05)
    assert sorted(told[2:]) == [("Downloader erin left", "Not heard from since 09:41."),
                                ("Worker hal joined", "It's lending its computer to mesh (script 5).")], told
finally:
    server.NOTIFIER.send = real_send
    server.settings().update({"storage.cap_gb": 0, "storage.warn_gb": 0, "storage.warn_percent": 0.5})
server.VOLUNTEERS.revoke(erin["id"])
server.VOLUNTEERS.cancel()
server.stop_campaign(c["id"])
fresh()
print("the cap on the data OK")

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
real_run_item = server.run_campaign_item
w = server.Worker("remote 1", remote=True)
server._workers.append(w)
server._here.worker = w
assert server.next_campaign_step()
got = items(c["id"])["Alpha"]
assert got["status"] == "pending" and "waiting for a machine" in got["note"] and got["project_id"], got
assert w.wait_until > time.time() and "no capacity" in w.error and w.campaign is None
assert server.q("SELECT status FROM projects WHERE id=?", got["project_id"], one=True)["status"] == "stopped"
# its card says what it waits for, the kind of machine, and when it tries again
server.settings().update({"compute.provider": "aws"})
mw = server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["machine_wait"]
assert mw["climb"] == "Alpha" and not mw["boulder"] and mw["kind"] == "an AWS instance" and "no capacity" in mw["why"], mw
assert mw["next"] == w.wait_until and mw["since"] <= time.time() and not mw["pull"], mw
server.settings().update({"compute.provider": "pull"})
mw = server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["machine_wait"]
assert mw["kind"] == "a machine that connects here for work" and mw["pull"] and mw["last_seen"] is None and not mw["connected"], mw
server.settings().update({"compute.provider": "none"})
assert server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["machine_wait"]["kind"] is None
server._here.worker = None
server.run_harvest = real_harvest
server._here.worker = w      # a climb meshed after it: the wait is over
server.run_campaign_item = lambda camp, item: None
w.wait_until = 0
server.set_item(c["id"], server.q("SELECT source_id FROM campaign_items WHERE campaign_id=? AND name='Alpha'", c["id"], one=True)["source_id"],
                status="pending")
assert server.next_campaign_step() and w.waiting is None
assert server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["machine_wait"] is None
server.run_campaign_item = real_run_item
server._here.worker = None
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
beta_videos, server.beta_mod.videos = server.beta_mod.videos, lambda d: {} if d.name == "aaaaaaaaaaa4" else {"u": {}}
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
server.beta_mod.videos, server.beta_mod.find = beta_videos, find
print("beta queue OK")

# --- finding beta and size on machines that connect here, when this server meshes nothing itself ------------------------
import shutil  # noqa: E402
import tempfile  # noqa: E402

mid, pid = "beefbeefbeef", "cafecafecafe"
md = server.MESHES / mid
md.mkdir(parents=True)
(md / "mesh.glb").write_bytes(b"glb")
(md / "texture.png").write_bytes(b"png")
(md / "cameras.json").write_text(json.dumps([{"name": "yt_aaaaaaaa_000.jpg"}, {"name": "p.jpg"}]))
for name in ("yt_aaaaaaaa_000.jpg", "p.jpg"):
    (server.PROJECTS / pid / "photos").mkdir(parents=True, exist_ok=True)
    (server.PROJECTS / pid / "photos" / name).write_bytes(b"jpg")
(server.PROJECTS / pid / "masks").mkdir()
(server.PROJECTS / pid / "masks" / "p.jpg.png").write_bytes(b"mask")
server.q("INSERT INTO meshes(id,name,filename,source,created) VALUES(?,?,?,?,?)", mid, "Gamma", "mesh.glb", f"project:{pid}", time.time(),
         write=True)
url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
sent = []


def fake_task(kind, files, args, label, log):
    sent.append((kind, files, args))
    back = pathlib.Path(tempfile.mkdtemp())
    (back / "out").mkdir()
    (back / "out" / ("beta.json" if kind == "beta" else "scale.json")).write_text(json.dumps({"status": "done", "videos": []}))
    (back / "out" / "page.html").write_text("<script>")
    return {"mesh": "", "files": []}, back


saved = (server.MACHINES.task, server.MACHINES.connected, server.beta_mod.videos, server._local_meshing, server._executor, server.queue_job)
server.MACHINES.task = fake_task
server.beta_mod.videos = lambda d: {url: {"tag": "aaaaaaaa", "title": "t", "uploader": "u", "frames": {"yt_aaaaaaaa_000.jpg": {}}}}
server._local_meshing, server._executor = False, server.MACHINES
assert server.offloading()
server.queue_beta(mid)
server.run_beta(mid)
rec = server.beta_mod.load(md)
assert rec["status"] == "queued" and rec["waiting_videos"] == [url] and not sent, "its video isn't here: it waits for it"
junk = pathlib.Path(tempfile.mkdtemp()) / "v.mp4"
junk.write_bytes(b"not really a video")
server.youtube_mod.cache_put(url, junk)
server.run_beta(mid)
kind, files, args = sent[-1]
assert kind == "beta" and {"mesh/mesh.glb", "mesh/cameras.json", "photos/yt_aaaaaaaa_000.jpg", "videos/aaaaaaaaaaa.mp4"} <= set(files), files
assert "mesh/texture.png" not in files and "photos/p.jpg" not in files, "only the mesh, and the video frames placed in it"
assert server.beta_mod.load(md)["status"] == "done" and not (md / "page.html").exists(), "only what the job may make comes back"
server.run_scale(mid)
kind, files, args = sent[-1]
assert kind == "scale" and "masks/p.jpg.png" in files and set(args) == {"climbs", "videos", "known"} and server.scale_mod.load(md)["status"] == "done"


def no_machine(*a, **k):
    raise server.compute.ComputeUnavailable("no machine has asked this server for work in the last 2 minutes")


server.MACHINES.task = no_machine
server.run_scale(mid)
assert server.scale_mod.load(md)["status"] == server.MACHINE_WAIT
queued = []
server.queue_job = queued.append
server.MACHINES.connected = lambda: True
server.requeue_waiting_scale()
assert server.scale_mod.load(md)["status"] == "queued" and queued == [{"type": "scale", "mid": mid}], "again once one's there"
server.run_beta(mid)
assert server.beta_mod.load(md)["machine_wait"] > time.time() and not server.beta_ready(mid), "beta waits a while"
(server.MACHINES.task, server.MACHINES.connected, server.beta_mod.videos, server._local_meshing, server._executor, server.queue_job) = saved
shutil.rmtree(md)
print("beta and size on machines OK")

# --- the first N meshes of an area: only those; entering it again, or raising N, goes on without doing again what's done ---
fresh()
server.settings().update({"areas.meshes": "climbs"})
real_export = server.mp_mod.export_area_csv
exports: list[bool] = []    # each export asked for: whether fresh, rather than from the cache
export_csv = CSV
server.mp_mod.export_area_csv = lambda aid, sort="popularity", fresh=False: exports.append(fresh) or server.mp_mod.parse_area_csv(export_csv)
url = "https://www.mountainproject.com/area/105720495/joshua-tree-national-park"
view = lambda cid: server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", cid, one=True))  # noqa: E731
c = server.create_campaign(url, "rating", max_meshes=1)      # by stars: Beta, Alpha, Alpha2, Gamma, Delta, Delta2
v = view(c["id"])
assert c["max_meshes"] == 1 and v["counts"]["pending"] == 1 and v["counts"]["beyond"] == 5 and [i["name"] for i in v["next"]] == ["Beta"], v
run_all(c["id"])
assert harvested == ["Beta"] and server.campaign_status(c["id"]) == "done" and view(c["id"])["made"] == 1, harvested
c2 = server.create_campaign(url, "rating", max_meshes=2)
assert c2["id"] == c["id"] and c2.get("again") and c2["status"] == "running" and len(server.q("SELECT id FROM campaigns")) == 1
run_all(c["id"])
assert harvested == ["Beta", "Alpha"], harvested     # the next by stars, and Beta not again
# failures not counted (the default): one that fails brings in the next
outcome["Alpha2"] = "boom"
server.set_campaign_options(c["id"], {"max_meshes": 3})     # from its card: a done area given more carries on
assert server.campaign_status(c["id"]) == "running"
run_all(c["id"])
got = items(c["id"])
assert harvested == ["Beta", "Alpha", "Alpha2", "Gamma"] and got["Alpha2"]["status"] == "failed" and got["Gamma"]["status"] == "done", harvested
v = view(c["id"])
assert v["made"] == 3 and v["counts"]["beyond"] == 2 and server.campaign_status(c["id"]) == "done", v["counts"]
# counted: Alpha2 takes Gamma's place, which, done, stays done; nothing more is meshed
server.set_campaign_options(c["id"], {"count_failures": True})
v = view(c["id"])
assert v["made"] == 2 and not v["next"] and server.campaign_status(c["id"]) == "done", v
for bad in (0, -2, "two", 1.5, True):
    try:
        server.set_campaign_options(c["id"], {"max_meshes": bad})
        raise AssertionError(bad)
    except ValueError as e:
        assert "whole number above 0" in str(e), e
server.set_campaign_options(c["id"], {"max_meshes": ""})
assert server.q("SELECT max_meshes FROM campaigns WHERE id=?", c["id"], one=True)["max_meshes"] is None and "beyond" not in view(c["id"])["counts"]
assert server.campaign_status(c["id"]) == "running", "no limit: on to the rest"
server.stop_campaign(c["id"])
fresh()
outcome["Beta"] = "boom"
c = server.create_campaign(url, "rating", max_meshes=1, count_failures=True)
run_all(c["id"])
assert harvested == ["Beta"] and server.campaign_status(c["id"]) == "done" and view(c["id"])["made"] == 0, harvested
server.set_campaign_options(c["id"], {"count_failures": False})
run_all(c["id"])
assert harvested == ["Beta", "Alpha"] and view(c["id"])["made"] == 1, harvested
# with boulders meshed from all their climbs' photos: a boulder's climbs are all harvested, wherever they come in the
# order, and its one mesh counts once
fresh()
server.settings().update({"areas.meshes": "boulders"})
c = server.create_campaign(url, "popularity", max_meshes=1)  # Alpha, Beta, Gamma, Alpha2 (on Alpha's boulder), Delta, Delta2
assert [i["name"] for i in view(c["id"])["next"]] == ["Alpha", "Alpha2"]
run_all(c["id"], 30)
got = items(c["id"])
proj = server.q("SELECT * FROM projects WHERE kind='boulder'", one=True)
assert harvested[:2] == ["Alpha", "Alpha2"] and "Beta" not in harvested, harvested
assert proj and proj["status"] == "done" and got["Alpha"]["mesh_id"] == got["Alpha2"]["mesh_id"] == proj["mesh_id"], (proj, got["Alpha"])
assert got["Beta"]["status"] == "pending" and view(c["id"])["made"] == 1 and server.campaign_status(c["id"]) == "done"
server.set_campaign_options(c["id"], {"max_meshes": 2})
run_all(c["id"], 30)
got = items(c["id"])
assert harvested.count("Alpha") == 1 and harvested[-1] == "Beta" and got["Beta"]["status"] == "done" and got["Gamma"]["status"] == "pending", harvested
assert len(server.q("SELECT id FROM projects WHERE kind='boulder'")) == 1, "the boulder not meshed again"
print("the first N meshes OK")

# --- areas whose lists overlap (a subarea, before or after the area it's in): what one has meshed, the other doesn't again,
# boulders' meshes too; and an area entered again asks Mountain Project for climbs added since only when asked to


def sub(names: set[str]) -> str:
    return HEAD + "".join(line for line in CSV.splitlines(True)[1:] if line.split(",")[0] in names)


for first, then in ((CSV, sub({"Alpha", "Alpha2", "Beta", "Gamma"})), (sub({"Alpha", "Alpha2", "Beta", "Gamma"}), CSV)):
    fresh()
    outcome["Gamma"] = "poor"
    a = server.create_campaign("", "popularity", first)
    run_all(a["id"], 40)
    meshes, before = server.q("SELECT COUNT(*) n FROM meshes", one=True)["n"], len(harvested)
    b = server.create_campaign("", "popularity", then)
    run_all(b["id"], 40)
    got = items(b["id"])
    assert harvested[before:] == ["Gamma"] + (["Delta", "Delta2"] if then == CSV else []), harvested[before:]   # only Gamma, skipped, tried again
    assert got["Alpha"]["note"] == got["Alpha2"]["note"] == "already meshed with its boulder" and got["Beta"]["note"] == "already had a mesh", got
    boulder_mesh = server.q("SELECT id FROM meshes WHERE boulder IS NOT NULL", one=True)["id"]
    assert got["Alpha"]["mesh_id"] == got["Alpha2"]["mesh_id"] == boulder_mesh and got["Gamma"]["status"] == "skipped"
    assert server.q("SELECT COUNT(*) n FROM meshes", one=True)["n"] - meshes == (2 if then == CSV else 0), "no boulder meshed again"
# a boulder mesh that didn't pool a climb's photos (as one harvested since) isn't its
assert not server.boulder_mesh_with("nonesuch", ["California", "Joshua Tree National Park", "* JT Bouldering", "Sector", "Alpha Boulder"])
server.stop_campaign(b["id"])

fresh()
export_csv = sub({"Alpha", "Beta", "Gamma", "Alpha2", "Delta"})
exports.clear()
c = server.create_campaign(url, "popularity", max_meshes=1)
assert exports == [False], "listed from the cache, if it has it"
run_all(c["id"])
assert server.create_campaign(url, "popularity", max_meshes=1)["added"] == 0 and exports == [False], "taken up again: not asked again"
export_csv = CSV + 'Epsilon,"Epsilon Boulder > Sector > * JT Bouldering > Joshua Tree National Park > California",' \
                   'https://www.mountainproject.com/route/7/epsilon,4.0,-1,Boulder,V7,,,34.0,-116.0\n'
again = server.create_campaign(url, "popularity", max_meshes=1, check_new=True)
got = items(c["id"])
assert exports == [False, True] and again["added"] == 2 and len(got) == 7, (exports, again["added"])
assert got["Delta2"]["rank"] == 6 and got["Epsilon"]["rank"] == 7 and got["Alpha"]["rank"] == 1, "after those listed, in its order"
assert server.create_campaign(url, "popularity", check_new=True)["added"] == 0, "each once"
server.stop_campaign(c["id"])
server.mp_mod.export_area_csv = real_export
server.settings().update({"areas.meshes": "climbs"})
print("overlapping areas OK")

# --- a boulder isn't meshed again from the same photos (same_photos_once); a rebuild that places fewer of the same photos
# leaves the mesh before current (keep_better); and climbs harvested for a boulder are done with it whoever carried it
fresh()
server.settings().update({"areas.meshes": "boulders"})
c = server.create_campaign("", "popularity", sub({"Alpha", "Alpha2"}))
assert c["same_photos_once"] == 1 and c["keep_better"] == 1, "both on unless turned off"
run_all(c["id"], 40)
got = items(c["id"])
first = server.q("SELECT id FROM meshes WHERE boulder IS NOT NULL AND replaced_by IS NULL", one=True)["id"]
assert got["Alpha"]["mesh_id"] == got["Alpha2"]["mesh_id"] == first, got
boulder, alpha2 = got["Alpha"]["boulder"], got["Alpha2"]["source_id"]
meshes = server.q("SELECT COUNT(*) n FROM meshes", one=True)["n"]
server.set_item(c["id"], alpha2, status="harvested", mesh_id=None)     # as when it resumes after an interruption
assert not server.settle_boulder(c["id"], boulder), "not queued again"
got = items(c["id"])
assert got["Alpha2"]["status"] == "done" and got["Alpha2"]["mesh_id"] == first and got["Alpha2"]["note"] == server.SAME_PHOTOS_NOTE
os.utime(server.PROJECTS / got["Alpha2"]["project_id"] / "photos")    # harvested again since: new photos, meshed again
server.set_item(c["id"], alpha2, status="harvested")
assert server.boulder_unchanged(server.boulder_of(server.climb_row(got["Alpha2"]["climb_id"])["area_path"])) is None
assert server.settle_boulder(c["id"], boulder), "queued, from its new photos"
server.set_item(c["id"], alpha2, status="harvested", redo=None, retry=0, note=None)
server.set_campaign_options(c["id"], {"same_photos_once": False})
assert server.q("SELECT same_photos_once FROM campaigns WHERE id=?", c["id"], one=True)["same_photos_once"] == 0
# off: queued even from the same photos; and Alpha, done before, carrying it leaves Alpha2 done with the new mesh
server.set_item(c["id"], got["Alpha"]["source_id"], status="pending", retry=1, redo="boulder", was="done", redo_started=0)
server.q("UPDATE campaigns SET status='running', only_retries=1 WHERE id=?", c["id"], write=True)     # as queue_boulders() does
run_all(c["id"], 5)
got = items(c["id"])
second = server.q("SELECT id FROM meshes WHERE boulder IS NOT NULL AND replaced_by IS NULL", one=True)["id"]
assert second != first and server.q("SELECT COUNT(*) n FROM meshes", one=True)["n"] == meshes + 1
assert got["Alpha"]["status"] == "done" and got["Alpha2"]["status"] == "done" and got["Alpha2"]["mesh_id"] == second, got
server.stop_campaign(c["id"])
server.settings().update({"areas.meshes": "climbs"})

kept = "k" * 12
server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,?)", kept, "Kept (harvest)", time.time(), "harvest", "harvested",
         write=True)
(server.PROJECTS / kept / "photos").mkdir(parents=True)
for n in "abcde":
    (server.PROJECTS / kept / "photos" / f"{n}.jpg").write_bytes(b"j")
placing: list[str] = []


def placing_rec(images, work, out_dir, log, use_gpu=False, internet=False, tracker=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mesh.ply").write_text("ply")
    (out_dir / "cameras.json").write_text(json.dumps([{"name": f"{n}.jpg"} for n in placing]))
    return out_dir / "mesh.ply", False


pipeline.reconstruct = placing_rec
current = lambda: server.q("SELECT mesh_id FROM projects WHERE id=?", kept, one=True)["mesh_id"]  # noqa: E731
placing[:] = "abcd"
m1 = server.run_reconstruct(kept)
placing[:] = "ab"
m2 = server.run_reconstruct(kept, keep_better=True)
assert current() == m1 and server.q("SELECT replaced_by FROM meshes WHERE id=?", m2, one=True)["replaced_by"] == m1, "the one before stays"
assert "fewer than the 4 mesh" in server.read_log(kept) and not server.q("SELECT replaced_by FROM meshes WHERE id=?", m1, one=True)["replaced_by"]
(server.PROJECTS / kept / "photos" / "d.jpg").unlink()      # not the same photos: one the mesh before placed is gone
m3 = server.run_reconstruct(kept, keep_better=True)
assert current() == m3 and server.q("SELECT replaced_by FROM meshes WHERE id=?", m1, one=True)["replaced_by"] == m3
placing[:] = "a"
m4 = server.run_reconstruct(kept)
assert current() == m4, "without keep_better, the newest is current"
pipeline.reconstruct = fake_reconstruct
print("the same photos once, and keeping the better mesh OK")

# --- an area's settings for COLMAP's mapper are what its meshes are made with, here or sent elsewhere ---------------------
import meshopts  # noqa: E402

fresh()
c = server.create_campaign("", "popularity", sub({"Beta"}), mapper_below=60, mapper_more=2)
assert (c["mapper_below"], c["mapper_more"]) == (60, 2)
seen_mapper = []
pipeline.reconstruct = lambda *a, **k: (seen_mapper.append(meshopts.given()), fake_reconstruct(*a, **k))[1]
run_all(c["id"], 10)
assert seen_mapper == [{"retry_below": 0.6, "more": 2}] and meshopts.given() is None, seen_mapper
server.set_campaign_options(c["id"], {"mapper_below": 0})
assert server.q("SELECT mapper_below, mapper_more FROM campaigns WHERE id=?", c["id"], one=True) == {"mapper_below": 0, "mapper_more": 2}
for bad in ({"mapper_below": 101}, {"mapper_more": 6}, {"mapper_more": "x"}, {"mapper_below": True}):
    try:
        server.set_campaign_options(c["id"], bad)
        raise AssertionError(bad)
    except ValueError as e:
        assert "a whole number from 0 to" in str(e), e
d = server.create_campaign("", "popularity", sub({"Gamma"}))
assert (d["mapper_below"], d["mapper_more"]) == (75, 1), "the defaults"
pipeline.reconstruct = fake_reconstruct
print("the mapper's settings OK")

# --- offloading to the backup (offload.py): marked, deleted only once the backup's copy began after, wanted back, put back
import offload  # noqa: E402

fresh()
server.q("DELETE FROM offloads", write=True)
day, now = 86400, time.time()
D = server.DATA


def put(rel: str, n: int = 2**20, age: float = 0) -> Path:
    f = D / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * n)
    os.utime(f, (now - age, now - age))
    return f


def climb(cid: str, path: list) -> None:
    server.q("INSERT INTO climbs(id,source,source_id,name,area_path,data) VALUES(?,?,?,?,?,?)", cid, "test", cid, cid, json.dumps(path), "{}",
             write=True)


def project(pid: str, cid: str | None, status: str, mesh: str | None = None, kind: str = "harvest", age: float = 20 * day) -> None:
    server.q("INSERT INTO projects(id,name,created,kind,status,climb_id,mesh_id,harvest) VALUES(?,?,?,?,?,?,?,?)", pid, pid, now - age, kind,
             status, cid, mesh, "{}", write=True)


def mesh(mid: str, age: float, replaced_by: str | None = None, climb_id: str | None = None) -> None:
    server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo,replaced_by) VALUES(?,?,?,?,?,?,?,?)", mid, mid, "mesh.glb",
             "upload", now - age, 1, 0, replaced_by, write=True)
    (server.MESHES / mid).mkdir(parents=True, exist_ok=True)
    if climb_id:
        server.link_mesh(mid, climb_id, "auto-linked from reconstruction project")


for cid, rock in (("c1", "Rock One"), ("c2", "Rock Two"), ("c4", "Rock Four"), ("c5", "Rock Five")):
    climb(cid, ["Somewhere", rock])
mesh("a1" * 6, 20 * day, climb_id="c1")
mesh("a0" * 6, 30 * day, replaced_by="a1" * 6)
put(f"meshes/{'a0' * 6}/mesh.glb", 2 * 2**20)
put(f"meshes/{'a0' * 6}/sources.json", 10)
mesh("a2" * 6, 2 * day, climb_id="c2")
mesh("a5" * 6, 20 * day, climb_id="c5")
project("d1" * 6, "c1", "done", "a1" * 6)                   # meshed 20 days ago: its photos may go
project("d2" * 6, "c2", "done", "a2" * 6)                   # meshed 2 days ago: not yet
project("d4" * 6, "c4", "done", "a1" * 6)                   # its climb is still to be meshed in an area
c = server.create_campaign("", "popularity", sub({"Alpha"}), start=False)
server.q("UPDATE campaign_items SET climb_id='c4', status='pending' WHERE campaign_id=?", c["id"], write=True)
for pid in ("d1" * 6, "d2" * 6, "d4" * 6):
    put(f"projects/{pid}/photos/a.jpg")
put(f"projects/{'d1' * 6}/masks/a.png", 1000)
put(f"projects/{'d2' * 6}/rejected/r.jpg", age=5 * day)     # rejected 5 days ago: may go
for vid, cid in (("v1aaaaaaaaa", "c1"), ("v5aaaaaaaaa", "c5")):
    put(f"cache/videos/{vid}.mp4")
    server.q("INSERT INTO climb_videos(climb_id,url,found_by,created) VALUES(?,?,?,?)", cid, f"https://www.youtube.com/watch?v={vid}",
             "search", now, write=True)
project("d5" * 6, "c5", "done", "a5" * 6, age=2 * day)
server.beta_mod.save(server.MESHES / ("a1" * 6), {"status": "done", "videos": []})      # c1's beta found; c5's never was
put("cache/http/old", 1000, age=40 * day)
got = {c[0]: c[1] for c in server.offload_candidates()}
assert got == {f"projects/{'d2' * 6}/rejected": "rejected photos", "cache/videos/v1aaaaaaaaa.mp4": "videos",
               f"projects/{'d1' * 6}/photos": "photos", f"projects/{'d1' * 6}/masks": "photos",
               f"meshes/{'a0' * 6}/mesh.glb": "older meshes"}, got
assert list(got)[0].endswith("rejected"), "rejected photos first"
server.settings().update({"storage.cap_gb": 0.001, "storage.offload_percent": 1.0})
server.measure_usage()
did = server.offload_step()
listed = (D / "offload.list").read_text().split()
assert sorted(listed) == sorted(got) and not did.get("freed"), "marked, not deleted: the backup hasn't copied them"
assert did["cache"] == 1000 and not (D / "cache/http/old").exists(), "the web cache's old pages go at once"
(D / "backed-up").write_text(str(time.time() - 3600))         # a copy that began before they were marked: not enough
assert not server.offload_step().get("freed") and (D / f"projects/{'d1' * 6}/photos/a.jpg").exists()
(D / "backed-up").write_text(str(time.time() + 1))
did = server.offload_step()
assert did["freed"] >= 5 * 2**20 and not (D / f"projects/{'d1' * 6}/photos/a.jpg").exists() and (D / f"projects/{'d1' * 6}/photos").is_dir()
assert not (D / "cache/videos/v1aaaaaaaaa.mp4").exists() and (D / "cache/videos/v5aaaaaaaaa.mp4").exists()
assert (D / f"meshes/{'a0' * 6}/sources.json").exists() and not (D / f"meshes/{'a0' * 6}/mesh.glb").exists(), "an older mesh keeps its records"
assert server.photos_away("d1" * 6) and sorted((D / "offload.list").read_text().split()) == sorted(got), "listed for the backup to keep"
(D / "backed-up").write_text(str(time.time() - 3600))         # no copy since: nothing more goes
st = offload.state(D, server.q)
assert st["away"]["photos"]["n"] == 2 and st["away"]["videos"]["n"] == 1 and st["backed_up"]
# a job that needs them asks for them back, and waits
try:
    server.need_back(["d1" * 6], "meshing it")
    raise AssertionError("should wait")
except server.PhotosAway as e:
    assert "in the backup" in str(e) and isinstance(e, server.compute.ComputeUnavailable)
assert (D / "offload-wanted.list").read_text().split() == [f"projects/{'d1' * 6}/masks", f"projects/{'d1' * 6}/photos"]
pooled_at = time.time()
f = put(f"restore-inbox/projects/{'d1' * 6}/photos/a.jpg", age=30 * day)
put(f"restore-inbox/projects/{'d1' * 6}/masks/a.png", 1000, age=30 * day)
assert sorted(server.offload_step()["back"]) == [f"projects/{'d1' * 6}/masks", f"projects/{'d1' * 6}/photos"]
assert (D / f"projects/{'d1' * 6}/photos/a.jpg").exists() and not server.photos_away("d1" * 6)
assert (D / f"projects/{'d1' * 6}/photos").stat().st_mtime < pooled_at - 29 * day, "its folder looks as it did"
assert not (D / "offload-wanted.list").read_text().strip() and not list((D / "restore-inbox").rglob("*.jpg"))
# a video fetched again is no longer offloaded; under the % nothing is marked, and what was is unmarked
put("cache/videos/v1aaaaaaaaa.mp4")
server.offload_step()
assert not server.q("SELECT 1 FROM offloads WHERE path='cache/videos/v1aaaaaaaaa.mp4' AND done IS NOT NULL"), "here again"
server.settings().update({"storage.cap_gb": 1000.0})
server.measure_usage()
server.offload_step()
assert not server.q("SELECT 1 FROM offloads WHERE done IS NULL"), "nothing marked once there's room"
server.settings().update({"storage.cap_gb": 0, "storage.offload_percent": 70.0})
print("offloading OK")

# --- areas being worked on, and those waiting their turn; a boulder's climbs, and the photos each gives its mesh --------
fresh()
server.settings().update({"areas.meshes": "boulders", "storage.cap_gb": 0})
old, mid_, new = (server.create_campaign("", "popularity", CSV) for _ in range(3))
for i, c in enumerate((old, mid_, new)):        # in this order, a second apart
    server.q("UPDATE campaigns SET created=?, name=? WHERE id=?", 1000.0 + i, f"area {i}", c["id"], write=True)
w2 = server.Worker("two")
server._workers[:] = [server._first, w2]
w2.campaign = mid_["id"]                         # a worker on the middle one; the other free
listed = server.campaigns_listed()
states = {v["name"]: v["state"] for v in listed}
assert states == {"area 0": "running", "area 1": "running", "area 2": "queued"}, "two workers: the oldest free one, and the third waits"
assert [v["name"] for v in listed][2] == "area 2", "queued after those being worked on"
server.q("UPDATE campaign_items SET status='done', mesh_id='m1', updated=? WHERE campaign_id=? AND name='Beta'", time.time(), mid_["id"], write=True)
assert [v["name"] for v in server.campaigns_listed()][:2] == ["area 1", "area 0"], "the one with the newest mesh first"
server.stop_campaign(old["id"])
w2.campaign = None
assert {v["name"]: v["state"] for v in server.campaigns_listed()} == {"area 0": "stopped", "area 1": "running", "area 2": "running"}
server._workers[:] = [server._first]
# Alpha and Alpha2 share a boulder: while their climbs are harvested, what each harvest kept; then, pooled, what each gives
camp = server.q("SELECT * FROM campaigns WHERE id=?", mid_["id"], one=True)
item = lambda name: server.q("SELECT * FROM campaign_items WHERE campaign_id=? AND name=?", camp["id"], name, one=True)  # noqa: E731
assert server.boulder_panel(camp, item("Beta")) is None, "a climb alone on its boulder: meshed on its own"
alpha = item("Alpha")
panel = server.boulder_panel(camp, alpha)
assert panel["name"] == "Alpha Boulder" and [(r["name"], r["state"], r["why"]) for r in panel["rows"]] == \
    [("Alpha", "pending", True), ("Alpha2", "pending", False)], panel
apid = "a1" * 6
(server.PROJECTS / apid / "photos").mkdir(parents=True)
for i in range(3):
    (server.PROJECTS / apid / "photos" / f"mountainproject_{i}.jpg").write_bytes(b"x")
server.set_item(camp["id"], alpha["source_id"], status="harvested", project_id=apid)
server.set_item(camp["id"], item("Alpha2")["source_id"], status="failed")
rows = {r["name"]: r for r in server.boulder_panel(camp, item("Alpha"))["rows"]}
assert (rows["Alpha"]["state"], rows["Alpha"]["photos"], rows["Alpha2"]["state"]) == ("in", 3, "out"), rows
# the boulder's mesh pooling them: each counted by the harvest its kept photos came from, and its own search apart
bkey = json.dumps(alpha["boulder"].split("/"))
bpid = "b2" * 6
(server.PROJECTS / bpid / "photos").mkdir(parents=True)
server.q("INSERT INTO projects(id,name,created,kind,boulder,status) VALUES(?,?,?,?,?,?)", bpid, "Alpha Boulder (boulder)", time.time(), "boulder",
         bkey, "harvested", write=True)
server.set_item(camp["id"], alpha["source_id"], status="running", redo="boulder")
for f in ("mountainproject_0.jpg", "mountainproject_1.jpg", "ddg_x.jpg"):
    (server.PROJECTS / bpid / "photos" / f).write_bytes(b"x")
(server.PROJECTS / bpid / server.CLIMBS_FILE).write_text(json.dumps({"climbs": [{"id": alpha["climb_id"] or "c-alpha", "projects": [apid]}]}))
(server.PROJECTS / bpid / "sources.json").write_text(json.dumps({"files": [{"file": f"mountainproject_{i}.jpg", "pooled_from": apid} for i in range(3)]
                                                                        + [{"file": "ddg_x.jpg"}]}))
if not alpha["climb_id"]:
    server.q("UPDATE campaign_items SET climb_id='c-alpha' WHERE campaign_id=? AND source_id=?", camp["id"], alpha["source_id"], write=True)
panel = server.boulder_panel(camp, item("Alpha"))
rows = {r["name"]: r for r in panel["rows"]}
assert panel["pooled"] and panel["own"] == 1 and (rows["Alpha"]["photos"], rows["Alpha"]["state"]) == (2, "in"), panel
assert (rows["Alpha2"]["photos"], rows["Alpha2"]["state"]) == (0, "out"), "kept none of its photos, or never harvested: none to give"
v = next(v for v in server.campaigns_listed() if v["id"] == camp["id"])
assert v["running"][0]["boulder_panel"]["name"] == "Alpha Boulder", "with the climb being worked on"
server.settings().update({"areas.meshes": "climbs"})
print("areas running and queued, and the boulder panel OK")
print("ALL CAMPAIGN TESTS PASSED")
