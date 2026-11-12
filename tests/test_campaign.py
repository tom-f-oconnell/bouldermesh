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
import server
import steptimes  # noqa: E402
from climbs.common import Cancelled, Climb, Media, jobctl  # noqa: E402

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


added: list[tuple[str, frozenset]] = []     # harvests that added to the one before: (climb, sources)


def fake_harvest(c, project_dir, log, sources, relevance_filter=True, tracker=None, pool_from=None, anchors_from=None, want=None,
                 want_search=None, add=False):
    harvested.append(c.name)
    if add:
        added.append((c.name, frozenset(sources)))
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
# areas stop rather than fill the disk (CAMPAIGN_MIN_FREE, 50 GB): not here, where the data is in /tmp, which on a shared
# machine (a tmpfs on hal) has more or less room as others use it, and stopped areas mid-test now and then
server.CAMPAIGN_MIN_FREE = 0
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
server.CAMPAIGN_MIN_FREE = float("inf")
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()
server.CAMPAIGN_MIN_FREE = 0
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


def climb_row_by_one(cid):    # what climb_row() worked out a climb at a time, six queries each, before climb_rows()
    r = server.q("SELECT * FROM climbs WHERE id=?", cid, one=True)
    r["area_path"] = json.loads(r["area_path"] or "[]")
    data = json.loads(r.pop("data") or "{}")
    r["n_images"], r["n_videos"] = len(data.get("images") or []), len(data.get("videos") or [])
    r["n_links"] = server.q("SELECT COUNT(*) n FROM mesh_links WHERE climb_id=? AND COALESCE(note,'') NOT LIKE ?", cid, server.SHARED + "%", one=True)["n"]
    boulder = server.boulder_of(r["area_path"])
    r["n_boulder"] = server.q("SELECT COUNT(*) n FROM meshes WHERE boulder=? AND replaced_by IS NULL", boulder, one=True)["n"] if boulder else 0
    shared = server.q(f"""SELECT l.note, ({server.MADE_FOR}) AS made_for FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id
                          WHERE l.climb_id=? AND l.note LIKE ? ORDER BY m.replaced_by IS NOT NULL, m.created DESC LIMIT 1""",
                      server.SHARED + "%", cid, server.SHARED + "%", one=True)
    r["n_shared"] = server.q("SELECT COUNT(*) n FROM mesh_links WHERE climb_id=? AND note LIKE ?", cid, server.SHARED + "%", one=True)["n"]
    r["shared_of"] = (shared["made_for"] or shared["note"][len(server.SHARED):]) if shared else None
    return r


every_id = [r["id"] for r in server.q("SELECT id FROM climbs ORDER BY name")] + ["nosuchclimb"]
assert server.climb_rows(every_id) == [climb_row_by_one(i) for i in every_id[:-1]], "all at once, as one at a time"
assert server.climb_rows([]) == [] and server.climb_row("nosuchclimb") is None
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
assert items(c["id"])["Alpha2"]["redo"] == "more+rebuild" and server.campaign_status(c["id"]) == "running"
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
seen_boulder = []


def reporting(c_, project_dir, log, sources, relevance_filter=True, tracker=None, **k):
    """Its harvest's progress moves the boulder's own, so that it's never taken as stuck (stall_check())."""
    if tracker and c_.name == "Alpha2":
        time.sleep(0.6)     # trackers save at most every 0.5 s
        tracker.update("filter", 0.5, "matching: 1 of 2")
        seen_boulder.append(json.loads(server.q("SELECT progress FROM projects WHERE kind='boulder'", one=True)["progress"]))
    return fake_harvest(c_, project_dir, log, sources, relevance_filter, tracker, **k)


server.harvest_mod.harvest = reporting
run_all(c["id"])
server.harvest_mod.harvest = fake_harvest
step = next(s for s in seen_boulder[-1]["steps"] if s["key"] == "climbs")
assert 0 < step["frac"] < 1 and step["detail"] == "Alpha2: filter off-topic photos, matching: 1 of 2", step
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

# --- one climb paused: its worker alone stops it, for real; the area goes on with its others without it, and doesn't
# finish while it's paused; a restart leaves it paused; resumed, it goes next and carries on in the same project
fresh()
outcome["Beta"] = "stop"
started.clear()
c = server.create_campaign("", "popularity", CSV)
sid = {i["name"]: i["source_id"] for i in items(c["id"]).values()}
stop_worker = threading.Event()
t = threading.Thread(target=loop, daemon=True)
t.start()
assert started.wait(20), "never got to Beta"
time.sleep(0.2)
assert server._first.item == sid["Beta"]
paused_at = time.time()
server.pause_item(c["id"], sid["Beta"])
running = server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["running"]
assert items(c["id"])["Beta"]["paused"] and all(r["paused"] for r in running if r["name"] == "Beta"), "pausing"
for _ in range(500):
    if server.campaign_status(c["id"]) == "running" and items(c["id"])["Delta2"]["status"] == "done":
        break
    time.sleep(0.02)
time.sleep(0.3)
got = items(c["id"])
assert time.time() - paused_at < 10, "tool wasn't killed"
assert got["Beta"]["status"] == "pending" and got["Beta"]["paused"] and "carries on" in got["Beta"]["note"], got["Beta"]
assert all(got[n]["status"] == "done" for n in ("Alpha", "Gamma", "Alpha2", "Delta", "Delta2")), got
assert harvested == ["Alpha", "Beta", "Gamma", "Alpha2", "Delta", "Delta2"], harvested
paused_pid = got["Beta"]["project_id"]
assert server.q("SELECT status FROM projects WHERE id=?", paused_pid, one=True)["status"] == "stopped"
view = next(v for v in server.campaigns_listed() if v["id"] == c["id"])
assert server.campaign_status(c["id"]) == "running" and view["state"] == "waiting" and server.PAUSED_NOTE in view["note"], view
assert view["counts"]["paused"] == 1 and [p["name"] for p in view["paused"]] == ["Beta"] and not view["next"], view
for bad, error in ((sid["Alpha"], ValueError), ("nope", KeyError)):
    try:
        server.pause_item(c["id"], bad)
        raise AssertionError(bad)
    except error:
        pass
stop_worker.set()       # a restart, with no worker on anything meanwhile
t.join(5)
server.init_db()
assert items(c["id"])["Beta"]["paused"] and server.campaign_status(c["id"]) == "running", "still paused"
outcome.clear()
server.pause_item(c["id"], sid["Beta"], False)
stop_worker = threading.Event()
t = threading.Thread(target=loop, daemon=True)
t.start()
for _ in range(500):
    if server.campaign_status(c["id"]) == "done":
        break
    time.sleep(0.02)
stop_worker.set()
t.join(5)
got = items(c["id"])
assert server.campaign_status(c["id"]) == "done" and got["Beta"]["status"] == "done" and not got["Beta"]["paused"], got["Beta"]
assert got["Beta"]["project_id"] == paused_pid and harvested[-1] == "Beta", "the same project carries on"
assert server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))["note"] is None
# among several workers: only the one on it is stopped
fresh()
c = server.create_campaign("", "popularity", CSV)
w1, w2 = server.Worker("one"), server.Worker("two", remote=True)
server._workers.extend([w1, w2])
with server._claim_lock:
    (_, a), (_, b) = server._claim(w1), server._claim(w2)
server.pause_item(c["id"], b["source_id"])
assert w2.control._stop.is_set() and not w1.control._stop.is_set() and not server._first.control._stop.is_set()
server._workers[:] = [server._first]
print("pausing one climb OK")

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

# --- jobs started outside areas: a restart marks one that had started interrupted, not failed, and Retry runs it again;
# one still waiting stays queued, and is queued again as the server starts, in the order they were
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
waited = own_project("queued", {"type": "boulder"})
later = own_project("queued", {"type": "reconstruct", "internet": True})
old = own_project("queued")     # from before jobs were recorded
st = server.jobs_state()
assert [j["name"] for j in st["queued"]] == ["mine running", "mine queued", "mine queued"] and st["running"] == [] and st["stopping"] is None, st
queued_items = [(i["what"], i["costly"], i["lost"]) for i in server.deploy_state()["items"]]
assert queued_items == [("reconstruct, queued", False, "queued again after"), ("boulder, queued", False, "queued again after"),
                        ("reconstruct, queued", False, "queued again after")], queued_items
with server.jobs.mutex:
    server.jobs.queue.clear()     # lost with the restart
server.init_db()
row = server.q("SELECT status, job FROM projects WHERE id=?", ran, one=True)
assert row["status"] == "interrupted" and "during this job" in server.last_failure(ran, "INTERRUPTED: "), (row, server.read_log(ran))
assert server.q("SELECT status FROM projects WHERE id=?", old, one=True)["status"] == "interrupted", "no job recorded: Retry runs it"
assert [server.q("SELECT status FROM projects WHERE id=?", p, one=True)["status"] for p in (waited, later)] == ["queued", "queued"]
assert server.requeue_waiting() == 2 and server.requeue_waiting() == 0, "once"
with server.jobs.mutex:
    again = list(server.jobs.queue)
    server.jobs.queue.clear()
assert again == [{"type": "boulder", "pid": waited}, {"type": "reconstruct", "internet": True, "pid": later}], "in the order they were asked for"
assert "queued again, as it was" in server.read_log(waited)
assert json.loads(server.q("SELECT job FROM projects WHERE id=?", ran, one=True)["job"])["resume"], "it had started: Retry carries on with it"
server.q("UPDATE projects SET status='interrupted' WHERE id=?", waited, write=True)
assert server.retry_project(ran) == ({"queued": True}, 202) and server.retry_project(waited)[1] == 202
with server.jobs.mutex:
    again = list(server.jobs.queue)
    server.jobs.queue.clear()
assert again == [{"type": "reconstruct", "internet": False, "pid": ran, "resume": True},
                 {"type": "boulder", "pid": waited}], "as they were asked for"
server.q("UPDATE projects SET status='failed' WHERE id=?", ran, write=True)
assert server.retry_project(ran)[1] == 202 and server.jobs.queue[-1] == {"type": "retry", "pid": ran}, "a failure: tried again"
with server.jobs.mutex:
    server.jobs.queue.clear()
assert server.retry_project(waited)[1] == 409, "already queued"
# Stop on the Projects tab: a queued job leaves the queue, a running one stops at once; either way it's stopped, and
# Retry runs it again as it was asked for, carrying on from the last step it finished if it had started
server.q("UPDATE projects SET status='queued' WHERE id=?", waited, write=True)
server.queue_job({"type": "boulder", "pid": waited})
assert server.stop_project(waited) == ({"stopped": True}, 200) and server.jobs.empty()
assert server.q("SELECT status FROM projects WHERE id=?", waited, one=True)["status"] == "stopped" and "stopped by hand" in server.read_log(waited)
assert server.stop_project(waited)[1] == 409, "nothing of it queued or running"
assert server.retry_project(waited)[1] == 202 and server.jobs.get_nowait() == {"type": "boulder", "pid": waited}, "as it was asked for"
stopper = server.Worker("this server 3")
server._workers.append(stopper)
stopper.job = {"job": {"type": "harvest", "pid": ran, "sources": ["youtube"], "reconstruct": False}, "started": time.time(), "from": 0.0}
server.q("UPDATE projects SET status='harvesting', job=? WHERE id=?", json.dumps(stopper.job["job"]), ran, write=True)
assert server.stop_project(ran)[1] == 200
try:
    stopper.control.check()
    raise AssertionError("the job running wasn't told to stop")
except Cancelled:
    pass
assert json.loads(server.q("SELECT job FROM projects WHERE id=?", ran, one=True)["job"])["resume"] is True
server._workers.remove(stopper)
assert server.retry_project(ran)[1] == 202 and server.jobs.get_nowait()["resume"] is True, "carries on from where it got to"
server.q("UPDATE projects SET status='harvesting' WHERE id=?", ran, write=True)
server.q("INSERT INTO campaigns(id, name, status, created) VALUES('s1', 'Some Area', 'running', 0)", write=True)
server.q("INSERT INTO campaign_items(campaign_id, source_id, name, project_id, status) VALUES('s1', 's1', 'Some Climb', ?, 'running')", ran,
         write=True)
assert server.stop_project(ran) == ({"paused": [{"area": "Some Area", "climb": "Some Climb"}]}, 200), "an area's climb: paused there"
assert server.q("SELECT paused FROM campaign_items WHERE campaign_id='s1'", one=True)["paused"]
server.q("DELETE FROM campaign_items WHERE campaign_id='s1'", write=True)
server.q("DELETE FROM campaigns WHERE id='s1'", write=True)

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

# nor for one whose work waits for a machine that connects here, which none has taken: nothing's done yet
server._stopping, said[:] = None, []
w.job = {"job": {"type": "retry", "pid": ran}, "started": time.time(), "from": 0.0, "thread": -2}
mj = server.MACHINES._new_job("reconstruct", {}, {}, ran, "img")
mj.owner = -2
fake = FakeServer()
stopper = threading.Thread(target=server.stop_when_idle, args=(fake,))
stopper.start()
stopper.join(5)
assert fake.stopped.is_set() and any("as no machine has taken it yet: after the restart, Retry on its project runs it" in x
                                     for x in said), said
server.MACHINES.jobs.pop(mj.id)
mj.input.unlink(missing_ok=True)
w.job = {"job": {"type": "retry", "pid": ran}, "started": time.time() - 120, "from": 0.0}

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
                        want_search=None, add=False):
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
assert it["status"] == "pending" and it["redo"] == "more+rebuild" and it["retry"] == 1, it
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

# --- what volunteers fetched, added to a harvest: its photos and record stay, only YouTube is looked at, and what it had
# before is kept in its summary; its photos brought back from the backup first if they were offloaded; from scratch
# without a harvest before
fresh()
acid = server.upsert_climb(Climb(source="test", source_id="add1", url="https://x/add1", name="Adding"))
apid = server.new_id()
(server.PROJECTS / apid / "photos").mkdir(parents=True)
(server.PROJECTS / apid / "photos" / "keep.jpg").write_bytes(b"x")
(server.PROJECTS / apid / "sources.json").write_text(json.dumps({"files": [{"file": "keep.jpg"}]}))
server.q("INSERT INTO projects(id,name,created,kind,climb_id,status,harvest) VALUES(?,?,?,?,?,?,?)", apid, "Adding (harvest)", time.time(),
         "harvest", acid, "done", json.dumps({"images": 1, "video_frames": 2, "videos": [{"url": "u1"}], "queries": ["q"]}), write=True)
seen_add = []


def adding(c_, project_dir, log, sources, relevance_filter=True, tracker=None, pool_from=None, anchors_from=None, want=None,
           want_search=None, add=False):
    seen_add.append((add, set(sources), (project_dir / "photos" / "keep.jpg").exists()))
    return {"images": 2, "video_frames": 1, "queries": [], "diversity": {"verdict": "good"}, "videos": [{"url": "u2"}]}


server.harvest_mod.harvest = adding
try:
    server.run_harvest(apid, set(server.HARVEST_SOURCES), False, again=True, add=True)
    assert seen_add == [(True, {"youtube"}, True)], seen_add
    h = json.loads(server.q("SELECT harvest FROM projects WHERE id=?", apid, one=True)["harvest"])
    assert h["video_frames"] == 3 and [v["url"] for v in h["videos"]] == ["u1", "u2"] and h["queries"] == ["q"], h
    assert "adding the videos volunteers fetched" in server.read_log(apid)
    # its photos in the backup: asked back, and it waits for them, as for a machine
    server.q("INSERT OR REPLACE INTO offloads(path, kind, ref, bytes, marked, done) VALUES(?,?,?,?,?,?)", f"projects/{apid}/photos",
             "photos", apid, 1, time.time() - 60, time.time() - 30, write=True)
    try:
        server.run_harvest(apid, set(server.HARVEST_SOURCES), False, again=True, add=True)
        raise AssertionError("harvested without its photos")
    except server.PhotosAway:
        pass
    assert server.q("SELECT wanted FROM offloads WHERE path=?", f"projects/{apid}/photos", one=True)["wanted"], "asked for back"
    assert len(seen_add) == 1
    server.q("DELETE FROM offloads", write=True)
    # without a harvest before: from scratch
    server.q("UPDATE projects SET harvest=NULL WHERE id=?", apid, write=True)
    server.run_harvest(apid, set(server.HARVEST_SOURCES), False, again=True, add=True)
    assert seen_add[-1] == (False, set(server.HARVEST_SOURCES), False), seen_add[-1]
finally:
    server.harvest_mod.harvest = fake_harvest
# climbs queued before to be harvested again from scratch for what volunteers fetched: added to instead, as the server
# starts; not one asked to harvest again by hand, nor one already started
cm = server.create_campaign("", "popularity", only("Alpha") + "".join(line for line in CSV.splitlines(True)[1:] if line.startswith(("Beta,", "Gamma,"))), start=False)
for name, redo, note, started in (("Alpha", "harvest+rebuild", "volunteers have fetched what it waited for: harvesting it again, then meshing it", 0),
                                  ("Beta", "harvest+rebuild", "queued to harvest again and rebuild", 0),
                                  ("Gamma", "harvest+rebuild", "waited 24 h for volunteers: meshing it with what's come", 1)):
    server.q("UPDATE campaign_items SET status='pending', redo=?, note=?, redo_started=? WHERE campaign_id=? AND name=?", redo, note, started,
             cm["id"], name, write=True)
hpid = server.new_id()
(server.PROJECTS / hpid / "photos").mkdir(parents=True)
server.q("INSERT INTO projects(id,name,created,kind,status,job) VALUES(?,?,?,?,?,?)", hpid, "Elsewhere (harvest)", time.time(), "harvest",
         "queued", json.dumps({"type": "harvest", "pid": hpid, "sources": ["youtube"], "reconstruct": False, "again": True}), write=True)
server.append_log(hpid, "volunteers fetched the videos this harvest couldn't have: harvesting it again (Reconstruct uses them)")
byhand = server.new_id()
(server.PROJECTS / byhand / "photos").mkdir(parents=True)
server.q("INSERT INTO projects(id,name,created,kind,status,job) VALUES(?,?,?,?,?,?)", byhand, "By hand (harvest)", time.time(), "harvest",
         "queued", json.dumps({"type": "harvest", "pid": byhand, "sources": ["youtube"], "reconstruct": False, "again": True}), write=True)
server.init_db()
assert {n: i["redo"] for n, i in items(cm["id"]).items()} == {"Alpha": "more+rebuild", "Beta": "harvest+rebuild", "Gamma": "harvest+rebuild"}
assert json.loads(server.q("SELECT job FROM projects WHERE id=?", hpid, one=True)["job"])["add"] is True, "a project outside areas, as its log says"
assert "add" not in json.loads(server.q("SELECT job FROM projects WHERE id=?", byhand, one=True)["job"]), "not one queued otherwise"
print("adding what volunteers fetched OK")

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

# --- a harvest's filtering on a machine that connects here, when one with a new enough script is in touch; here
# otherwise, or when none takes it in time; its rejections moved here, and the pairs it matched kept here
fpid = "f1f1f1f1f1f1"
fdir = server.PROJECTS / fpid
(fdir / "photos").mkdir(parents=True)
fphotos = []
for name in ("mountainproject_a.jpg", "ddg_b.jpg", "ddg_c.jpg"):
    (fdir / "photos" / name).write_bytes(name.encode())
    fphotos.append(fdir / "photos" / name)
other_harvest = server.PROJECTS / "f2f2f2f2f2f2" / "photos"
other_harvest.mkdir(parents=True)
(other_harvest / "mountainproject_z.jpg").write_bytes(b"z")
server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,?)", fpid, "Filtered (harvest)", time.time(), "harvest", "harvesting",
         write=True)
remote, here = [], []


def remote_filter(kind, files, args, label, log, tracker=None, version=None, take_within=None):
    remote.append((kind, sorted(files), args, label, version, take_within))
    tracker.update("filter", 0.5, "matching: 1 of 2")
    back = pathlib.Path(tempfile.mkdtemp())
    (back / "out").mkdir()
    (back / "out" / "relevance.json").write_text(json.dumps({"methods": ["cards"], "kept": ["mountainproject_a.jpg", "ddg_b.jpg"],
                                                             "rejected": {"ddg_c.jpg": "nomatch"}, "counts": {}, "scores": {}}))
    import sqlite3
    with sqlite3.connect(back / "out" / "cache.sqlite") as con:
        con.execute("CREATE TABLE clip(digest TEXT, model TEXT, p REAL, PRIMARY KEY(digest, model))")
        con.execute("CREATE TABLE keypoints(digest TEXT, sift TEXT, n INTEGER, PRIMARY KEY(digest, sift))")
        con.execute("CREATE TABLE pairs(a TEXT, b TEXT, how TEXT, inliers INTEGER, coverage REAL, PRIMARY KEY(a, b, how))")
        con.execute("INSERT INTO pairs VALUES('m1','m2','h',42,0.3)")
    return {"mesh": "", "files": []}, back


real_filter = server.relevance_mod.filter_photos
saved = (server.MACHINES.task, server.MACHINES.can, server._local_meshing, server._executor, server.relevance_mod.CACHE_FILE)
server.relevance_mod.CACHE_FILE = pathlib.Path(tempfile.mkdtemp()) / "relevance.sqlite"
server.relevance_mod.filter_photos = lambda paths, rejected, log, **kw: (here.append(kw), {"kept": [p.name for p in paths], "rejected": {}})[1]
server.MACHINES.task = remote_filter
server._local_meshing, server._executor = False, server.MACHINES
try:
    steps = []
    server.MACHINES.can = lambda version: version <= 11
    server.filter_anywhere(fphotos, fdir / "rejected", print, progress=lambda f=0.0, d=None: steps.append((f, d)))
    assert len(here) == 1 and not remote, "no machine with a script that filters: here"
    server.MACHINES.can = lambda version: version <= server.FILTER_VERSION
    prior = {"kept": ["ddg_b.jpg"], "rejected": {"ddg_c.jpg": "nomatch"}}
    report = server.filter_anywhere(fphotos, fdir / "rejected", print, progress=lambda f=0.0, d=None: steps.append((f, d)),
                                    more_anchors=[other_harvest / "mountainproject_z.jpg"], prior=prior, methods=["cards", "same-rock"])
    (kind, files, args, label, version, take_within), = remote
    assert (kind, version, take_within) == ("filter", server.FILTER_VERSION, server.FILTER_TAKE) and label == "filter: Filtered (harvest)"
    assert files == ["cache.sqlite", "more/0/mountainproject_z.jpg", "photos/ddg_b.jpg", "photos/ddg_c.jpg", "photos/mountainproject_a.jpg"], files
    assert args == {"methods": ["cards", "same-rock"], "more": ["more/0/mountainproject_z.jpg"], "prior": prior, "matcher": "brute"}, args
    assert report["rejected"] == {"ddg_c.jpg": "nomatch"} and (fdir / "rejected" / "nomatch_ddg_c.jpg").exists() and not fphotos[2].exists()
    assert steps[-1] == (0.5, "matching: 1 of 2"), steps
    assert server.relevance_mod.cache().pairs("h", ["m1"]) == {("m1", "m2"): (42, 0.3)}, "what it matched, kept here"
    (fdir / "rejected" / "nomatch_ddg_c.jpg").rename(fphotos[2])
    # its step says where it's done: waiting for a machine, then on the one that took it
    tracked = server.Tracker([("filter", "Filter off-topic photos", 1)], where="server")
    server.filter_anywhere(fphotos, fdir / "rejected", print, progress=tracked.callback("filter"))
    assert tracked.snapshot()["steps"][0]["where"] == "machine", tracked.snapshot()
    (fdir / "rejected" / "nomatch_ddg_c.jpg").rename(fphotos[2])
    # none took it in time, or it failed: here, as before
    server.MACHINES.task = no_machine
    here.clear()
    tracked = server.Tracker([("filter", "Filter off-topic photos", 1)], where="server")
    server.filter_anywhere(fphotos, fdir / "rejected", print, progress=tracked.callback("filter"))
    assert len(here) == 1 and tracked.snapshot()["steps"][0]["where"] == "server", tracked.snapshot()
    here.clear()
    server.filter_anywhere(fphotos, fdir / "rejected", print)
    assert len(here) == 1
    server.settings().update({"relevance.machines": False})
    server.MACHINES.task = remote_filter
    remote.clear()
    server.filter_anywhere(fphotos, fdir / "rejected", print)
    assert not remote, "turned off on the admin page"
    server.settings().update({"relevance.machines": True})
    # here, one harvest's at a time: another waits, saying so, until the first is done
    server.MACHINES.task = no_machine
    said, done = [], threading.Event()
    with server._filtering_here:
        waiter = threading.Thread(target=lambda: (server.filter_anywhere(fphotos, fdir / "rejected", said.append,
                                                                         progress=lambda f=0.0, d=None: said.append(d)), done.set()))
        waiter.start()
        time.sleep(0.5)
        assert not done.is_set() and "waiting for another harvest's filtering on the server" in said, said
    assert done.wait(10), "carries on once the other's done"
finally:
    (server.MACHINES.task, server.MACHINES.can, server._local_meshing, server._executor, server.relevance_mod.CACHE_FILE) = saved
    server.relevance_mod.filter_photos = real_filter
server.apply_settings()
assert server.relevance_mod.RUNNER is server.filter_anywhere, "harvests filter through it"
print("filtering on machines OK")

# --- a job whose progress stops while the server stays busy: its threads written down once, its log says where, and
# your phone is told; not while the server is idle (waiting on something), nor for a job on a machine
fresh()
spid = "5ta11ed00000"
server.q("INSERT INTO projects(id,name,created,kind,status,progress) VALUES(?,?,?,?,?,?)", spid, "Stuck (boulder)", time.time(), "boulder",
         "harvesting", json.dumps({"overall": 7.6, "steps": [{"key": "filter", "frac": 0.638, "state": "running"}]}), write=True)
told, dumps = [], []
real_send, real_dump, real_running = server.NOTIFIER.send, server.diagnostics.dump_threads, server.MACHINES.running
server.NOTIFIER.send = lambda title, text, **kw: told.append(title)
server.diagnostics.dump_threads = lambda dest, why, native=True: (dumps.append(why), dest / "threads-test.txt")[1]
server.MACHINES.running = lambda: []
server._stalls.clear()
server._stall_dumped.clear()
server._last_stall_dump = 0.0
try:
    t0 = 1_000_000.0
    assert server.stall_check(t0, 0.0) == []
    assert server.stall_check(t0 + 1200, 100.0) == [], "20 min, but under half a core: waiting, not stuck"
    server._stalls[spid] = (server._stalls[spid][0], t0, 0.0)
    assert server.stall_check(t0 + 1300, 1300.0) == [spid], "20 min at a core: written down"
    assert "no progress for 22 min" in dumps[0] and "1.0 cores" in dumps[0] and told == ["BoulderMesh: Stuck (boulder) looks stuck"]
    time.sleep(0.2)     # the notification goes on a thread
    assert "data/diagnostics/threads-test.txt" in server.read_log(spid)
    assert server.stall_check(t0 + 1400, 1400.0) == [], "once for this stall"
    server.q("UPDATE projects SET progress=? WHERE id=?", json.dumps({"overall": 8.0}), spid, write=True)
    assert server.stall_check(t0 + 1500, 1500.0) == [], "moving again"
    server._last_stall_dump = 0.0
    server.MACHINES.running = lambda: [{"label": spid}]
    server._stalls[spid] = (server._stalls[spid][0], t0, 0.0)
    assert server.stall_check(t0 + 3000, 3000.0) == [], "on a machine: this server only waits"
finally:
    server.NOTIFIER.send, server.diagnostics.dump_threads, server.MACHINES.running = real_send, real_dump, real_running
print("stalls written down OK")

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
assert (d["mapper_below"], d["mapper_more"]) == (75, 0), "the defaults: no more seeds"
# areas holding the old defaults (75%, 1 more seed) take the new ones, once; others keep theirs
server.q("UPDATE campaigns SET mapper_below=75, mapper_more=1 WHERE id=?", d["id"], write=True)
with server.db() as con:
    con.execute("PRAGMA user_version=0")
server.init_db()
assert server.q("SELECT mapper_more FROM campaigns WHERE id=?", d["id"], one=True)["mapper_more"] == 0
assert server.q("SELECT mapper_below, mapper_more FROM campaigns WHERE id=?", c["id"], one=True) == {"mapper_below": 0, "mapper_more": 2}
server.q("UPDATE campaigns SET mapper_more=1 WHERE id=?", d["id"], write=True)
server.init_db()
assert server.q("SELECT mapper_more FROM campaigns WHERE id=?", d["id"], one=True)["mapper_more"] == 1, "only once"
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
assert panel["url"] is None, "no page of a climb on it names its page yet"
on_it = alpha["boulder"]     # a climb on it, whose page names its areas' pages: the last is the boulder's
server.q("INSERT INTO climbs(id, source, source_id, url, name, area_path, data, created) VALUES(?,?,?,?,?,?,?,?)", "c-alpha3",
         "mountainproject", "999", "https://www.mountainproject.com/route/999/alpha3", "Alpha3", on_it,
         json.dumps({"area_urls": ["https://www.mountainproject.com/area/1/the-area", "https://www.mountainproject.com/area/2/alpha-boulder"]}),
         time.time(), write=True)
assert server.boulder_panel(camp, alpha)["url"] == "https://www.mountainproject.com/area/2/alpha-boulder"
server.q("DELETE FROM climbs WHERE id='c-alpha3'", write=True)
apid = "a1" * 6
(server.PROJECTS / apid / "photos").mkdir(parents=True)
for i in range(3):
    (server.PROJECTS / apid / "photos" / f"mountainproject_{i}.jpg").write_bytes(b"x")
server.set_item(camp["id"], alpha["source_id"], status="harvested", project_id=apid)
server.set_item(camp["id"], item("Alpha2")["source_id"], status="failed")
rows = {r["name"]: r for r in server.boulder_panel(camp, item("Alpha"))["rows"]}
assert (rows["Alpha"]["state"], rows["Alpha"]["photos"], rows["Alpha2"]["state"]) == ("in", 3, "out"), rows
# the boulder's mesh pooling them: each counted by the harvest its kept photos came from, and its own search apart
bkey = alpha["boulder"]
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
# meshing its boulder: the bars are the boulder's project's, not its own harvest's, and it says what it's meshing
server.q("UPDATE projects SET progress=? WHERE id=?", json.dumps({"overall": 42.0, "steps": []}), bpid, write=True)
server.q("UPDATE projects SET progress=? WHERE id=?", json.dumps({"overall": 100.0, "steps": []}), apid, write=True)
v = next(v for v in server.campaigns_listed() if v["id"] == camp["id"])
assert v["running"][0]["meshing"] == {"kind": "boulder", "name": "Alpha Boulder", "project_id": bpid}, v["running"][0]["meshing"]
assert v["running"][0]["progress"]["overall"] == 42.0, "the boulder's, not its own finished harvest's 100%"
w2.campaign, w2.doing = camp["id"], "Alpha2"     # why a machine asking for work gets none: what the areas are doing
server._workers[:] = [server._first, w2]
server._idle_why["at"] = 0
why = server.machines_idle_why()
assert "area 1 is harvesting Alpha2" in why and "still to harvest" in why, why
server._workers[:] = [server._first]
server.settings().update({"areas.meshes": "climbs"})
print("areas running and queued, and the boulder panel OK")

# --- how long each step takes, for the time left of the bars and deploy.sh: kept for every run (step_runs), filled at
# first from projects' last jobs, and estimated from the runs most like it: same kind, same machine, nearest in photos
fresh()
server.q("DELETE FROM step_runs", write=True)


def ran(pid: str, images: int, **seconds) -> None:
    steps = [{"key": k, "label": k, "frac": 1.0, "detail": "", "state": "done", "started": 1000.0 + i, "seconds": v}
             for i, (k, v) in enumerate(seconds.items())]
    server.q("INSERT INTO projects(id,name,created,kind,status,progress,harvest) VALUES(?,?,?,?,?,?,?)", pid, pid, time.time(), "harvest",
             "done", json.dumps({"overall": 100, "steps": steps}), json.dumps({"images": images}), write=True)


for i in range(3):
    ran(f"small{i}", 10, mapper=60 + i, download=30)
    ran(f"big{i}", 100, mapper=600 + i, download=40)
ran("one", 50, mapper=99)
server.q("INSERT INTO projects(id,name,created,kind,status,progress) VALUES('cut','cut',0,'harvest','stopped',?)",
         json.dumps({"overall": 5, "steps": [{"key": "mapper", "state": "running", "frac": 0.5, "started": 1.0}]}), write=True)
server.backfill_step_runs()
assert server.q("SELECT COUNT(*) n FROM step_runs", one=True)["n"] == 13, "a step cut short isn't kept"
server.backfill_step_runs()
assert server.q("SELECT COUNT(*) n FROM step_runs", one=True)["n"] == 13, "only once"
server._typicals["at"] = 0
steps = [{"key": "download", "state": "done", "frac": 1.0}, {"key": "mapper", "state": "running", "frac": 0.01},
         {"key": "dense", "state": "pending", "frac": 0.0}]
got = server.with_typicals({"overall": 10, "steps": [dict(s) for s in steps]}, json.dumps({"images": 10}))["steps"]
assert got[0].get("typical") is None and got[2].get("typical") is None and 55 < got[1]["typical"] < 70, got
big = server.with_typicals({"overall": 10, "steps": [dict(s) for s in steps]}, json.dumps({"images": 200}))["steps"][1]["typical"]
assert 1100 < big < 1300, ("twice the photos of the biggest: about twice its time", big)
assert 60 <= server.with_typicals({"overall": 0, "steps": [dict(s) for s in steps]}, None)["steps"][1]["typical"] <= 600, "any"
runs = [{"step": "mapper", "kind": "harvest", "machine": "server", "units": 20, "seconds": 900}] * 3 + \
       [{"step": "mapper", "kind": "harvest", "machine": "hal-2", "units": 20, "seconds": 100}] * 3 + \
       [{"step": "climbs", "kind": "boulder", "machine": "server", "units": 4, "seconds": 2000}] * 3
assert steptimes.estimate(runs, "mapper", 20, "harvest", "hal-2") == 100 and steptimes.estimate(runs, "mapper", 20, machine="server") == 900
assert steptimes.estimate(runs, "mapper", 20, "harvest", "atlas") is not None, "a machine with no runs: any"
assert steptimes.estimate(runs, "climbs", 8, "boulder") == 3600, "twice the climbs: scaled by (8 + 1) / (4 + 1)"
assert steptimes.estimate(runs, "dense", 20) is None
assert steptimes.job_left({"steps": [{"state": "done"}, {"state": "pending", "typical": 50}, {"state": "pending", "typical": 30}]}) == 80
assert steptimes.job_left({"steps": [{"state": "pending", "typical": 50}, {"state": "pending"}]}) is None
# a tracker keeps each step it finishes, with where it ran and how much it had to do
tpid = "f7" * 6
(server.PROJECTS / tpid / "photos").mkdir(parents=True, exist_ok=True)
for n in range(4):
    (server.PROJECTS / tpid / "photos" / f"{n}.jpg").write_bytes(b"x")
server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,?)", tpid, "B (boulder)", time.time(), "boulder", "running", write=True)
tr = server.tracker_for(tpid, [("climbs", "Harvest its climbs", 6), ("mapper", "Place cameras", 40)])
tr.units("climbs", 2)
tr.update("climbs", 0.5)
tr.update("mapper", 0.1)
tr.finish()
kept = server.q("SELECT step, kind, machine, units FROM step_runs WHERE project_id=? ORDER BY step", tpid)
assert kept == [{"step": "climbs", "kind": "boulder", "machine": "server", "units": 2},
                {"step": "mapper", "kind": "boulder", "machine": "server", "units": 4}], kept
server._typicals["at"] = 0
print("how long steps usually take OK")

# --- the Projects tab: which area a project was made for, and what stopping or deleting it does to that area's climb,
# said first; deleting leaves the climb something it can carry on with
fresh()
c = server.create_campaign("", "popularity", CSV)
area = server.q("SELECT name FROM campaigns WHERE id=?", c["id"], one=True)["name"]
rows = items(c["id"])


def made_project(name: str, kind: str = "harvest", status: str = "done", **extra) -> str:
    pid = server.new_id()
    (server.PROJECTS / pid / "photos").mkdir(parents=True)
    server.q("INSERT INTO projects(id,name,created,kind,status,harvest,boulder) VALUES(?,?,?,?,?,?,?)", pid, name, time.time(), kind, status,
             json.dumps({"images": 20}), extra.get("boulder"), write=True)
    return pid


def set_climb(name: str, **fields) -> None:
    server.set_item(c["id"], rows[name]["source_id"], **fields)


pa, pb, pg, pa2, pd = (made_project(f"{n} (harvest)") for n in ("Alpha", "Beta", "Gamma", "Alpha2", "Delta"))
server.q("INSERT INTO meshes(id,name,filename,source,created) VALUES('ma','Alpha','mesh.glb',?,0)", f"project:{pa}", write=True)
server.q("INSERT INTO climbs(id,source,source_id,url,name,created) VALUES('ca','mountainproject','1','https://x/1','Alpha',0)", write=True)
server.q("UPDATE projects SET climb_id='ca' WHERE id=?", pa, write=True)
set_climb("Alpha", status="done", project_id=pa, mesh_id="ma")
set_climb("Beta", status="pending", project_id=pb, paused=time.time())
set_climb("Gamma", status="running", project_id=pg)
set_climb("Alpha2", status="pending", project_id=pa2, mesh_id="ma2", redo="more+rebuild", was="done", retry=1)
set_climb("Delta", status="harvested", project_id=pd)
alpha_boulder, delta_boulder = rows["Alpha"]["boulder"], rows["Delta"]["boulder"]
pbo = made_project("Alpha Boulder (boulder)", "boulder", boulder=alpha_boulder)
(server.PROJECTS / pbo / server.CLIMBS_FILE).write_text(json.dumps({"climbs": [{"id": "x", "projects": [pa]}], "created": 0}))
pdb = made_project("Delta Boulders (boulder)", "boulder", status="new", boulder=delta_boulder)
set_climb("Delta2", status="pending", redo="boulder", was="harvested", retry=1)
listed = server.project_areas()
assert listed[pa] == [{"id": c["id"], "name": area, "kind": "climb", "climb": "Alpha", "status": "done", "paused": False}], listed[pa]
assert listed[pb][0]["paused"] and listed[pbo] == [{"id": c["id"], "name": area, "kind": "boulder", "climbs": 2}], listed[pbo]
fx = server.project_effects(pg)
assert "Gamma is in progress" in fx["delete"]["blocked"] and "Gamma is paused: the area goes on" in fx["stop"]["says"][0], fx
assert server.delete_project(pg)[1] == 409 and (server.PROJECTS / pg).is_dir(), "not while it's being worked on"
says = " ".join(server.project_effects(pa)["delete"]["says"])
assert "Alpha stays meshed, with its mesh" in says and "The mesh made from it stays" in says, says
assert "pooled in the mesh of Alpha Boulder" in says and "harvests Alpha again first" in says, says
assert "isn't to add what volunteers fetched and rebuild after all" in " ".join(server.project_effects(pa2)["delete"]["says"])
assert "Beta is harvested again from the start, in a new project, when the area gets to it, once you resume it" in \
    " ".join(server.project_effects(pb)["delete"]["says"])
assert server.project_effects(pb)["stop"]["blocked"], "paused already, and no job"
fx = server.project_effects(pdb)
assert "Delta2's queued mesh its boulder from every climb's photos starts over" in " ".join(fx["delete"]["says"]), fx
assert "Delta2 is paused before the area gets to it" in fx["stop"]["says"][0], fx
for pid in (pa, pb, pa2, pd):
    assert server.delete_project(pid) == ({"deleted": pid}, 200) and not (server.PROJECTS / pid).exists()
got = items(c["id"])
assert (got["Alpha"]["status"], got["Alpha"]["mesh_id"], got["Alpha"]["project_id"]) == ("done", "ma", None), got["Alpha"]
assert server.q("SELECT id FROM meshes WHERE id='ma'"), "its mesh stays"
assert (got["Beta"]["status"], got["Beta"]["project_id"]) == ("pending", None) and got["Beta"]["paused"], "still paused, to start over"
assert (got["Alpha2"]["status"], got["Alpha2"]["redo"], got["Alpha2"]["mesh_id"]) == ("done", None, "ma2"), got["Alpha2"]
assert (got["Delta"]["status"], got["Delta"]["project_id"], got["Delta"]["note"]) == \
    ("pending", None, "its project was deleted: harvested again from the start"), got["Delta"]
assert server.stop_project(pdb) == ({"paused": [{"area": area, "climb": "Delta2"}]}, 200)
pb2 = made_project("Beta (harvest)", status="stopped")
(server.PROJECTS / pb2 / "photos" / "a.jpg").write_bytes(b"x")
set_climb("Beta", project_id=pb2)
assert server.retry_project(pb2)[1] == 202 and not items(c["id"])["Beta"]["paused"], "Retry on its project resumes it"
# a job outside areas: stopping takes it out of the queue; deleting waits for that
mine = made_project("Mine", "user", status="queued")
server.queue_job({"type": "reconstruct", "pid": mine})
assert server.project_effects(mine)["stop"]["says"] == ["It leaves the queue, and Retry queues it again."]
assert "queued or running" in server.delete_project(mine)[0]["error"]
assert server.stop_project(mine)[1] == 200 and server.delete_project(mine)[1] == 200
assert server.delete_project(mine)[1] == 404
print("the Projects tab's areas, and stopping or deleting OK")

# --- beta found in each new mesh with video frames placed in it; kept videos beta can't use, deleted once settled ------
fresh()
server.q("DELETE FROM offloads", write=True)
real_videos = server.beta_mod.videos
server.beta_mod.videos = lambda d: {"https://www.youtube.com/watch?v=aaaaaaaaaaa": {}} if (d / "mesh.ply").exists() else {}
c = server.create_campaign("", "popularity", CSV)
server.next_campaign_step()     # Alpha, meshed
mid = items(c["id"])["Alpha"]["mesh_id"]
assert mid and server.beta_mod.load(server.MESHES / mid)["status"] == "queued", "queued with its mesh"
server.settings().update({"beta.auto": False})
server.next_campaign_step()     # Beta, meshed: not queued now
assert server.beta_mod.load(server.MESHES / items(c["id"])["Beta"]["mesh_id"]) is None
server.settings().update({"beta.auto": True})
server.stop_campaign(c["id"])
server.beta_mod.videos = real_videos
for m in server.MESHES.iterdir():
    (m / server.beta_mod.FILE).unlink(missing_ok=True)

fresh()
server.q("DELETE FROM offloads", write=True)
V = server.youtube_mod.VIDEO_CACHE
url = lambda v: f"https://www.youtube.com/watch?v={v * 11}"  # noqa: E731
for v, age in (("a", 7), ("b", 7), ("c", 7), ("d", 7), ("e", 1), ("h", 7)):
    put(f"cache/videos/{v * 11}.mp4", 1000, age * 3600)
    (V / f"{v * 11}.json").write_text("{}")


def sources(pid: str, vids: str, status: str) -> None:
    (server.PROJECTS / pid).mkdir(parents=True, exist_ok=True)
    (server.PROJECTS / pid / "sources.json").write_text(json.dumps({"files": [
        {"file": f"yt_{v}_000.jpg", "source": "youtube", "url": url(v)} for v in vids]}))
    server.q("INSERT INTO projects(id,name,created,kind,status) VALUES(?,?,?,?,?)", pid, pid, time.time(), "harvest", status, write=True)


sources("p1" * 6, "abefgh", "done")
sources("p2" * 6, "c", "harvested")
cp = server.create_campaign("", "popularity", CSV, start=False)
server.q("UPDATE campaign_items SET status='harvested', project_id=? WHERE campaign_id=? AND name='Alpha'", "p2" * 6, cp["id"], write=True)
m1 = server.MESHES / ("m1" * 6)
m1.mkdir(parents=True)
(m1 / "photo_sources.json").write_text(json.dumps({"basis": "placed", "photos": [{"file": "yt_b_000.jpg", "source": "youtube", "url": url("b")}]}))
(m1 / "cameras.json").write_text(json.dumps([{"name": "yt_b_000.jpg", "q": [1, 0, 0, 0], "t": [0, 0, 0]}]))
m2 = server.MESHES / ("m2" * 6)
m2.mkdir(parents=True)
server.beta_mod.save(m2, {"status": "queued", "videos": [], "waiting_videos": [url("h")]})
for v, done, wanted in (("f", now - day, None), ("g", now - day, now)):
    server.q("INSERT INTO offloads(path,kind,ref,bytes,marked,done,wanted) VALUES(?,?,?,?,?,?,?)", f"cache/videos/{v * 11}.mp4", "video",
             None, 1000, now - 2 * day, done, wanted, write=True)
out = server.prune_videos()
left = sorted(f.name for f in V.iterdir() if f.suffix == ".mp4" and len(set(f.stem)) == 1)     # not the offloading test's
assert left == ["bbbbbbbbbbb.mp4", "ccccccccccc.mp4", "ddddddddddd.mp4", "eeeeeeeeeee.mp4", "hhhhhhhhhhh.mp4"], left
assert out == {"deleted": 1, "bytes": 1000, "dropped": 1}, out
assert (V / "aaaaaaaaaaa.json").exists(), "its details stay"
assert [r["path"] for r in server.q("SELECT path FROM offloads")] == ["cache/videos/ggggggggggg.mp4"], "the one asked back stays"
assert "fffffffffff" not in (D / "offload.list").read_text() and "ggggggggggg" in (D / "offload.list").read_text()
# once its climb's boulder is meshed, and the video's frames not placed in it, Alpha's goes too
server.q("UPDATE campaign_items SET status='skipped' WHERE campaign_id=?", cp["id"], write=True)
server.q("UPDATE projects SET status='done' WHERE id=?", "p2" * 6, write=True)
os.utime(V / "ccccccccccc.mp4", (now - 7 * 3600, now - 7 * 3600))
assert server.prune_videos()["deleted"] == 1 and not (V / "ccccccccccc.mp4").exists()
print("beta with each new mesh, and pruning videos OK")

# --- what a restart would cost, for deploy.sh: work in progress, what it would lose, about how long it has left -------
fresh()
with server.jobs.mutex:
    server.jobs.queue.clear()     # what tests before left queued
assert server.deploy_state() == {"draining": False, "items": [], "costly": 0, "eta": None}
c = server.create_campaign("", "popularity", CSV, start=False)
pid = "e1" * 6
server.q("INSERT INTO projects(id,name,created,kind,status,progress) VALUES(?,?,?,?,?,?)", pid, "Alpha (harvest)", time.time(), "harvest",
         "harvesting", json.dumps({"overall": 30, "steps": [{"key": "download", "label": "Download photos", "frac": 0.5, "state": "running",
                                                            "started": time.time() - 120}]}), write=True)
server.q("UPDATE campaign_items SET status='running', project_id=?, updated=? WHERE campaign_id=? AND name='Alpha'", pid, time.time() - 120,
         c["id"], write=True)
st = server.deploy_state()
(it,) = st["items"]
assert it["name"] == "Alpha" and not it["costly"] and it["what"].endswith("download photos") and 100 < it["eta"] < 140, it
assert st["costly"] == 0, "a harvest only pauses"
# a mesh on a machine: ends with a restart; about as long left as its last jobs took, less what's gone
board = server.MACHINES
for i, took in enumerate((600, 900, 1200)):
    board.q("INSERT INTO machine_jobs(id,task,label,created,taken,ended,outcome) VALUES(?,?,?,?,?,?,?)", f"old{i}", "reconstruct", "x",
            1.0, 100.0, 100.0 + took, "done")
assert board.typical("reconstruct") == 900
(server.PROJECTS / pid / "photos").mkdir(parents=True, exist_ok=True)
job = board._new_job("reconstruct", {"photos": server.PROJECTS / pid / "photos"}, {"internet": True}, pid, "img")
job.state, job.machine_name, job.taken = "running", "hal-2", time.time() - 300
st = server.deploy_state()
mesh = next(i for i in st["items"] if i["what"].startswith("meshing on"))
assert mesh["costly"] and mesh["what"] == "meshing on hal-2" and 590 <= mesh["eta"] <= 610 and st["eta"] == mesh["eta"], st
assert mesh["name"] == "Alpha (harvest)", "its project's name, not its id"
# a size found on a machine, from a job here: told once, as the machine's job, under its mesh's name; not costly while
# it waits for a machine, as nothing's done yet, and while draining none takes it (Nov 11, 2026: it waited 90 min for that)
mid = "5c" * 6
server.q("INSERT INTO meshes(id,name,filename,source,created) VALUES(?,?,?,?,?)", mid, "East Face", "m.glb", "project:x",
         time.time(), write=True)
w = server.Worker("this server 2")
server._workers.append(w)
w.job = {"job": {"type": "scale", "mid": mid}, "started": time.time() - 60, "from": 0.0, "thread": -1}
sj = board._new_job("scale", {}, {}, f"size: {mid}", "img")
sj.owner = -1   # the worker's thread, which waits for it
st = server.deploy_state()
sizes = [i for i in st["items"] if i["what"].startswith("scale")]
assert [(i["what"], i["name"], i["costly"]) for i in sizes] == [("scale, waiting for a machine", "East Face", False)], sizes
assert st["costly"] == 1, "only the mesh on hal-2"
sj.state, sj.machine_name, sj.taken = "running", "hal-2", time.time()
sizes = [i for i in server.deploy_state()["items"] if i["what"].startswith("scale")]
assert [(i["what"], i["costly"]) for i in sizes] == [("scale on hal-2", True)], "once taken, costly"
# a job asked for here: time left from how fast it went up to its last progress, counting down while it doesn't move,
# rather than growing with the time it's taken (Nov 12, 2026: a boulder harvesting a climb went from 76 to 211 min)
real_progress = server.job_progress
seen = {"p": 10.0}
server.job_progress = lambda job: seen["p"]
try:
    run = {"job": {"type": "boulder", "pid": pid}, "started": time.time() - 600, "from": 0.0}
    first = server.job_info(run["job"], run)["eta"]
    assert 5350 < first < 5450, first     # 10% in 600 s: about 5400 s for the other 90%
    run["moved"] = (10.0, time.time() - 300)    # then no progress for 5 minutes
    stalled = server.job_info(run["job"], run)["eta"]
    assert 2350 < stalled < 2450, stalled  # 10% in the 300 s to then: 2700 s for the rest, less the 300 since
    run["moved"] = (10.0, time.time() - 3000)
    assert server.job_info(run["job"], run)["eta"] is None, "stalled for longer than it had: no telling"
    seen["p"] = 20.0
    assert server.job_info(run["job"], run)["eta"] is not None and run["moved"][0] == 20.0, "moving again"
finally:
    server.job_progress = real_progress
# with no machine's job, it's found here, and starts again after a restart (sizes carry on: scale.carry_on())
board.jobs.pop(sj.id)
sizes = [i for i in server.deploy_state()["items"] if i["what"].startswith("scale")]
assert [(i["what"], i["name"], i["costly"], i["lost"]) for i in sizes] == [("scale here", "East Face", True, "starts again after")], sizes
assert server.jobs_state()["running"][0]["machine_job"] is None
w.job = None
server._workers[:] = [server._first]
sj.input.unlink(missing_ok=True)
# taking no new work while it waits, and carrying on after
state = server.drain(True)
assert state["draining"] and board.paused and server._draining and not server._draining["now"]
assert server.drain(True, now=True) and server._draining["now"], "now too: a stop won't wait"
assert not server.drain(False)["draining"] and board.paused is None and server._draining is None
board.jobs.pop(job.id, None)
job.input.unlink(missing_ok=True)
server.stop_campaign(c["id"])
print("what a restart would cost OK")

# --- areas taken in the order entered, top first, kept in the database; entered again, they move to the end ----------
fresh()
url_of = lambda n: f"https://www.mountainproject.com/area/1000{n}/area-{n}"  # noqa: E731
real_export = server.mp_mod.export_area_csv
server.mp_mod.export_area_csv = lambda aid, sort="popularity", fresh=False: server.mp_mod.parse_area_csv(CSV)
first, second, third = (server.create_campaign(url_of(n), "popularity") for n in (1, 2, 3))
assert first["priority"] < second["priority"] < third["priority"]
for c, name in ((first, "first"), (second, "second"), (third, "third")):
    server.q("UPDATE campaigns SET name=?, created=? WHERE id=?", name, 3000.0 - c["priority"], c["id"], write=True)   # created the other way round
w2 = server.Worker("two")
server._workers[:] = [server._first, w2]
with server._claim_lock:
    picked = server._claim(w2)
assert picked[0]["name"] == "first", "by the order entered, not by when created"
w2.campaign = None
listed = {v["name"]: v for v in server.campaigns_listed()}
assert (listed["first"]["place"], listed["second"]["place"], listed["third"]["place"]) == (1, 2, 3)
server.create_campaign(url_of(1), "popularity")      # entered again: after the others now
assert [v["name"] for v in sorted(server.campaigns_listed(), key=lambda v: v["place"] or 99)][:3] == ["second", "third", "first"]
# one with no climb to take for now: waiting, and passed over
server.q("UPDATE campaign_items SET status='waiting' WHERE campaign_id=?", second["id"], write=True)
with server._claim_lock:
    picked = server._claim(w2)
assert picked[0]["name"] == "third", "the next with a climb to take"
listed = {v["name"]: v["state"] for v in server.campaigns_listed()}
assert listed["second"] == "waiting", listed
server._workers[:] = [server._first]
server.mp_mod.export_area_csv = real_export
print("areas in the order entered OK")

# --- what a climb's lowest area is, from its page (name and description): a group's climbs mesh on their own, a
# boulder's together; one harvested for an area thought a boulder, before its page could be read, meshes on its own -----
fresh()
server.q("DELETE FROM boulders", write=True)
server.settings().update({"areas.meshes": "boulders"})
PAGES = {"Bas Cuvier": "Main zone in the region, if im not mistaken this is the earliest development in Fontainebleau and has a "
                       "large amount of high quality test pieces, the area",
         "Peabody": "The Peabody is a huge boulder with classics on every side."}
area_url = lambda leaf: f"https://www.mountainproject.com/area/9{len(leaf)}/{leaf.lower().replace(' ', '-')}"  # noqa: E731
area_down: list[bool] = [False]


def with_areas(url, include_children=False):
    if "/area/" in url:
        if area_down[0]:
            raise OSError("Mountain Project is down")
        fetched.append(url)
        leaf = next(n for n in PAGES if area_url(n) == url)
        path = [leaf, "Sector", "* JT Bouldering", "Joshua Tree National Park", "California"][::-1]
        return [Climb(source="mountainproject", source_id=url.split("/")[-2], url=url, name=leaf, kind="area", area_path=path,
                      description=PAGES[leaf], raw={"routes": [("r", "x", "V1")] * 2})]
    out = fake_fetch(url, include_children)
    for c_ in out:
        if c_.area_path:
            c_.area_urls = [area_url(c_.area_path[-1])]
    return out


def leaf_row(rid: int, name: str, stars: float, boulder: str) -> str:    # as row() was (that name is taken above)
    return (f'{name},"{boulder} > Sector > * JT Bouldering > Joshua Tree National Park > California",'
            f'https://www.mountainproject.com/route/{rid}/{name.lower()},{stars},-1,Boulder,V{rid},,,34.0,-116.0\n')


server.climbs_mod.fetch_climb_url = with_areas
LEAVES = HEAD + "".join([leaf_row(11, "Marie", 4.0, "Bas Cuvier"), leaf_row(12, "Rose", 3.9, "Bas Cuvier"),
                         leaf_row(13, "Pea", 3.8, "Peabody"), leaf_row(14, "Body", 3.7, "Peabody")])
c = server.create_campaign("", "popularity", LEAVES)
run_all(c["id"])
got = items(c["id"])
assert got["Marie"]["status"] == got["Rose"]["status"] == "done" and got["Marie"]["mesh_id"] != got["Rose"]["mesh_id"], \
    "a sector's climbs: each its own mesh"
assert got["Pea"]["status"] == got["Body"]["status"] == "done" and got["Pea"]["mesh_id"] == got["Body"]["mesh_id"], "a boulder's: one"
kinds = {r["name"]: (r["kind"], r["why"]) for r in server.q("SELECT name, kind, why FROM boulders")}
assert kinds == {"Bas Cuvier": ("group", "its description says 'Main zone'"),
                 "Peabody": ("boulder", "its description says 'is a huge boulder'")}, kinds
assert sum("/area/" in u for u in fetched) == 3, "each area's page read once for what it is; a boulder's again, for its problems, as it's meshed"
marie = server.climb_row(got["Marie"]["climb_id"])
assert [p.name for p in server.boulder_projects(marie["id"])] == [got["Marie"]["project_id"]], "a sector's other climbs aren't its anchors"
assert server.boulder_key(["X", "Bas Cuvier"]) == "[\"X\", \"Bas Cuvier\"]" and server.leaf_kind(server.boulder_key(["X", "Hall of Horrors Area"])) == "group"
# harvested for the sector as one boulder while its page couldn't be read: meshed on its own once it's read
fresh()
server.q("DELETE FROM boulders", write=True)
area_down[0] = True
c = server.create_campaign("", "popularity", LEAVES.splitlines(True)[0] + "".join(LEAVES.splitlines(True)[1:3]))
server.next_campaign_step()
assert items(c["id"])["Marie"]["status"] == "harvested", "not read: pooled by its name, as before"
area_down[0] = False
run_all(c["id"])
got = items(c["id"])
assert got["Marie"]["status"] == got["Rose"]["status"] == "done" and got["Marie"]["mesh_id"] and got["Marie"]["redo"] is None, got["Marie"]
assert got["Marie"]["mesh_id"] != got["Rose"]["mesh_id"]
# boulders kept as their path joined by "/", which a name with one in it broke: JSON once the server starts, and those
# marked failed with a boulder whose mesh was made, done with it
fresh()
slash = ["California", "Bishop Area", "Drifter/Smoking Boulder"]
c = server.create_campaign("", "popularity", LEAVES.splitlines(True)[0] + leaf_row(21, "Drifter", 4.0, "Drifter/Smoking Boulder"))
server.q("UPDATE campaign_items SET boulder=?, data=?, status='failed', note=? WHERE campaign_id=?", "/".join(slash),
         json.dumps({"area_path": slash, "name": "Drifter"}), "its boulder meshed from 234 photos of 9 of its 9 climbs", c["id"], write=True)
server.q("INSERT INTO projects(id,name,created,kind,boulder,status,mesh_id) VALUES('dr1ft3r00000','x',1,'boulder',?,'done','m-drifter')",
         json.dumps(slash), write=True)
server.init_db()
got = items(c["id"])["Drifter"]
assert (got["boulder"], got["status"], got["mesh_id"]) == (json.dumps(slash), "done", "m-drifter"), got
assert got["note"] == "meshed with its boulder: 234 photos of 9 of its 9 climbs", got["note"]
# what a boulder's mesh left out of its climbs' photos (keep_rest()): the climbs with REST_LEAST or more out have theirs
# meshed together apart, once, from the boulder's own photos; each climb placed there linked to it; the boulder's card
# says group rather than boulder, and each climb counts toward the mesh placing most of its own photos
fresh()
c = server.create_campaign("", "popularity", HEAD + "".join([leaf_row(13, "Pea", 3.8, "Peabody"), leaf_row(14, "Body", 3.7, "Peabody"),
                                                            leaf_row(15, "Arm", 3.6, "Peabody")]))
real_keep, queued_rest = server.keep_rest, []
server.keep_rest = lambda mid, pid, log, queue=True, untied=True: (queued_rest.append((queue, untied)), real_keep(mid, pid, log, queue, untied))[1]
try:
    run_all(c["id"])
finally:
    server.keep_rest = real_keep
assert queued_rest == [(True, False)], "tied scenes queued by default (boulders.rest_auto); untied leftovers never by themselves"
got = items(c["id"])
boulder_mesh = got["Pea"]["mesh_id"]
assert boulder_mesh and got["Body"]["mesh_id"] == got["Arm"]["mesh_id"] == boulder_mesh, got
assert server.load_rest(boulder_mesh) is None, "without its cameras, what it left out can't be told: nothing kept"
bproj = server.q("SELECT * FROM projects WHERE mesh_id=? AND kind='boulder'", boulder_mesh, one=True)
harvest = {n: got[n]["project_id"] for n in ("Pea", "Body", "Arm")}
ids = {n: got[n]["climb_id"] for n in ("Pea", "Body", "Arm")}
photos = server.PROJECTS / bproj["id"] / "photos"
from PIL import Image  # noqa: E402
files, k = [], 0
for n, count in (("Pea", 10), ("Body", 9), ("Arm", 7)):
    for i in range(count):
        name = f"{'mountainproject' if i < 2 else 'ddg'}_{n.lower()}{i:02d}.jpg"
        Image.new("RGB", (8, 8), (k % 255, 40, 90)).save(photos / name)
        files.append({"file": name, "pooled_from": harvest[n], **({"also_in": [harvest["Arm"]]} if n == "Body" and i == 8 else {})})
        k += 1
(server.PROJECTS / bproj["id"] / "sources.json").write_text(json.dumps({"files": files}))
placed = [f["file"] for f in files if f["file"].startswith(("mountainproject_pea", "ddg_pea"))][:8] + ["ddg_body02.jpg"]
(server.MESHES / boulder_mesh / "cameras.json").write_text(json.dumps([{"name": n} for n in placed]))
logged = []
before = []
while not server.jobs.empty():
    before.append(server.jobs.get_nowait())
untied = server.keep_rest(boulder_mesh, bproj["id"], logged.append, untied=False)
assert untied["apart"] and untied["attempt"] is None and server.jobs.empty(), \
    ("untied leftovers (a mesh from before ties) aren't queued after a boulder's mesh", untied, before)
for j in before:
    server.jobs.put(j)
rec = server.keep_rest(boulder_mesh, bproj["id"], logged.append)
assert rec["apart"] == sorted([ids["Arm"], ids["Body"]], key=lambda i: {ids["Arm"]: "Arm", ids["Body"]: "Body"}[i]), rec["apart"]
assert len(rec["climbs"][ids["Body"]]["left"]) == 8 and rec["climbs"][ids["Body"]]["own"] == 2, rec["climbs"][ids["Body"]]
assert len(rec["climbs"][ids["Arm"]]["left"]) == 8, "and the photo another climb's harvest had too"
assert len(rec["climbs"][ids["Pea"]]["left"]) == 2 and ids["Pea"] not in rec["apart"], "too few out to mesh apart"
queued = []
while not server.jobs.empty():
    queued.append(server.jobs.get_nowait())
rpid = rec["attempt"]["project"]
assert {"type": "boulder", "pid": rpid} in queued, queued
assert server.q("SELECT status FROM projects WHERE id=?", rpid, one=True)["status"] == "queued"
assert any("queued to be meshed together" in s for s in logged), logged
assert server.keep_rest(boulder_mesh, bproj["id"], logged.append)["attempt"]["project"] == rpid and server.jobs.empty(), "once"
real_assess, real_place = server.diversity_mod.assess, server.place_climbs
server.diversity_mod.assess = lambda photos, log: {"verdict": "good", "unique_images": 15}


def place_body(mid, pid, log):     # Body's own photos place in it; Arm's don't
    (server.MESHES / mid / "views.json").write_text(json.dumps({"climbs": {ids["Body"]: {"photos": 2, "of": 2}}}))


server.place_climbs = place_body
try:
    server.run_boulder(rpid)
finally:
    server.diversity_mod.assess, server.place_climbs = real_assess, real_place
rproj = server.q("SELECT * FROM projects WHERE id=?", rpid, one=True)
rest_mesh = rproj["mesh_id"]
assert rproj["status"] == "done" and rest_mesh and rest_mesh != boulder_mesh, rproj
assert sorted(f.name for f in (server.PROJECTS / rpid / "photos").iterdir()) == sorted(set(rec["climbs"][ids["Body"]]["left"]) | set(rec["climbs"][ids["Arm"]]["left"]))
own = {c["id"]: c["own"] for c in json.loads((server.PROJECTS / rpid / "climbs.json").read_text())["climbs"]}
assert sorted(own[ids["Body"]]) == ["mountainproject_body00.jpg", "mountainproject_body01.jpg"], own
assert [r["climb_id"] for r in server.q("SELECT climb_id FROM mesh_links WHERE mesh_id=? AND note=?", rest_mesh, server.REST_LINK)] == [ids["Body"]]
attempt = server.load_rest(boulder_mesh)["attempt"]
assert (attempt["status"], attempt["mesh"], attempt["climbs"]) == ("done", rest_mesh, [ids["Body"]]), attempt
assert server.q("SELECT boulder FROM meshes WHERE id=?", rest_mesh, one=True)["boulder"] == json.dumps(json.loads(got["Pea"]["boulder"]) + [server.REST_MARK + boulder_mesh])
assert server.q("SELECT name FROM meshes WHERE id=?", rest_mesh, one=True)["name"] == "Body", "named for the climbs it shows, not all its pool's"
kinds = server.boulder_kinds()
views = {boulder_mesh: {"climbs": {ids["Pea"]: {"photos": 8}, ids["Body"]: {"photos": 1}}}, rest_mesh: {"climbs": {ids["Body"]: {"photos": 2}}}}
rows = server.q("SELECT id, boulder FROM meshes WHERE id IN (?, ?)", boulder_mesh, rest_mesh)
homes = server.boulder_homes(rows, views)
assert homes == {ids["Body"]: rest_mesh}, homes
on_parent = [{"id": i, "view": (views[boulder_mesh]["climbs"]).get(i)} for i in ids.values()]
parent = server.pooled_summary(got["Pea"]["boulder"], on_parent, kinds, boulder_mesh, homes)
assert (parent["whole"], parent["split"], parent["placed"], parent["kind"]) == (False, True, 1, "boulder"), parent
rest_card = server.pooled_summary(rows[0]["boulder"] if rows[0]["id"] == rest_mesh else rows[1]["boulder"],
                                  [{"id": ids["Body"], "view": {"photos": 2}}, {"id": ids["Arm"], "view": None}], kinds, rest_mesh, homes)
assert (rest_card["placed"], rest_card["rest_of"], rest_card["whole"]) == (1, boulder_mesh, False), rest_card
# meshing what it left out failed, or nothing was left out to: one boulder
rec = server.load_rest(boulder_mesh)
server.save_rest(boulder_mesh, {**rec, "attempt": {**rec["attempt"], "status": "failed", "mesh": None, "climbs": []}})
assert server.pooled_summary(got["Pea"]["boulder"], on_parent, kinds, boulder_mesh)["whole"] is True
server.save_rest(boulder_mesh, {**rec, "apart": [], "attempt": None})
assert server.pooled_summary(got["Pea"]["boulder"], on_parent, kinds, boulder_mesh)["whole"] is True
one_placed = [{**c, "view": c["view"] if c["id"] == ids["Pea"] else None} for c in on_parent]
assert server.pooled_summary(got["Pea"]["boulder"], one_placed, kinds, boulder_mesh)["whole"] is False, "one climb isn't a boulder"
server.save_rest(boulder_mesh, {**rec, "attempt": None})
summary = server.pooled_summary(got["Pea"]["boulder"], on_parent, kinds, boulder_mesh)
assert (summary["whole"], summary["apart"], summary["rest"]) == (False, 2, None), ("enough left out, never tried: a group till it is", summary)
while not server.jobs.empty():
    server.jobs.get_nowait()
assert server.rest_waiting() == [] and server.rest_queue_all() == 0 and server.jobs.empty(), \
    "untied leftovers from before ties are never queued, even from the admin page: meshing the boulder again checks them"
# one boulder needs every climb on it harvested and tried in its mesh: one known since, or listed on its page and
# never harvested, holds it a group; its page's problems become known as its mesh is made, to harvest and place too
server.save_rest(boulder_mesh, {**rec, "apart": [], "attempt": None})
assert server.boulder_untried(boulder_mesh, got["Pea"]["boulder"]) == 0
assert server.pooled_summary(got["Pea"]["boulder"], on_parent, kinds, boulder_mesh)["whole"] is True
# each climb with photos of its own must have some placed, and at least the admin page's % of them
shares = [{"id": ids["Pea"], "name": "Pea", "view": {"photos": 8, "of": 10}}, {"id": ids["Body"], "name": "Body", "view": {"photos": 1, "of": 9}},
          {"id": ids["Arm"], "name": "Arm", "view": None, "own": 0}]
summary = server.pooled_summary(got["Pea"]["boulder"], shares, kinds, boulder_mesh)
assert (summary["whole"], summary["short"], summary["least"]) == (True, [], 0), "one of each is enough by default; Arm has none of its own"
summary = server.pooled_summary(got["Pea"]["boulder"], shares, kinds, boulder_mesh, least=20)
assert (summary["whole"], summary["short"]) == (False, [{"name": "Body", "placed": 1, "of": 9}]), summary
shares[2]["own"] = 7
summary = server.pooled_summary(got["Pea"]["boulder"], shares, kinds, boulder_mesh)
assert (summary["whole"], summary["short"]) == (False, [{"name": "Arm", "placed": 0, "of": 7}]), "own photos, none placed"
server.settings().update({"boulders.placed_percent": 20})
assert server.pooled_summary(got["Pea"]["boulder"], shares[:2], kinds, boulder_mesh)["least"] == 20
server.settings().update({"boulders.placed_percent": 0})
# the admin page's and the mesh's log's account of them, fewest placed first
(server.MESHES / boulder_mesh / "views.json").write_text(json.dumps({"climbs": {ids["Pea"]: {"photos": 8, "of": 10}, ids["Body"]: {"photos": 1, "of": 9}},
                                                                     "unplaced": {ids["Arm"]: 7}}))
mine = [m for m in server.boulder_shares() if m["id"] == boulder_mesh]
assert mine and mine[0]["climbs"] == [{"name": "Arm", "placed": 0, "of": 7}, {"name": "Body", "placed": 1, "of": 9},
                                      {"name": "Pea", "placed": 8, "of": 10}], mine
logged = []
server.log_shares(boulder_mesh, logged.append)
assert logged == ["own photos placed, each climb with any: Arm 0/7 (under), Body 1/9, Pea 8/10; a boulder needs at least 0% of each, and one"], logged
assert server.settings().get("boulders.rest_auto") is True, "scenes tied to a boulder's mesh are meshed apart by themselves"
late = server.upsert_climb(Climb(source="mountainproject", source_id="977", url="https://x/977", name="Late", area_path=json.loads(got["Pea"]["boulder"])))
summary = server.pooled_summary(got["Pea"]["boulder"], on_parent, kinds, boulder_mesh)
assert (summary["whole"], summary["untried"]) == (False, 1), summary
server.q("DELETE FROM climbs WHERE id=?", late, write=True)
record = json.loads((server.PROJECTS / bproj["id"] / "climbs.json").read_text())
(server.PROJECTS / bproj["id"] / "climbs.json").write_text(json.dumps({**record, "page_problems": 4}))
assert server.boulder_untried(boulder_mesh, got["Pea"]["boulder"]) == 1, "its page listed one more than were tried"
server.q("INSERT OR REPLACE INTO boulders(key, name, url, kind, why, routes, checked) VALUES(?,?,?,?,?,?,?)",
         got["Pea"]["boulder"], "Peabody", "https://www.mountainproject.com/area/9/peabody", "boulder", "test", 4, 1, write=True)
page = Climb(source="mountainproject", source_id="9", url="https://www.mountainproject.com/area/9/peabody", name="Peabody", kind="area")
stubs = [Climb(source="mountainproject", source_id=sid, url=f"https://www.mountainproject.com/route/{sid}/x", name=n, kind="boulder",
               raw={"stub": True}) for sid, n in (("13", "Pea"), ("14", "Body"), ("15", "Arm"), ("16", "Leg"))]
real_fetch = server.climbs_mod.fetch_climb_url
server.climbs_mod.fetch_climb_url = lambda url, include_children=False: [page] + (stubs if include_children else [])
try:
    logged = []
    assert server.adopt_page_climbs(got["Pea"]["boulder"], logged.append) == 4
finally:
    server.climbs_mod.fetch_climb_url = real_fetch
leg = server.q("SELECT * FROM climbs WHERE source_id='16'", one=True)
assert leg and leg["area_path"] == got["Pea"]["boulder"] and "1 problem its Mountain Project page lists" in logged[0], (leg, logged)
assert server.q("SELECT COUNT(*) n FROM climbs WHERE area_path=?", got["Pea"]["boulder"], one=True)["n"] == 4, "the known ones as they were"

# scenes from one run: where the reconstruction recorded each model COLMAP made and how the photos the mesh left out are
# tied to those it placed (models.json), a model of 6 or more of them is a scene to mesh apart if it holds a climb's page
# photo, or 2 of them share 16+ verified matches with placed photos; each is recorded with why, meshed once (with
# boulders.rest_auto, or from the admin page), and only one waiting or meshed keeps the boulder's mesh a group
fresh()
server.settings().update({"boulders.rest_auto": False})
c = server.create_campaign("", "popularity", HEAD + "".join([leaf_row(13, "Pea", 3.8, "Peabody"), leaf_row(14, "Body", 3.7, "Peabody"),
                                                            leaf_row(15, "Arm", 3.6, "Peabody")]))
run_all(c["id"])
got = items(c["id"])
bmesh = got["Pea"]["mesh_id"]
bproj = server.q("SELECT * FROM projects WHERE mesh_id=? AND kind='boulder'", bmesh, one=True)
ids = {n: got[n]["climb_id"] for n in ("Pea", "Body", "Arm")}
harvest = {n: got[n]["project_id"] for n in ("Pea", "Body", "Arm")}
leaf = got["Pea"]["boulder"]
by = {"Pea": ["mountainproject_pea00.jpg", "mountainproject_pea01.jpg"] + [f"ddg_pea{i:02d}.jpg" for i in range(2, 12)],
      "Body": ["mountainproject_body00.jpg", "mountainproject_body01.jpg"] + [f"yt_bodyvid0_{i:03d}.jpg" for i in range(14)],
      "Arm": ["mountainproject_arm00.jpg"] + [f"ddg_arm{i:02d}.jpg" for i in range(1, 9)],
      "Web": [f"ddg_web{i:02d}.jpg" for i in range(16)]}
bphotos = server.PROJECTS / bproj["id"] / "photos"
for n in sum(by.values(), []):
    (bphotos / n).write_bytes(b"x")
(server.PROJECTS / bproj["id"] / "sources.json").write_text(json.dumps({"files": [
    {"file": f, "pooled_from": harvest["Pea" if n == "Web" else n]} for n, fs in by.items() for f in fs]}))
record = json.loads((server.PROJECTS / bproj["id"] / "climbs.json").read_text())
for c_ in record["climbs"]:
    c_["own"] = [f for f in by[c_["name"]] if not f.startswith("ddg_")]
(server.PROJECTS / bproj["id"] / "climbs.json").write_text(json.dumps(record))
main = by["Pea"][:8] + ["mountainproject_body00.jpg", "yt_bodyvid0_000.jpg", "yt_bodyvid0_001.jpg", "ddg_arm01.jpg"] + by["Web"][:4]
models = {"main": "0", "models": [
    {"name": "0", "photos": sorted(main)},
    {"name": "1", "photos": sorted(by["Web"][4:12])},                                             # 8, two tied: counts
    {"name": "aborted0-1", "photos": sorted(by["Web"][4:12])},                                    # the same, from another seed
    {"name": "2", "photos": sorted([f"yt_bodyvid0_{i:03d}.jpg" for i in range(2, 9)])},           # 7, one tied: no
    {"name": "3", "photos": sorted(["mountainproject_arm00.jpg"] + [f"ddg_arm{i:02d}.jpg" for i in range(2, 7)])},  # page photo
    {"name": "4", "photos": sorted(by["Web"][12:16] + [f"yt_bodyvid0_{i:03d}.jpg" for i in (9, 10)])},  # 6, untied
    {"name": "5", "photos": sorted(by["Pea"][2:8])},                                              # all placed in the main one
    {"name": "6", "photos": sorted(["ddg_arm07.jpg", "ddg_arm08.jpg", "yt_bodyvid0_011.jpg", "yt_bodyvid0_012.jpg", "yt_bodyvid0_013.jpg"])}]}
placed = set(main)
tie = {"ddg_web04.jpg": 40, "ddg_web05.jpg": 16, "yt_bodyvid0_002.jpg": 30, "ddg_web12.jpg": 15, "yt_bodyvid0_009.jpg": 3}
models["left"] = {n: {"with": main[0] if tie.get(n) else None, "inliers": tie.get(n, 0)}
                  for n in sum(by.values(), []) if n not in placed}
(server.MESHES / bmesh / "models.json").write_text(json.dumps(models))
(server.MESHES / bmesh / "cameras.json").write_text(json.dumps([{"name": n} for n in main]))
logged = []
rec = server.keep_rest(bmesh, bproj["id"], logged.append, queue=False)
scene = {m["name"]: m for m in rec["models"]}
assert [m["name"] for m in rec["models"] if m["counts"]] == ["1", "3"] and "apart" not in rec and server.jobs.empty(), rec["models"]
assert scene["1"]["tied"] == 2 and "2 of its photos share 16+ verified matches" in scene["1"]["why"], scene["1"]
assert scene["aborted0-1"]["photos"] == [] and "the rest being in a larger one" in scene["aborted0-1"]["why"], "one scene, once"
assert not scene["2"]["counts"] and scene["2"]["tied"] == 1 and "(it needs 2)" in scene["2"]["why"], scene["2"]
assert scene["3"]["page"] == ["mountainproject_arm00.jpg"] and "page photo of Arm" in scene["3"]["why"], scene["3"]
assert scene["3"]["climbs"] == [ids["Arm"]] and scene["1"]["climbs"] == [ids["Pea"]], "by own photos, else by whose harvests had them"
assert not scene["4"]["counts"] and not scene["5"]["counts"] and scene["5"]["photos"] == [] and not scene["6"]["counts"]
assert "a mesh needs 6" in scene["6"]["why"] and "not tied to the boulder" in scene["4"]["why"], (scene["4"], scene["6"])
assert any("COLMAP made 7 other models of its photos, 2 of them a scene tied to it to mesh apart: " in x for x in logged), logged
# a climb's own photos left out, counted as its views count them: page photos always, video frames tied to placed photos
assert rec["climbs"][ids["Body"]]["own"] == 2 and len(rec["climbs"][ids["Body"]]["left"]) == 13, rec["climbs"][ids["Body"]]
views = {"climbs": {ids["Pea"]: {"photos": 2, "of": 2}, ids["Body"]: {"photos": 3, "of": 5}, ids["Arm"]: {"photos": 1, "of": 1}}}
(server.MESHES / bmesh / "views.json").write_text(json.dumps(views))
on_b = [{"id": i, "name": n, "view": views["climbs"][i]} for n, i in ids.items()]
kinds = server.boulder_kinds()
card = lambda: server.pooled_summary(leaf, on_b, kinds, bmesh)  # noqa: E731
assert server.boulder_untried(bmesh, leaf) == 0
summary = card()
assert (summary["whole"], summary["apart"], summary["rest"], summary["scenes"], summary["split"]) == (False, 2, None, True, False), summary
assert server.rest_waiting() == [bmesh]
# untied, or no scene at all: one boulder, whatever each climb left out (the old 6 per climb no longer counts)
for m in rec["models"]:
    m["counts"] = False
server.save_rest(bmesh, rec)
assert card()["whole"] is True and card()["apart"] == 0 and server.rest_waiting() == []
rec = server.keep_rest(bmesh, bproj["id"], logged.append, queue=False)
# the admin page's: each scene queued once, in a pool and project of its own; again only if cut short
assert server.rest_queue_all() == 1
queued = []
while not server.jobs.empty():
    queued.append(server.jobs.get_nowait())
rec = server.load_rest(bmesh)
att = {m["name"]: m["attempt"] for m in rec["models"] if m["counts"]}
assert sorted(queued, key=lambda j: j["pid"]) == sorted([{"type": "boulder", "pid": a["project"]} for a in att.values()], key=lambda j: j["pid"])
key3 = json.dumps(json.loads(leaf) + [f"{server.REST_MARK}{bmesh} 3"])
assert server.q("SELECT boulder FROM projects WHERE id=?", att["3"]["project"], one=True)["boulder"] == key3
assert server.rest_ref(key3) == (bmesh, "3") and server.rest_parent(key3) == bmesh and server.pool_members(key3) == [ids["Arm"]]
assert card()["rest"] == "queued" and server.rest_waiting() == [] and server.rest_queue_all() == 0 and server.jobs.empty()
# a scene meshed: its climbs placed by their own photos linked to it, and it's named for them
real_assess, real_place = server.diversity_mod.assess, server.place_climbs
server.diversity_mod.assess = lambda photos, log: {"verdict": "good", "unique_images": 15}
server.place_climbs = lambda mid, pid, log: (server.MESHES / mid / "views.json").write_text(json.dumps({"climbs": {ids["Arm"]: {"photos": 1, "of": 1}}}))
try:
    server.run_boulder(att["3"]["project"])
finally:
    server.diversity_mod.assess, server.place_climbs = real_assess, real_place
p3 = server.q("SELECT * FROM projects WHERE id=?", att["3"]["project"], one=True)
assert p3["status"] == "done" and p3["mesh_id"], p3
assert sorted(f.name for f in (server.PROJECTS / p3["id"] / "photos").iterdir()) == scene["3"]["photos"]
assert json.loads((server.PROJECTS / p3["id"] / "climbs.json").read_text())["climbs"][0]["own"] == ["mountainproject_arm00.jpg"]
assert server.q("SELECT name FROM meshes WHERE id=?", p3["mesh_id"], one=True)["name"] == "Arm"
assert [r["climb_id"] for r in server.q("SELECT climb_id FROM mesh_links WHERE mesh_id=?", p3["mesh_id"])] == [ids["Arm"]]
a3 = {m["name"]: m["attempt"] for m in server.load_rest(bmesh)["models"]}["3"]
assert (a3["status"], a3["mesh"], a3["climbs"]) == ("done", p3["mesh_id"], [ids["Arm"]]), a3
# the other's mesh made nothing for want of photos ("failed"), or failed otherwise ("error", to try again)
p1 = server.q("SELECT name FROM projects WHERE id=?", att["1"]["project"], one=True)["name"]
for how, status in (("boom", "error"), ("few", "failed")):
    outcome[p1.split(" (")[0]] = how
    server.diversity_mod.assess = lambda photos, log: {"verdict": "good", "unique_images": 15}
    try:
        server.run_boulder(att["1"]["project"])
    finally:
        server.diversity_mod.assess = real_assess
    a1 = {m["name"]: m["attempt"] for m in server.load_rest(bmesh)["models"]}["1"]
    assert a1["status"] == status, (how, a1)
    assert server.rest_waiting() == ([bmesh] if status == "error" else []), "an error is to try again; too few photos isn't"
outcome.clear()
summary = card()
assert (summary["whole"], summary["split"], summary["rest"]) == (False, True, "done"), "a scene made a mesh: a group"
# that mesh deleted: meshed again from the same photos (nothing fetched), and a group meanwhile
server.rest_deleted(p3["mesh_id"], key3)
a3 = {m["name"]: m["attempt"] for m in server.load_rest(bmesh)["models"]}["3"]
assert a3["status"] == "queued" and a3["project"] == server.q("SELECT id FROM projects WHERE id=?", a3["project"], one=True)["id"], a3
assert server.jobs.get_nowait() == {"type": "boulder", "pid": a3["project"]} and server.jobs.empty()
assert card()["whole"] is False
fetched_before = len(fetched)
server.diversity_mod.assess = lambda photos, log: {"verdict": "good", "unique_images": 15}
server.place_climbs = lambda mid, pid, log: (server.MESHES / mid / "views.json").write_text(json.dumps({"climbs": {ids["Arm"]: {"photos": 1, "of": 1}}}))
try:
    server.run_boulder(a3["project"])
finally:
    server.diversity_mod.assess, server.place_climbs = real_assess, real_place
again = {m["name"]: m["attempt"] for m in server.load_rest(bmesh)["models"]}["3"]
assert again["status"] == "done" and again["mesh"] != p3["mesh_id"] and len(fetched) == fetched_before, (again, "nothing fetched")
assert sorted(f.name for f in (server.PROJECTS / a3["project"] / "photos").iterdir()) == sorted(
    next(m for m in server.load_rest(bmesh)["models"] if m["name"] == "3")["photos"]), "the same photos"
p3 = server.q("SELECT * FROM projects WHERE id=?", a3["project"], one=True)     # its mesh now the new one
assert p3["mesh_id"] == again["mesh"]
# a scene whose mesh made nothing: then no scene waits or made one: one boulder
server.rest_attempt(bmesh, "3", status="failed", mesh=None, climbs=[])
assert card()["whole"] is True and card()["rest"] == "failed"
# cut short: a restart left it queued (its project interrupted), or no machine took it: due again, and the card says so
rec = server.load_rest(bmesh)
scene1 = next(m for m in rec["models"] if m["name"] == "1")
scene1["attempt"] = {**scene1["attempt"], "status": "running"}
server.save_rest(bmesh, rec)
server.q("UPDATE projects SET status='interrupted' WHERE id=?", att["1"]["project"], write=True)
assert server.rest_state(scene1["attempt"]) == "interrupted" and server.rest_waiting() == [bmesh] and card()["whole"] is False
assert server.rest_resume() == 0, "only with boulders.rest_auto"
server.settings().update({"boulders.rest_auto": True})
try:
    assert server.rest_resume() == 1 and server.jobs.get_nowait() == {"type": "boulder", "pid": att["1"]["project"], "resume": True}
finally:
    server.settings().update({"boulders.rest_auto": False})


def no_machine_for_rest(*a, **k):
    raise server.compute.ComputeUnavailable("no machine is connected")


real_reconstruct = pipeline.reconstruct
pipeline.reconstruct = no_machine_for_rest
server.diversity_mod.assess = lambda photos, log: {"verdict": "good", "unique_images": 15}
try:
    try:
        server.run_boulder(att["1"]["project"])
        raise AssertionError("waiting for a machine goes on up")
    except server.compute.ComputeUnavailable:
        pass
finally:
    pipeline.reconstruct, server.diversity_mod.assess = real_reconstruct, real_assess
a1 = {m["name"]: m["attempt"] for m in server.load_rest(bmesh)["models"]}["1"]
assert a1["status"] == "waiting" and server.q("SELECT status FROM projects WHERE id=?", att["1"]["project"], one=True)["status"] == "stopped"
assert server.rest_waiting() == [bmesh], "to queue again"
server.settings().update({"boulders.rest_auto": True})
try:
    assert server.rest_resume() == 1 and server.jobs.get_nowait()["pid"] == att["1"]["project"], "and with it, after a restart"
finally:
    server.settings().update({"boulders.rest_auto": False})
# re-meshing the boulder: the meshes of what the old mesh left out go with it, and come back with it
assert server.q("SELECT replaced_by FROM meshes WHERE id=?", p3["mesh_id"], one=True)["replaced_by"] is None
server.q("UPDATE meshes SET replaced_by=NULL WHERE id=?", p3["mesh_id"], write=True)
server.link_mesh(p3["mesh_id"], ids["Arm"], server.REST_LINK)
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo,boulder) VALUES('n3wb0uld3r00','Peabody','m.ply',?,?,1,0,?)",
         f"project:{bproj['id']}", time.time(), leaf, write=True)
server.supersede(bmesh, "n3wb0uld3r00")
assert server.q("SELECT replaced_by FROM meshes WHERE id=?", p3["mesh_id"], one=True)["replaced_by"] == "n3wb0uld3r00"
assert not server.q("SELECT 1 FROM mesh_links WHERE mesh_id=?", p3["mesh_id"]), "its climbs no longer go to it"
server.make_current(bmesh)
assert server.q("SELECT replaced_by FROM meshes WHERE id=?", p3["mesh_id"], one=True)["replaced_by"] is None
assert [r["climb_id"] for r in server.q("SELECT climb_id FROM mesh_links WHERE mesh_id=?", p3["mesh_id"])] == [ids["Arm"]]
assert server.q("SELECT replaced_by FROM meshes WHERE id='n3wb0uld3r00'", one=True)["replaced_by"] == bmesh
# a pool of climbs whose photos match never takes over a pool of what a boulder's mesh left out
assert server.pool_for(leaf, [ids["Arm"], ids["Body"]]) != key3 and server.pool_members(key3) == [ids["Arm"]]
# a climb tried in it only if a photo of its harvests was pooled into it (a harvest that found none keeps it a group)
sources = json.loads((server.PROJECTS / bproj["id"] / "sources.json").read_text())
(server.PROJECTS / bproj["id"] / "sources.json").write_text(json.dumps({"files": [f for f in sources["files"] if f["pooled_from"] != harvest["Arm"]]}))
assert server.boulder_untried(bmesh, leaf) == 1
(server.PROJECTS / bproj["id"] / "sources.json").write_text(json.dumps(sources))
# without the ties recorded, nor cameras kept, or with its photos in the backup, what it left out can't be told
(server.MESHES / bmesh / "models.json").write_text(json.dumps({"models": models["models"]}))
real_away = server.photos_away
server.photos_away = lambda pid: pid == bproj["id"]
try:
    logged = []
    assert server.keep_rest(bmesh, bproj["id"], logged.append) is None and "its photos are in the backup" in logged[-1], logged
finally:
    server.photos_away = real_away
(server.MESHES / bmesh / "cameras.json").unlink()
assert server.keep_rest(bmesh, bproj["id"], logged.append) is None and "no record of the photos it placed" in logged[-1], logged
# models.json with the ties still links climbs whose photos landed in one model together, as before
assert server.links_from_models(bmesh, bproj["id"]) >= 1
# a sector's climbs whose photos match (climb_links): meshed together as a pool once the sector's climbs are through,
# each linked to its mesh; one that matches none stays on its own; not pooled again from the same climbs
fresh()
for t in ("climb_links", "pools"):
    server.q(f"DELETE FROM {t}", write=True)
MATCH = {("Fausse", "Marie"): 3, ("Marie", "Rose"): 2, ("Lone", "Rose"): 1}
names_of = lambda: {r["id"]: r["name"] for r in server.q("SELECT id, name FROM climbs")}  # noqa: E731
real_samples, real_nearest, real_link = server.climb_samples, server.relevance_mod.nearest, server.relevance_mod.link_climbs
server.climb_samples = lambda ids: {c: [pathlib.Path(f"/x/{c}.jpg")] for c in ids if server.climb_harvests(c)}
server.relevance_mod.nearest = lambda samples, new, k, log: sorted({tuple(sorted((a, b))) for a in new for b in samples if b != a})
compared: list[tuple[str, str]] = []


def fake_links(samples, pairs, log, progress=None):
    n = names_of()
    compared.extend(pairs)
    return {(a, b): MATCH.get(tuple(sorted((n[a], n[b]))), 0) for a, b in pairs}


server.relevance_mod.link_climbs = fake_links
try:
    c = server.create_campaign("", "popularity", HEAD + "".join([leaf_row(31, "Marie", 4.0, "Bas Cuvier"), leaf_row(32, "Rose", 3.9, "Bas Cuvier"),
                                                                leaf_row(33, "Fausse", 3.8, "Bas Cuvier"), leaf_row(34, "Lone", 3.7, "Bas Cuvier")]))
    run_all(c["id"], 30)
    got = items(c["id"])
    assert all(got[n]["status"] == "done" and got[n]["mesh_id"] for n in ("Marie", "Rose", "Fausse", "Lone")), got
    assert len(set(compared)) == 6, "each pair once"
    pool = server.q("SELECT * FROM pools", one=True)
    proj = server.q("SELECT * FROM projects WHERE kind='boulder'", one=True)
    members = sorted(names_of()[m] for m in json.loads(pool["members"]))
    assert members == ["Fausse", "Marie", "Rose"] and proj["boulder"] == pool["key"] and proj["status"] == "done", (members, proj)
    mesh = server.q("SELECT * FROM meshes WHERE id=?", proj["mesh_id"], one=True)
    assert mesh["boulder"] == pool["key"] and mesh["name"].startswith("Fausse, Marie, Rose"), mesh
    linked = {names_of()[r["climb_id"]] for r in server.q("SELECT climb_id FROM mesh_links WHERE mesh_id=? AND note=?", mesh["id"], server.POOLED_LINK)}
    assert linked == {"Fausse", "Marie", "Rose"}, linked
    assert got["Marie"]["mesh_id"] != mesh["id"] and "meshed with the 2 other climbs its photos match" in got["Marie"]["note"], got["Marie"]
    assert pools[mesh["name"].split(" (")[0]] and len(pools[mesh["name"].split(" (")[0]]) == 3, pools
    assert server.campaign_status(c["id"]) == "done"
    server._settled.clear()
    assert server.settle_clusters(c["id"], got["Marie"]["boulder"]) == 0, "not again from the same climbs"
finally:
    server.climb_samples, server.relevance_mod.nearest, server.relevance_mod.link_climbs = real_samples, real_nearest, real_link
# what a pooled mesh's card says: how many of its climbs it places, and whether it's the whole boulder; a sector isn't
# meshed as one from a climb's card; names without the project's kind
summary = server.pooled_summary(pool["key"], [{"id": "a", "view": {"photos": 2}}, {"id": "b", "view": None}], server.boulder_kinds())
assert summary == {"climbs": 2, "placed": 1, "area": "Bas Cuvier", "kind": "group", "whole": False, "apart": 0, "rest": None,
                   "split": False, "untried": None, "scenes": False, "rest_of": None, "short": [], "least": 0}, summary
pea = server.q("SELECT key FROM boulders WHERE name='Peabody'", one=True)["key"]
for n, sid in (("Pea", "961"), ("Body", "962")):
    server.upsert_climb(Climb(source="mountainproject", source_id=sid, url=f"https://x/{sid}", name=n, area_path=json.loads(pea)))
on_pea = [{"id": r["id"], "view": {"photos": 3}} for r in server.q("SELECT id FROM climbs WHERE area_path=?", pea)]
assert len(on_pea) == 2 and server.pooled_summary(pea, on_pea, server.boulder_kinds())["whole"] is True, "one rock, every climb placed"
assert server.pooled_summary(pea, on_pea[:1] + [{"id": on_pea[1]["id"], "view": None}], server.boulder_kinds())["whole"] is False
body, code = server.mesh_boulder(got["Marie"]["boulder"])
assert code == 409 and "several boulders" in body["error"], body
info = server.boulder_info(server.climb_row(got["Marie"]["climb_id"]))
assert info["kind"] == "group" and "Main zone" in info["why"] and info["url"].endswith("/bas-cuvier"), info
assert server.mesh_name("Alpha (harvest)") == "Alpha" and server.mesh_name("Peabody (boulder)") == "Peabody" and server.mesh_name("x") == "x"
assert not server.q("SELECT 1 FROM meshes WHERE name LIKE '% (harvest)' OR name LIKE '% (boulder)'"), "new ones named without"
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo) VALUES('oldname00000','Old One (harvest)','m.ply','upload',1,1,0)", write=True)
server.init_db()
assert server.q("SELECT name FROM meshes WHERE id='oldname00000'", one=True)["name"] == "Old One", "and old ones, as the server starts"
# a mesh from before boulders had their own, one climb's project pooling its neighbours' harvests ("(pooled)"): a pool
# of the climbs whose photos it has, each linked to it
fresh()
for t in ("climb_links", "pools"):
    server.q(f"DELETE FROM {t}", write=True)
leafp = ["California", "Joshua Tree", "Old Boulder"]
ids = {n: server.upsert_climb(Climb(source="mountainproject", source_id=sid, url=f"https://x/{sid}", name=n, area_path=leafp))
       for n, sid in (("Old A", "901"), ("Old B", "902"), ("Old C", "903"))}
for n, pid in (("Old B", "b0b0b0b0b0b0"), ("Old C", "c0c0c0c0c0c0")):
    (server.PROJECTS / pid / "photos").mkdir(parents=True)
    server.q("INSERT INTO projects(id,name,created,kind,climb_id,status,harvest) VALUES(?,?,1,'harvest',?,'harvested','{}')",
             pid, f"{n} (harvest)", ids[n], write=True)
old_pid = "a0a0a0a0a0a0"
(server.PROJECTS / old_pid / "photos").mkdir(parents=True)
for f in ("mountainproject_a1.jpg", "yt_bbbbbbbbbbb_001.jpg", "ddg_b.jpg"):
    (server.PROJECTS / old_pid / "photos" / f).write_bytes(b"x")
(server.PROJECTS / old_pid / "sources.json").write_text(json.dumps({"files": [{"file": "mountainproject_a1.jpg"},
                                                                          {"file": "yt_bbbbbbbbbbb_001.jpg", "pooled_from": "b0b0b0b0b0b0"},
                                                                          {"file": "ddg_b.jpg", "pooled_from": "b0b0b0b0b0b0"}]}))
server.q("INSERT INTO projects(id,name,created,kind,climb_id,status) VALUES(?,?,1,'harvest',?,'done')", old_pid, "Old Boulder (pooled)",
         ids["Old A"], write=True)
server.q("INSERT INTO meshes(id,name,filename,source,created,size,demo) VALUES('0ldp00l00000','Old Boulder','m.ply',?,1,1,0)",
         f"project:{old_pid}", write=True)
server.link_mesh("0ldp00l00000", ids["Old A"], "auto-linked from reconstruction project")
server.link_mesh("0ldp00l00000", ids["Old B"], server.SHARED + "Old A")
server.link_mesh("0ldp00l00000", ids["Old C"], server.SHARED + "Old A")
assert server.adopt_old_pools(lambda s: None) == 1 and server.adopt_old_pools(lambda s: None) == 0, "once"
key = server.q("SELECT boulder FROM meshes WHERE id='0ldp00l00000'", one=True)["boulder"]
assert server.is_pool(key) and sorted(server.pool_members(key)) == sorted([ids["Old A"], ids["Old B"]]), key
notes = {r["climb_id"]: r["note"] for r in server.q("SELECT climb_id, note FROM mesh_links WHERE mesh_id='0ldp00l00000'")}
assert notes == {ids["Old A"]: "auto-linked from reconstruction project", ids["Old B"]: server.POOLED_LINK,
                 ids["Old C"]: server.SHARED + "Old A"}, notes
# climbs whose pages name each other ("left of Marie Rose") are linked; "the Arete" names nothing
server.q("DELETE FROM climb_links", write=True)
texts = {"Text A": "Just left of Text Bee on the same block.", "Text Bee": "Sit start.", "Text C": "Same holds as the Arete.", "Arete": "x"}
tid = {n: server.upsert_climb(Climb(source="mountainproject", source_id=f"97{i}", url=f"https://x/97{i}", name=n, area_path=leafp,
                                    description=d)) for i, (n, d) in enumerate(texts.items())}
assert server.links_from_text(list(tid.values())) == 1
assert server.q("SELECT a, b, how FROM climb_links", one=True) == {**dict(zip("ab", sorted((tid["Text A"], tid["Text Bee"])))), "how": "text"}
# boulders' meshes made before what they left out was kept: kept as the server starts, not queued (the admin page does)
fresh()
c = server.create_campaign("", "popularity", HEAD + "".join([leaf_row(41, "Pea", 3.8, "Peabody"), leaf_row(42, "Body", 3.7, "Peabody")]))
server.settings().update({"areas.meshes": "boulders"})
server.climbs_mod.fetch_climb_url = with_areas
run_all(c["id"])
got = items(c["id"])
assert not (server.MESHES / got["Pea"]["mesh_id"] / server.REST_FILE).exists(), "no cameras kept: nothing to tell"
(server.MESHES / got["Pea"]["mesh_id"] / "cameras.json").write_text("[]")
server.rest_existing_meshes()
assert server.load_rest(got["Pea"]["mesh_id"])["attempt"] is None and server.jobs.empty()
# made before ties were recorded: the admin page meshes it again, from its photos as harvested, after reading its area's
# kind where never read; one with them recorded isn't
assert [m["id"] for m in server.untied_meshes()] == [got["Pea"]["mesh_id"]]
kind_read = []
real_kind, real_leaf = server.boulder_kind, server.leaf_kind
server.leaf_kind = lambda key, kinds=None: None
server.boulder_kind = lambda boulder, url, log=print: kind_read.append((boulder, url)) or "boulder"
try:
    assert server.remesh_untied() == {"queued": 1, "groups": 0, "busy": 0, "unread": 0}
finally:
    server.boulder_kind, server.leaf_kind = real_kind, real_leaf
assert len(kind_read) == 1 and kind_read[0][0] == got["Pea"]["boulder"], kind_read
job = server.jobs.get_nowait()
assert job["type"] == "boulder" and server.q("SELECT boulder FROM projects WHERE id=?", job["pid"], one=True)["boulder"] == got["Pea"]["boulder"]
assert server.remesh_untied()["busy"] == 1, "not twice while it's queued"
# one whose page, read, doesn't say it's one rock: not meshed whole again (its climbs are pooled by their photos)
server.q("UPDATE projects SET status='done' WHERE id=?", job["pid"], write=True)
read_as = {"kind": None}
server.leaf_kind = lambda key, kinds=None: read_as["kind"]
server.boulder_kind = lambda boulder, url, log=print: read_as.update(kind="unknown") or "unknown"
real_mesh = server.mesh_boulder
server.mesh_boulder = lambda b: (_ for _ in ()).throw(AssertionError("meshed whole"))
try:
    got_unclear = server.remesh_untied()
finally:
    server.boulder_kind, server.leaf_kind, server.mesh_boulder = real_kind, real_leaf, real_mesh
assert got_unclear == {"queued": 0, "groups": 1, "busy": 0, "unread": 0} and server.jobs.empty(), got_unclear
server.q("INSERT OR REPLACE INTO boulders(key, name, kind, why, checked) VALUES(?,?,?,?,?)", got["Pea"]["boulder"], "Peabody", "unknown", "test", 1,
         write=True)
assert server.untied_meshes() == [], "and not counted as one to mesh again"
server.q("UPDATE boulders SET kind='boulder' WHERE key=?", got["Pea"]["boulder"], write=True)
server.q("UPDATE projects SET status='done' WHERE id=?", job["pid"], write=True)
server.save_rest(got["Pea"]["mesh_id"], {**server.load_rest(got["Pea"]["mesh_id"]), "models": []})
assert server.untied_meshes() == [] and server.jobs.empty()
server.climbs_mod.fetch_climb_url = fake_fetch
server.settings().update({"areas.meshes": "climbs"})
print("boulders told from groups by their pages OK")

# --- your phone told the server is back up after a restart, and of the scripts being downloaded, roughly from where ----
fresh()
began = time.time() - 12
server.STOPPED_AT.write_text(f"{began - 125}\n")
title, text = server.back_up_note(began)
assert title == "BoulderMesh is back up" and ", up in 12 s, after 2 min down." in text and not server.STOPPED_AT.exists(), text
assert "without saying so" in server.back_up_note(time.time())[1], "no stop recorded: a crash or a reboot"
told, real_send = [], server.NOTIFIER.send
server.NOTIFIER.send = lambda title, message, actions=None, priority=3, tags=None: told.append((title, message, actions)) or True
server.DOWNLOADS.last, server.DOWNLOADS.gap = 0.0, 0.3
server._fetched.clear()
try:
    got = server.script_downloaded("volunteer")
    where = {"cf-ipcity": "Pasadena", "cf-region": "California", "cf-ipcountry": "US", "cf-iplatitude": "34.1", "cf-iplongitude": "-118.1"}
    got({"ip": "203.0.113.60", "ua": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36", "headers": where})
    got({"ip": "203.0.113.60", "ua": "curl/8.5.0", "headers": where})                 # the same address again today
    got({"ip": "203.0.113.61", "ua": "bouldermesh-volunteer/8", "headers": {}})         # a running copy updating itself
    got({"ip": "203.0.113.62", "ua": "Googlebot/2.1", "headers": {}})
    server.script_downloaded("worker")({"ip": "203.0.113.63", "ua": "curl/8.5.0", "headers": {"cf-ipcountry": "FR"}})
    for _ in range(50):
        if len(told) >= 2:
            break
        time.sleep(0.05)
    assert told[0] == ("Someone downloaded the volunteer script", "From Pasadena, California, US, with Chrome on Windows.",
                       [{"action": "view", "label": "Map", "url": "https://www.google.com/maps/search/?api=1&query=34.1000%2C-118.1000"}]), told
    time.sleep(0.5)
    assert told[1] == ("Someone downloaded the worker script", "From FR, with curl 8.5.0.", None) and len(told) == 2, told
    kept = [json.loads(x) for x in server.SCRIPT_DOWNLOADS.read_text().splitlines()]
    assert [(k["script"], k["ip"]) for k in kept] == [("volunteer", "203.0.113.60"), ("volunteer", "203.0.113.60"), ("worker", "203.0.113.63")], kept
    assert kept[0]["place"] == "Pasadena, California, US" and kept[0]["map"].endswith("34.1000%2C-118.1000")
    server.settings().update({"notify.downloads": False})
    server._fetched.clear()
    server.script_downloaded("worker")({"ip": "203.0.113.64", "ua": "curl/8.5.0", "headers": {}})
    time.sleep(0.5)
    assert len(told) == 2 and len(server.SCRIPT_DOWNLOADS.read_text().splitlines()) == 4, "off: kept, not told"
finally:
    server.NOTIFIER.send = real_send
    server.settings().update({"notify.downloads": True})
print("back up, and scripts downloaded OK")

# --- the public leaderboard: volunteers by what they fetched; machines by the meshes their timings say they made, beta
# and sizes found, and their processor and GPU time; names shown only as leaderboard.names allows
fresh()
vol = server.VOLUNTEERS.add("Jane")
server.VOLUNTEERS.q("INSERT INTO tasks(id, kind, video_id, state, volunteer, size, finished) VALUES('lbtask000001','video','lbvideo0001','done',?,?,?)",
                    vol["id"], 2**30, time.time())
box = server.MACHINES.add("Jane's PC")
server.MACHINES.q("INSERT INTO machine_jobs(id, task, machine_id, machine, created, taken, ended, outcome) VALUES('lbjob0000001','beta',?,?,1,2,3,'done')",
                  box["id"], "Jane's PC")
(server.MESHES / "lbmesh000001").mkdir(parents=True, exist_ok=True)
(server.MESHES / "lbmesh000001" / "timings.json").write_text(json.dumps({
    "where": "Jane's PC", "remote": True, "gpu": True, "started": 1794400000.0, "cpu_seconds": 7200,
    "tools": [{"step": "COLMAP: matching", "seconds": 1800, "cpu": 100}, {"step": "OpenMVS: densify", "seconds": 1800, "cpu": 100},
              {"step": "COLMAP: sparse reconstruction (mapper)", "seconds": 3600, "cpu": 7000}]}))
try:
    for mode, name in (("all", "Jane"), ("none", "anonymous volunteer 1")):
        server.settings().update({"leaderboard.names": mode})
        b = server.leaderboard()
        v = next(x for x in b["volunteers"] if x["videos"] == 1)
        assert v["name"] == name and v["gb"] == 1.0, b["volunteers"]
    server.settings().update({"leaderboard.names": "new"})
    b = server.leaderboard()
    w = b["workers"][0]
    assert w["name"] == ("Jane's PC" if box.get("created", time.time()) >= server.NAMES_SHOWN_SINCE else "anonymous computer 1"), w
    assert (w["meshes"], w["beta"], w["sizes"], w["cpu_hours"], w["gpu_hours"]) == (1, 1, 0, 2.0, 1.0), w
    assert b["since"] == 1794400000.0 and b["jobs_since"] == 1, b
    # GPU use measured by the script (from version 10), in 3090 hours by the GPU's speed; the steps' estimate only before
    assert abs(server.gpu_factor("NVIDIA GeForce RTX 4090") - 82.6 / 35.6) < 1e-9 and server.gpu_factor("NVIDIA GeForce RTX 3090 Ti") == 40.0 / 35.6
    assert server.gpu_factor("NVIDIA GeForce RTX 3060 Laptop GPU") == 12.7 / 35.6 * 0.6 and server.gpu_factor("Some GPU") is None
    assert server.gpu_factor("Tesla T4") == 8.1 / 35.6 and server.gpu_factor("NVIDIA L40S") == 91.6 / 35.6
    server.MACHINES.q("UPDATE machines SET gpu_name='NVIDIA GeForce RTX 4090' WHERE id=?", box["id"])
    server.MACHINES.q("INSERT INTO machine_jobs(id, task, machine_id, machine, created, taken, ended, outcome, gpu_busy, gpu_name) "
                      "VALUES('lbjob0000002','reconstruct',?,?,1794500000,1794500000,1794503600,'done',1800,'NVIDIA GeForce RTX 4090')", box["id"], "Jane's PC")
    w = server.leaderboard()["workers"][0]
    assert abs(w["gpu_hours"] - round((1.0 + 0.5) * 82.6 / 35.6, 2)) < 0.011 and w["gpu"] == "NVIDIA GeForce RTX 4090" and w["gpu_rough"], w
    server.MACHINES.q("UPDATE machine_jobs SET taken=1 WHERE id='lbjob0000002'")      # measured from before that mesh: no estimate
    w = server.leaderboard()["workers"][0]
    assert abs(w["gpu_hours"] - round(0.5 * 82.6 / 35.6, 2)) < 0.011 and not w["gpu_rough"], w
finally:
    server.settings().update({"leaderboard.names": "new"})
    server.VOLUNTEERS.q("DELETE FROM tasks WHERE id='lbtask000001'")
    server.MACHINES.q("DELETE FROM machine_jobs WHERE id IN ('lbjob0000001', 'lbjob0000002')")
print("leaderboard OK")
print("ALL CAMPAIGN TESTS PASSED")
