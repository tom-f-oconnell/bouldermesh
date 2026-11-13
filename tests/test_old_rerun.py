#!/usr/bin/env python3
"""Meshes made under older rules (MESH_RULES), and those made since from less: areas' climbs whose only meshes are old,
or that are linked only via another climb's mesh, queued again as their areas run, after the rest of their lists, and
an area's first N counting them as still to make; only meshes under older rules replaced by what's made again. Offline:
the harvest and reconstruction are stubbed, as in test_campaign.py.

    python3 tests/test_old_rerun.py
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
tmp = tempfile.mkdtemp(prefix="bouldermesh-oldrerun-")
os.environ["DATA_DIR"] = tmp

import pipeline  # noqa: E402
import server  # noqa: E402
from climbs.common import Climb, Media  # noqa: E402

HEAD = 'Route,Location,URL,"Avg Stars","Your Stars","Route Type",Rating,Pitches,Length,"Area Latitude","Area Longitude"\n'


def row(rid: int, name: str, boulder: str) -> str:
    return (f'{name},"{boulder} > Sector > * JT Bouldering > Joshua Tree National Park > California",'
            f'https://www.mountainproject.com/route/{rid}/{name.lower()},3.0,-1,Boulder,V{rid},,,34.0,-116.0\n')


outcome: dict[str, str] = {}      # climb or project name -> "boom": its reconstruction fails
harvested: list[tuple[str, bool, bool]] = []     # (climb, add, refresh), in the order harvested


def fake_fetch(url, include_children=False):
    rid = url.split("/")[-2]
    item = server.q("SELECT data FROM campaign_items WHERE source_id=? LIMIT 1", rid, one=True)
    data = json.loads(item["data"]) if item else {}
    return [Climb(source="mountainproject", source_id=rid, url=url, name=url.split("/")[-1].capitalize(), kind="boulder", lat=34.0,
                  lng=-116.0, area_path=data.get("area_path") or [], boulder_name=data.get("boulder_name"),
                  images=[Media(url=f"http://x/{i}.jpg", kind="image") for i in range(3)])]


def fake_harvest(c, project_dir, log, sources, relevance_filter=True, tracker=None, pool_from=None, anchors_from=None, want=None,
                 want_search=None, add=False, refresh=False, rejudge=False):
    harvested.append((c.name, add, refresh))
    (project_dir / "photos").mkdir(parents=True, exist_ok=True)
    (project_dir / "photos" / f"{c.name}.jpg").write_bytes(b"jpg")
    (project_dir / "sources.json").write_text(json.dumps({"files": []}))    # so that a harvest again adds to it
    return {"images": 20, "diversity": {"verdict": "good"}, "video_frames": 0, "queries": []}


def fake_reconstruct(images, work, out_dir, log, use_gpu=False, internet=False, tracker=None):
    name = server.q("SELECT name FROM projects WHERE id=?", pathlib.Path(images).parent.name, one=True)["name"]
    if outcome.get(name.split(" (")[0]) == "boom":
        raise RuntimeError("OpenMVS crashed")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mesh.ply").write_text("ply")
    return out_dir / "mesh.ply", False


def fake_place(mid, pid, log):     # each climb pooled is placed on its boulder's mesh
    climbs = json.loads((server.PROJECTS / pid / server.CLIMBS_FILE).read_text())["climbs"]
    (server.MESHES / mid / "views.json").write_text(json.dumps({"climbs": {c["id"]: {"photos": 1, "of": 1} for c in climbs}}))


server.climbs_mod.fetch_climb_url = fake_fetch
server.harvest_mod.harvest = fake_harvest
pipeline.reconstruct = fake_reconstruct
server.pipeline.steps = lambda: []
server.place_climbs = fake_place
server.CAMPAIGN_MIN_FREE = 0
server.init_db()
server.settings().update({"areas.meshes": "climbs"})


def run_all(camp_id: str, limit: int = 30) -> None:
    for _ in range(limit):
        if server.campaign_status(camp_id) != "running":
            return
        server.next_campaign_step()


def items(camp_id: str) -> dict[str, dict]:
    return {i["name"]: i for i in server.q("SELECT * FROM campaign_items WHERE campaign_id=?", camp_id)}


def mesh(mid: str) -> dict:
    return server.q("SELECT * FROM meshes WHERE id=?", mid, one=True)


def make_old(*mids: str) -> None:
    """As if made, and their climbs finished in their areas, before the rules last changed."""
    before = server.MESH_RULES_SINCE[server.MESH_RULES] - 3600
    for mid in mids:
        server.q("UPDATE meshes SET rules=0, created=? WHERE id=?", before, mid, write=True)
        server.q("UPDATE campaign_items SET updated=? WHERE mesh_id=?", before, mid, write=True)
        server.q("UPDATE projects SET progress=NULL WHERE mesh_id=?", mid, write=True)
    server.old_changed()


def fresh() -> None:
    for t in ("campaigns", "campaign_items", "projects", "meshes", "mesh_links", "climbs", "boulders", "pools", "old_decisions"):
        server.q(f"DELETE FROM {t}", write=True)
    outcome.clear()
    harvested.clear()
    server.old_changed()


def start(camp_id: str) -> None:
    server.q("UPDATE campaigns SET status='running', only_retries=0, note=NULL WHERE id=?", camp_id, write=True)


try:
    # --- climbs whose only meshes are old: queued again as "new+rebuild" when their area runs, after what's queued, in
    # its order; made again whatever the harvest finds; the old mesh an older version of the new one -----------------
    fresh()
    csv = HEAD + row(1, "Beta", "Beta Boulder") + row(2, "Gamma", "Gamma Boulder") + row(3, "Delta", "Delta Boulder")
    c = server.create_campaign("", "popularity", csv)
    run_all(c["id"])
    got = items(c["id"])
    assert {n: i["status"] for n, i in got.items()} == {"Beta": "done", "Gamma": "done", "Delta": "done"}, got
    beta0, gamma0, delta0 = got["Beta"]["mesh_id"], got["Gamma"]["mesh_id"], got["Delta"]["mesh_id"]
    make_old(beta0, gamma0)
    # Delta's as if never reached: still to go when the area runs again
    server.q("DELETE FROM mesh_links WHERE mesh_id=?", delta0, write=True)
    server.q("DELETE FROM meshes WHERE id=?", delta0, write=True)
    server.q("UPDATE campaign_items SET status='pending', mesh_id=NULL, project_id=NULL WHERE name='Delta'", write=True)
    assert server.old_mesh_report(fresh=True)["waiting"] == 2
    # a stopped area stays as it is
    server.q("UPDATE campaigns SET status='stopped' WHERE id=?", c["id"], write=True)
    assert server.queue_old(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True)) == 0
    assert not server.next_campaign_step() and items(c["id"])["Beta"]["redo"] is None, "not started"
    start(c["id"])
    harvested.clear()
    assert server.queue_old(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True)) == 2
    got = items(c["id"])
    assert (got["Beta"]["redo"], got["Beta"]["retry"], got["Beta"]["old_rerun"], got["Beta"]["rerun_rules"]) == ("new+rebuild", 0, 1, 1), got["Beta"]
    order = [i["name"] for i in server.ordered_pending(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))]
    assert order == ["Delta", "Beta", "Gamma"], order
    camp = server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))
    assert camp["old_queued"] == 2, camp
    run_all(c["id"])
    assert harvested == [("Delta", False, False), ("Beta", True, True), ("Gamma", True, True)], harvested
    got = items(c["id"])
    for n, was in (("Beta", beta0), ("Gamma", gamma0)):
        new = got[n]["mesh_id"]
        assert got[n]["status"] == "done" and new != was and not got[n]["old_rerun"] and got[n]["rerun_rules"] == server.MESH_RULES, got[n]
        assert mesh(new)["rules"] == server.MESH_RULES and mesh(was)["replaced_by"] == new, (mesh(new), mesh(was))
    r = server.old_mesh_report(fresh=True)
    assert (r["old_meshes"], r["climbs"], r["waiting"], r["missed"]) == (0, 0, 0, []), r     # none old left to follow
    assert server.campaign_status(c["id"]) == "done"
    # once tried under the rules, not again: one whose meshing fails keeps its old mesh, and is listed as missed
    make_old(got["Gamma"]["mesh_id"])
    server.q("UPDATE campaign_items SET rerun_rules=NULL, updated=0 WHERE name='Gamma'", write=True)
    outcome["Gamma"] = "boom"
    start(c["id"])
    run_all(c["id"])
    got = items(c["id"])
    assert got["Gamma"]["status"] == "done" and "rebuilding failed" in got["Gamma"]["note"] and mesh(got["Gamma"]["mesh_id"])["rules"] == 0
    r = server.old_mesh_report(fresh=True)
    assert [m["name"] for m in r["missed"]] == ["Gamma"] and r["waiting"] == 0, r
    start(c["id"])
    harvested.clear()
    run_all(c["id"])
    assert harvested == [] and server.campaign_status(c["id"]) == "done", "not queued again"
    # an old one interrupted by a restart goes next when the server's back
    server.q("UPDATE campaign_items SET status='running', old_rerun=1, retry=0 WHERE name='Gamma'", write=True)
    server.init_db()
    assert server.q("SELECT retry, status FROM campaign_items WHERE name='Gamma'", one=True) == {"retry": 1, "status": "pending"}
    print("old climbs queued again OK")

    # --- a climb linked only via another climb's mesh, never harvested itself: harvested and meshed as if new ------
    fresh()
    c = server.create_campaign("", "popularity", HEAD + row(1, "Beta", "Beta Boulder") + row(2, "Gamma", "Beta Boulder"))
    server.q("UPDATE campaigns SET status='stopped' WHERE id=?", c["id"], write=True)
    beta = server.upsert_climb(fake_fetch("https://www.mountainproject.com/route/1/beta")[0])
    gamma = server.upsert_climb(fake_fetch("https://www.mountainproject.com/route/2/gamma")[0])
    server.q("INSERT INTO meshes(id,name,filename,source,created,rules) VALUES('bm','Beta','m.ply','project:pp',?,1)", time.time(), write=True)
    server.link_mesh("bm", beta, "auto-linked from reconstruction project")
    server.link_mesh("bm", gamma, server.SHARED + "Beta")
    before = server.MESH_RULES_SINCE[server.MESH_RULES] - 3600   # an area linked it so long before
    server.q("UPDATE campaign_items SET status='done', mesh_id='bm', climb_id=?, note='already had a mesh', updated=? WHERE name='Beta'",
             beta, before, write=True)
    server.q("UPDATE campaign_items SET status='done', mesh_id='bm', climb_id=?, note=?, updated=? WHERE name='Gamma'",
             gamma, server.SHARED + "Beta", before, write=True)
    server.old_changed()
    assert server.old_waiting(fresh=True) == {gamma}, "Beta's mesh is new: only Gamma, via it"
    start(c["id"])
    run_all(c["id"])
    got = items(c["id"])
    assert harvested == [("Gamma", False, False)] and got["Gamma"]["status"] == "done" and got["Gamma"]["mesh_id"] != "bm", (harvested, got["Gamma"])
    assert got["Gamma"]["project_id"] and server.climb_row(gamma)["n_links"] == 1 and mesh("bm")["replaced_by"] is None
    r = server.old_mesh_report(fresh=True)
    assert (r["via"], r["waiting"], r["climbs"]) == (0, 0, 0), r
    print("climbs linked only via another's mesh OK")

    # --- the first N meshes of an area: one done whose only mesh is old may yet make one, rather than having made one;
    # it's made again, and the area doesn't go on to the next ------------------------------------------------------
    fresh()
    c = server.create_campaign("", "popularity", HEAD + row(1, "Beta", "Beta Boulder") + row(2, "Gamma", "Gamma Boulder")
                               + row(3, "Delta", "Delta Boulder"), max_meshes=1)
    run_all(c["id"])
    got = items(c["id"])
    assert [n for n, i in got.items() if i["status"] == "done"] == ["Beta"] and server.campaign_status(c["id"]) == "done"
    camp = lambda: server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True)  # noqa: E731
    assert server.limit_scope(camp()) == ({"1"}, 1)
    make_old(got["Beta"]["mesh_id"])
    assert server.limit_scope(camp()) == ({"1"}, 0), "within it still, but no mesh made"
    start(c["id"])
    harvested.clear()
    run_all(c["id"])
    got = items(c["id"])
    assert harvested == [("Beta", True, True)] and got["Gamma"]["status"] == got["Delta"]["status"] == "pending", (harvested, got)
    assert server.limit_scope(camp()) == ({"1"}, 1) and server.campaign_status(c["id"]) == "done"
    print("the first N with old meshes OK")

    # --- a boulder meshed whole: its climbs harvested again and the boulder meshed again, the old boulder's mesh an
    # older version of the new one; a climb's own mesh under older rules that the new one covers too is replaced, but
    # not one under the same rules ---------------------------------------------------------------------------------
    fresh()
    server.settings().update({"areas.meshes": "boulders"})
    c = server.create_campaign("", "popularity", HEAD + row(1, "Alpha", "Alpha Boulder") + row(2, "Alpha2", "Alpha Boulder"))
    run_all(c["id"])
    got = items(c["id"])
    proj = server.q("SELECT * FROM projects WHERE kind='boulder'", one=True)
    b0 = proj["mesh_id"]
    assert got["Alpha"]["mesh_id"] == got["Alpha2"]["mesh_id"] == b0 and got["Alpha"]["status"] == "done", got
    alpha, alpha2 = got["Alpha"]["climb_id"], got["Alpha2"]["climb_id"]
    make_old(b0)
    then = time.time()
    for mid, rules, cid in (("ao", 0, alpha), ("an", 1, alpha)):    # Alpha's own: one from before, one under the rules
        server.q("INSERT INTO meshes(id,name,filename,source,created,rules) VALUES(?,?,?,?,?,?)", mid, "Alpha", "m.ply", f"project:{mid}x",
                 then - 60, rules, write=True)
        server.link_mesh(mid, cid, "auto-linked from reconstruction project")
    server.old_changed()
    assert server.old_waiting(fresh=True) == {alpha2}, "Alpha has a mesh under the rules"
    start(c["id"])
    harvested.clear()
    run_all(c["id"])
    got = items(c["id"])
    b1 = server.q("SELECT mesh_id FROM projects WHERE id=?", proj["id"], one=True)["mesh_id"]
    assert harvested == [("Alpha2", True, True), ("Alpha Boulder", False, False)], harvested
    assert b1 != b0 and mesh(b0)["replaced_by"] == b1 and mesh(b1)["rules"] == server.MESH_RULES, (mesh(b0), mesh(b1))
    assert got["Alpha2"]["status"] == "done" and got["Alpha2"]["mesh_id"] == b1, got["Alpha2"]
    assert mesh("ao")["replaced_by"] == b1 and not server.q("SELECT 1 FROM mesh_links WHERE mesh_id='ao'"), "under older rules: replaced"
    assert mesh("an")["replaced_by"] is None and server.q("SELECT 1 FROM mesh_links WHERE mesh_id='an'"), "under the same rules: kept"
    r = server.old_mesh_report(fresh=True)
    assert (r["old_meshes"], r["waiting"], r["missed"]) == (0, 0, []), r
    server.settings().update({"areas.meshes": "climbs"})
    print("a boulder's mesh made again, and only meshes under older rules replaced OK")

    # --- a group's climbs whose photos match, pooled into a mesh under older rules: pooled again from the same climbs,
    # once tried under the rules not again -----------------------------------------------------------------------------
    fresh()
    server.settings().update({"areas.meshes": "boulders"})
    server.link_climbs = lambda ids, log: None      # their photos aren't compared here: linked below
    c = server.create_campaign("", "popularity", HEAD + row(1, "Marie", "Bas Cuvier") + row(2, "Rose", "Bas Cuvier"), start=False)
    leaf = items(c["id"])["Marie"]["boulder"]
    server.q("INSERT INTO boulders(key, name, kind, why, checked) VALUES(?,?,?,?,?)", leaf, "Bas Cuvier", "group", "test", time.time(), write=True)
    start(c["id"])
    run_all(c["id"])
    got = items(c["id"])
    assert got["Marie"]["mesh_id"] != got["Rose"]["mesh_id"] and got["Rose"]["status"] == "done", got
    server.link(got["Marie"]["climb_id"], got["Rose"]["climb_id"], 30, "photos")
    server._settled.clear()
    assert server.settle_clusters(c["id"], leaf) == 1
    run_all(c["id"])
    pool = server.q("SELECT * FROM projects WHERE kind='boulder'", one=True)
    p0 = pool["mesh_id"]
    assert p0 and server.climb_row(got["Rose"]["climb_id"])["n_links"] == 2, pool
    server._settled.clear()
    assert server.settle_clusters(c["id"], leaf) == 0, "these same climbs: not again"
    make_old(p0)
    server._settled.clear()
    assert server.settle_clusters(c["id"], leaf) == 1, "its mesh is old: pooled again"
    run_all(c["id"])
    p1 = server.q("SELECT mesh_id FROM projects WHERE id=?", pool["id"], one=True)["mesh_id"]
    assert p1 != p0 and mesh(p0)["replaced_by"] == p1, (mesh(p0), p1)
    make_old(p1)
    server.q("UPDATE projects SET progress=? WHERE id=?", json.dumps({"steps": [{"key": "x", "started": time.time()}]}), pool["id"], write=True)
    server._settled.clear()
    assert server.settle_clusters(c["id"], leaf) == 0, "tried under the rules already"
    server.settings().update({"areas.meshes": "climbs"})
    print("a group's pools made again OK")

    # --- old by data: a newer mesh under the same rules, made from photos one lacked, of every climb it's of; without a
    # record of what each was made from, one whose photos are some of the newer one's; anything else is to decide ----
    fresh()
    import meshinputs
    import provenance
    now = time.time()
    for cid in ("x", "y", "z", "w"):
        server.q("INSERT INTO climbs(id,source,source_id,url,name,created) VALUES(?,?,?,?,?,0)", cid, "mountainproject", cid, f"https://x/{cid}",
                 f"Climb {cid}", write=True)

    def made(mid, created, photos=None, names=None, boulder=None, places=(), rules=1, climb=None):
        server.q("INSERT INTO meshes(id,name,filename,source,created,rules,boulder) VALUES(?,?,?,?,?,?,?)", mid, f"Mesh {mid}", "m.ply",
                 f"project:p{mid}", created, rules, boulder, write=True)
        (server.MESHES / mid).mkdir(parents=True, exist_ok=True)
        if photos is not None:     # {name: sha1}
            files = {n: [3, h] for n, h in photos.items()}
            meshinputs.save(server.MESHES / mid, {"photos": {"n": len(files), "digest": meshinputs.digest_of(files), "at": created,
                                                             "files": files}, "meshopts": {"x": 1}, "mesh_rules": rules})
        if names is not None:
            (server.MESHES / mid / provenance.FILE).write_text(json.dumps({"basis": "given", "photos": [{"file": n} for n in names]}))
        if places:
            (server.MESHES / mid / "views.json").write_text(json.dumps({"climbs": {c: {"photos": 1, "of": 1} for c in places}}))
        if climb:
            server.link_mesh(mid, climb, "auto-linked from reconstruction project")

    rock = json.dumps(["Rock"])
    made("xa", now - 300, photos={"a.jpg": "s1", "b.jpg": "s2"}, climb="x")             # x's own
    made("xb", now - 200, photos={"a.jpg": "s1", "b.jpg": "s2", "c.jpg": "s3"}, boulder=rock, places=("x", "y"))  # its boulder's, from more
    made("xo", now - 400, photos={"a.jpg": "s1"}, climb="x", rules=0)                   # old by rules: not looked at
    made("ya", now - 150, names=["p.jpg"], climb="z")                                   # z's, no record: by name
    made("yb", now - 100, names=["p.jpg", "q.jpg"], climb="z")
    made("wa", now - 90, photos={"a.jpg": "s9"}, climb="w")                              # w's twice, from the same photos
    made("wb", now - 80, photos={"a.jpg": "s9"}, climb="w")
    got = server.old_by_data(log=lambda s: None)
    flags = {r["id"]: r["old_data"] for r in server.q("SELECT id, old_data FROM meshes")}
    assert flags == {"xa": "xb", "xb": None, "xo": None, "ya": "yb", "yb": None, "wa": None, "wb": None}, flags
    assert [(d["id"], d["newer"]) for d in got["decide"]] == [("wa", "wb")] and "a different result" in got["decide"][0]["why"], got
    assert not server.q("SELECT 1 FROM meshes WHERE replaced_by IS NOT NULL"), "never made older versions"
    r = server.old_mesh_report(fresh=True)
    assert (r["old_data"], r["old_meshes"], [d["id"] for d in r["decide"]]) == (2, 1, ["wa"]), r
    # decided: old, or kept; and it stands
    server.decide_old("wa", "wb", "keep")
    assert server.old_by_data(log=lambda s: None)["decide"] == [] and server.q("SELECT old_data FROM meshes WHERE id='wa'", one=True)["old_data"] is None
    server.decide_old("wa", "wb", "old")
    assert server.q("SELECT old_data FROM meshes WHERE id='wa'", one=True)["old_data"] == "wb"
    try:
        server.decide_old("wa", "wb", "maybe")
        raise AssertionError("only old or keep")
    except ValueError:
        pass
    # the newer one gone: no longer old by data
    server.q("DELETE FROM meshes WHERE id='xb'", write=True)
    server.old_by_data(log=lambda s: None)
    assert server.q("SELECT old_data FROM meshes WHERE id='xa'", one=True)["old_data"] is None
    # a newer one made from fewer of the same photos is to decide, not old
    made("xc", now - 50, photos={"a.jpg": "s1"}, climb="x")
    got = server.old_by_data(log=lambda s: None)
    assert [(d["id"], d["newer"]) for d in got["decide"]] == [("xa", "xc")] and "fewer" in got["decide"][0]["why"], got
    print("old by data OK")
    print("ALL OLD RERUN TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
