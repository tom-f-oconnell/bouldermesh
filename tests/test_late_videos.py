#!/usr/bin/env python3
"""Videos that come after a boulder's or pool's photos were pooled: a boulder's mesh waits for the videos of climbs it
harvests itself; what comes for a climb its area hasn't reached is added to its harvest; a pool is meshed again with what
came for one of its climbs, as a boulder is; and what comes late for a boulder waits for the rest, however
long, so that it's meshed again once. Offline: harvests, meshing and the volunteers' broker are stand-ins.

    python3 tests/test_late_videos.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = tempfile.mkdtemp(prefix="bouldermesh-late-")
os.environ["DATA_DIR"] = tmp

import server  # noqa: E402

try:
    server.init_db()
    now = time.time()
    assert server.VOLUNTEERS.unsettled("harvest", 0) == [], "the broker's own, on an empty database"
    tasks: dict[str, list[dict]] = {}       # harvest project -> what volunteers still have to fetch for it
    server.VOLUNTEERS.pending_tasks = lambda purpose, refs: [t for r in refs for t in tasks.get(r, [])]
    server.VOLUNTEERS.pending = lambda purpose, ref: len(tasks.get(ref, []))
    server.VOLUNTEERS.unsettled = lambda purpose, since: []
    queued: list[dict] = []
    server.queue_job = lambda job: queued.append(job)
    LEAF = json.dumps(["CA", "JT", "Rock Boulder"])
    server.q("INSERT INTO boulders(key, name, kind) VALUES(?, 'Rock Boulder', 'boulder')", LEAF, write=True)
    server._kinds_gen += 1

    def climb(cid: str, leaf: str = LEAF) -> None:
        server.q("INSERT INTO climbs(id,source,source_id,name,area_path,data) VALUES(?,?,?,?,?,?)", cid, "mountainproject", cid, cid, leaf,
                 json.dumps({"source": "mountainproject", "source_id": cid, "url": "", "name": cid, "kind": "boulder"}), write=True)

    def harvest(pid: str, cid: str, status: str = "harvested", photos: int = 3) -> None:
        server.q("INSERT INTO projects(id,name,created,status,kind,climb_id,harvest) VALUES(?,?,?,?,?,?,?)", pid, f"{cid} (harvest)", now,
                 status, "harvest", cid, json.dumps({"images": photos, "diversity": {"verdict": "good"}}), write=True)
        (server.PROJECTS / pid / "photos").mkdir(parents=True, exist_ok=True)
        for i in range(photos):
            (server.PROJECTS / pid / "photos" / f"{pid}-{i}.jpg").write_bytes(b"x")

    def pooled(key: str, climbs: dict[str, list[str]], at: float, status: str = "done") -> str:
        pid = server.boulder_project_made(key)     # its one project, pooled from these at that time
        server.q("UPDATE projects SET status=?, mesh_id=? WHERE id=?", status, "m-" + pid, pid, write=True)
        (server.PROJECTS / pid).mkdir(parents=True, exist_ok=True)
        (server.PROJECTS / pid / server.CLIMBS_FILE).write_text(json.dumps(
            {"boulder": key, "created": at, "climbs": [{"id": c, "name": c, "projects": hs} for c, hs in climbs.items()]}))
        return pid

    # ---- a boulder's mesh harvests a climb its page lists that no one has: it waits for that climb's videos
    for c in ("c1", "c2"):
        climb(c)
    harvest("h1", "c1")
    server.adopt_page_climbs = lambda boulder, log: None
    server.harvest_mod.harvest = lambda *a, **k: {"images": 6, "diversity": {"verdict": "good"}, "queries": []}
    reconstructed = []
    server.run_reconstruct = lambda pid, **k: (reconstructed.append(pid), server.q(
        "UPDATE projects SET status='done', mesh_id=? WHERE id=?", "m-" + pid, pid, write=True))

    def harvest_climb(climb_id, name, log, follow=None):     # what it asks volunteers for comes after
        harvest("h2", climb_id)
        tasks["h2"] = [{"moved": time.time()}]
        return "h2"
    server.harvest_climb = harvest_climb
    bpid = server.boulder_project_made(LEAF)
    server.run_boulder(bpid, wait=True)
    assert server.q("SELECT status FROM projects WHERE id=?", bpid, one=True)["status"] == "waiting" and not reconstructed
    assert not (server.PROJECTS / bpid / server.CLIMBS_FILE).exists(), "not pooled yet"
    tasks["h2"] = []
    server.q("UPDATE projects SET status='harvesting' WHERE id='h2'", write=True)
    server.run_boulder(bpid, wait=True)
    assert server.q("SELECT status FROM projects WHERE id=?", bpid, one=True)["status"] == "waiting", "nor while it's harvested again"
    server.q("UPDATE projects SET status='harvested' WHERE id='h2'", write=True)
    server.run_boulder(bpid, wait=True)
    assert reconstructed == [bpid], "pooled and meshed once they've come"
    record = json.loads((server.PROJECTS / bpid / server.CLIMBS_FILE).read_text())
    assert {c["id"]: c["projects"] for c in record["climbs"]} == {"c1": ["h1"], "c2": ["h2"]}, record
    print("a boulder's mesh waits for the climbs it harvested OK")

    # ---- as an area's: its climb carrying it waits harvested with the rest, not done or failed with it
    server.q("INSERT INTO campaigns(id,name,created,status) VALUES('a','Area',?,'done')", now, write=True)
    for sid, cid, pid, status in (("1", "c1", "h1", "harvested"), ("3", "c3", None, "harvested")):
        if cid == "c3":
            climb("c3")
            harvest("h3", "c3")
            pid = "h3"
        server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,climb_id,project_id,status,boulder) VALUES('a',?,?,?,?,?,?,?)",
                 sid, int(sid), cid, cid, pid, status, LEAF, write=True)
    server.q("UPDATE projects SET status='done' WHERE id=?", bpid, write=True)
    (server.PROJECTS / "h3" / "photos" / "new.jpg").write_bytes(b"y")     # harvested since: not unchanged
    tasks["h2"] = [{"moved": time.time()}]
    server.set_item("a", "1", status="pending", retry=1, redo="boulder", was="harvested", redo_started=0)
    camp = server.q("SELECT * FROM campaigns WHERE id='a'", one=True)
    row = server.q("SELECT * FROM campaign_items WHERE campaign_id='a' AND source_id='1'", one=True)
    server.run_redo(camp, row)
    got = {r["source_id"]: r for r in server.q("SELECT * FROM campaign_items WHERE campaign_id='a'")}
    assert got["1"]["status"] == "harvested" and got["1"]["redo"] is None and "waits for the videos" in got["1"]["note"], got["1"]
    assert got["3"]["status"] == "harvested", "its neighbour isn't failed with it"
    assert server.settle_boulder("a", LEAF) is False and server.q("SELECT 1 FROM video_holds"), "held while they come"
    tasks["h2"] = []
    assert server.settle_boulder("a", LEAF) is True
    assert server.q("SELECT status FROM campaigns WHERE id='a'", one=True)["status"] == "running", "a finished area goes on for it"
    print("an area's boulder waiting OK")

    # ---- a harvest being added to holds a boulder quietly (the phone isn't told)
    server.q("DELETE FROM video_holds", write=True)
    server.q("UPDATE campaign_items SET status='harvested', redo=NULL WHERE campaign_id='a'", write=True)
    server.q("UPDATE projects SET status='queued', job=? WHERE id='h2'", json.dumps({"type": "harvest", "pid": "h2"}), write=True)
    assert server.settle_boulder("a", LEAF) is False
    held = server.q("SELECT * FROM video_holds", one=True)
    assert held and held["notified"], held
    server.q("UPDATE projects SET status='queued', job=? WHERE id='h2'", json.dumps({"type": "reconstruct", "pid": "h2"}), write=True)
    assert server.harvests_busy(["c2"]) == [], "meshing a climb on its own isn't harvesting it"
    server.q("UPDATE projects SET status='harvested', job=NULL WHERE id='h2'", write=True)
    print("held quietly for a harvest OK")

    # ---- what volunteers fetch for a climb its area hasn't reached, harvested by its boulder's mesh: added to its photos
    server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,climb_id,project_id,status,boulder) "
             "VALUES('a','2',2,'c2','c2','h2','pending',?)", LEAF, write=True)
    queued.clear()
    assert server.reharvest_with_videos("h2") is True
    assert len(queued) == 1 and queued[0]["pid"] == "h2" and queued[0]["add"] and queued[0]["late"], queued
    assert server.q("SELECT status FROM campaign_items WHERE source_id='2'", one=True)["status"] == "pending", "left to its area"
    server.q("UPDATE projects SET status='harvested' WHERE id='h2'", write=True)
    queued.clear()
    server.set_item("a", "2", redo="new+rebuild")
    assert server.reharvest_with_videos("h2") is True and not queued, "its queued harvest adds them anyway"
    server.set_item("a", "2", redo=None, status="running")
    assert server.reharvest_with_videos("h2") is False, "not now: its area is at work on it"
    server.set_item("a", "2", status="pending")
    print("a climb not reached yet OK")

    # ---- that harvest job notes the boulder's mesh as to be made again, if it added photos
    pooled(LEAF, {"c1": ["h1"], "c2": ["h2"], "c3": ["h3"]}, now - 3600)
    real_harvest = server.run_harvest
    server.run_harvest = lambda pid, *a, **k: (server.PROJECTS / pid / "photos" / "frame-late.jpg").write_bytes(b"z")
    try:
        server._run_job({"type": "harvest", "pid": "h2", "sources": [], "reconstruct": False, "again": True, "add": True, "late": True}, False)
    finally:
        server.run_harvest = real_harvest
    late = server.q("SELECT * FROM late_photos WHERE key=?", LEAF, one=True)
    assert late and late["first"] == late["last"], late
    print("noting late photos OK")

    # ---- late photos wait for the rest still to come, however long, then the boulder is meshed again once
    server.q("UPDATE campaign_items SET status='done', redo=NULL WHERE campaign_id='a'", write=True)
    server.q("UPDATE campaigns SET status='done' WHERE id='a'", write=True)
    tasks["h3"] = [{"moved": now - 20 * 3600}]      # nothing heard of it for 20 h: still to come
    assert server.settle_late() == 0, "waits for the rest"
    server.q("UPDATE late_photos SET first=?", now - 100 * 3600, write=True)
    assert server.settle_late() == 0, "however long"
    tasks["h3"] = []
    assert server.settle_late() == 1, "once it's come"
    row = server.q("SELECT * FROM campaign_items WHERE campaign_id='a' AND source_id='1'", one=True)
    assert (row["status"], row["redo"], row["was"]) == ("pending", "boulder", "done"), row
    assert server.q("SELECT status FROM campaigns WHERE id='a'", one=True)["status"] == "running"
    assert not server.q("SELECT 1 FROM late_photos"), "queued: forgotten"
    assert server.settle_late() == 0, "once"
    print("late photos batched OK")

    # ---- an area's climb on the boulder redone for the same: its late photos batched in settle_boulder() too
    server.q("UPDATE campaign_items SET status='harvested', redo=NULL WHERE campaign_id='a'", write=True)
    server.q("DELETE FROM video_holds", write=True)
    server.note_late("c3")
    tasks["h3"] = [{"moved": now - 20 * 3600}]
    assert server.settle_boulder("a", LEAF) is False, "the rest still to come for h3"
    tasks["h3"] = []
    assert server.settle_boulder("a", LEAF) is True
    pooled(LEAF, {"c1": ["h1"], "c2": ["h2"], "c3": ["h3"]}, time.time() + 1)
    assert server._settle_late(LEAF) == 0 and not server.q("SELECT 1 FROM late_photos"), "pooled since: forgotten"
    print("late photos in an area OK")

    # ---- a pool: meshed again on one of its climbs, as the area would
    GROUP = json.dumps(["CA", "JT", "Manx Boulders"])
    server.q("INSERT INTO boulders(key, name, kind) VALUES(?, 'Manx Boulders', 'unknown')", GROUP, write=True)
    server._kinds_gen += 1
    for cid, pid in (("p1", "hp1"), ("p2", "hp2")):
        climb(cid, GROUP)
        harvest(pid, cid)
    key = server.pool_for(GROUP, ["p1", "p2"])
    pooled(key, {"p1": ["hp1"], "p2": ["hp2"]}, now - 3600)
    server.q("INSERT INTO campaigns(id,name,created,status) VALUES('g','Group area',?,'running')", now, write=True)
    for sid, cid, pid in (("11", "p1", "hp1"), ("12", "p2", "hp2")):
        server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,climb_id,project_id,status,boulder) "
                 "VALUES('g',?,?,?,?,?,'done',?)", sid, int(sid), cid, cid, pid, GROUP, write=True)
    assert server.note_late("p2") == [key], "its pool, not a whole boulder"
    assert server.settle_late() == 1
    row = server.q("SELECT * FROM campaign_items WHERE campaign_id='g' AND source_id='11'", one=True)
    assert (row["status"], row["redo"], row["pool"]) == ("pending", "cluster", key), row
    print("a pool made again OK")

    # ---- a boulder no area lists: by a job, which waits too
    LONE = json.dumps(["CA", "JT", "Lone Boulder"])
    server.q("INSERT INTO boulders(key, name, kind) VALUES(?, 'Lone Boulder', 'boulder')", LONE, write=True)
    server._kinds_gen += 1
    climb("l1", LONE)
    harvest("hl1", "l1")
    pooled(LONE, {"l1": ["hl1"]}, now - 3600)
    server.note_late("l1")
    queued.clear()
    assert server.settle_late() == 1
    assert queued and queued[-1]["type"] == "boulder" and queued[-1]["wait"], queued
    print("a boulder outside areas OK")

    # ---- what came for a harvest busy then is given again later
    given = []
    server.VOLUNTEERS.unsettled = lambda purpose, since: ["hx"]
    server.VOLUNTEERS.resettle = lambda purpose, ref: given.append(ref) or True
    assert server.resettle_harvests() == 1 and given == ["hx"]
    print("given again OK")
    print("ALL LATE VIDEO TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
