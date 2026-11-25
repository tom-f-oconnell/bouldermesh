#!/usr/bin/env python3
"""What's out of date, by tier (server.mesh_tiers()), and remaking a tier at low priority (server.queue_remakes()):
meshes made by an older pipeline, from the CPU's SIFT without GPU SIFT's being tried or judged, textured by older
texturing, with photos they weren't made from, and densified while OpenMVS cropped to a region it guessed (roicrop.py);
each mesh's pipeline record (pipeline.write_record()) and the record
of meshes from before it (server.pipeline_backfill()). Offline.

    python3 tests/test_tiers.py
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tarfile
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = tempfile.mkdtemp(prefix="bouldermesh-tiers-")
os.environ["DATA_DIR"] = tmp

import meshinputs  # noqa: E402
import pipeline  # noqa: E402
import reconstruction  # noqa: E402
import server  # noqa: E402

try:
    # ---- which features a placing kept, and why; which of those the lowest tier takes
    fr = pipeline.features_record
    assert fr({"device": "gpu"}, {}, []) == {"kept": "gpu", "why": None}
    assert fr({"device": "cpu"}, {"plain_sift_rejected": {"kept": "cpu"}}, []) == {"kept": "cpu", "why": "judged"}
    assert fr({"device": "cpu"}, {"plain_sift_rejected": {"why": "x"}}, [])["why"] == "judged", "before the better was kept"
    assert fr({"device": "cpu"}, {}, [{"step": "feature_extractor"}])["why"] == "gpu-failed"
    assert fr({"device": "cpu"}, {}, [])["why"] == "not-tried"
    tier = lambda f: pipeline.fallback_tier({"features": f})  # noqa: E731
    assert tier({"kept": "cpu", "why": "not-tried"}) == "not-tried" and tier({"kept": "cpu", "why": "judged"}) is None
    assert tier({"kept": "gpu", "why": None}) is None and pipeline.fallback_tier(None) is None
    print("features kept OK")

    # ---- a mesh's pipeline record, as the pipeline writes it beside the mesh
    with tempfile.TemporaryDirectory() as td:
        work, out = pathlib.Path(td) / "work", pathlib.Path(td) / "out"
        (work / "dense" / "images").mkdir(parents=True)
        (work / pipeline.EXTRACT_FILE).write_text(json.dumps({"device": "gpu"}))
        (work / pipeline.MAPPER_FILE).write_text(json.dumps({"mapper": "incremental"}))
        (work / "dense" / "text.json").write_text("{}")
        tex = pipeline.texture_record(work / "dense" / "images", ["--ignore-mask-label", "0"])
        assert tex["version"] == pipeline.TEXTURE_VERSION and tex["masked"] and tex["text"], tex
        rec = pipeline.write_record(work, out, tex)
        assert json.loads((out / pipeline.PIPELINE_FILE).read_text()) == json.loads(json.dumps(rec))
        assert rec["version"] == pipeline.PIPELINE_VERSION and rec["features"]["kept"] == "gpu" and rec["mapper"] == "incremental"
        assert not pipeline.texture_record(out, [])["text"], "no text looked for"
    print("pipeline record OK")

    # ---- meshes of each tier
    server.init_db()
    now = time.time()
    current = {"version": pipeline.PIPELINE_VERSION, "features": {"kept": "gpu", "why": None}}
    tex_now = {"version": pipeline.TEXTURE_VERSION}

    def mesh(mid: str, kind: str = "climb", made: dict | None = None, tex: dict | None = None, rules: int = 1, photos=("a.jpg",),
             kept: bool = True, **more) -> str:
        pid = f"p{mid}"
        server.q("INSERT INTO meshes(id,name,filename,source,created,rules,boulder) VALUES(?,?,?,?,?,?,?)", mid, mid, "mesh.glb",
                 f"project:{pid}", now - 100, rules, more.get("leaf") if kind == "boulder" else None, write=True)
        server.q("INSERT INTO projects(id,name,created,status,kind,boulder) VALUES(?,?,?,?,?,?)", pid, mid, now - 200, "done",
                 "boulder" if kind in ("boulder", "pool") else "harvest", more.get("key") or more.get("leaf"), write=True)
        d = server.MESHES / mid
        d.mkdir(parents=True)
        (server.PROJECTS / pid / "photos").mkdir(parents=True)
        for p in photos:
            (server.PROJECTS / pid / "photos" / p).write_bytes(b"jpg")
        if kept:
            with tarfile.open(d / reconstruction.FILE, "w:gz") as tar:
                data = json.dumps({"extract": {"device": "cpu"}}).encode()
                info = tarfile.TarInfo("build.json")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        rec = {"project": pid, "kind": kind, "photos": {"n": len(photos), "digest": "x", "at": now,
                                                         "files": {p: [3, "s"] for p in photos}}, "unknown": {}, **more}
        if made is not None:
            rec["pipeline"], rec["texture"] = made, tex
        meshinputs.save(d, rec)
        return pid

    mesh("ok", made=current, tex=tex_now)
    mesh("oldrules", made=current, tex=tex_now, rules=0)
    mesh("oldpipe", made={**current, "version": 0}, tex=tex_now)
    mesh("cpu", made={**current, "features": {"kept": "cpu", "why": "not-tried"}}, tex=tex_now)
    mesh("judged", made={**current, "features": {"kept": "cpu", "why": "judged"}}, tex=tex_now)
    mesh("oldtex", made=current, tex={"version": 1})
    mesh("oldtexgone", made=current, tex={"version": 1}, kept=False)
    pid = mesh("newphotos", made=current, tex=tex_now)
    (server.PROJECTS / pid / "photos" / "b.jpg").write_bytes(b"jpg")
    mesh("before", made=None, tex=None)       # from before meshes recorded what made them
    mesh("novggt", made={**current, "vggt_skipped": "COLMAP's mapper placed only 4, and VGGT couldn't run: only 7149 MB"}, tex=tex_now)
    # made from photos its filter now rejects: in the tier; from photos still to be judged by today's CLIP (kept without
    # its score, or by another CLIP): waiting for it; neither for those it wasn't made from
    now_key = server.relevance_mod.settings_key()
    pid = mesh("unclipped", made=current, tex=tex_now)
    (server.PROJECTS / pid / "relevance.json").write_text(json.dumps({"kept": ["a.jpg"], "scores": {"a.jpg": {"flat": 0.1}},
                                                                      "rejected": {}, "settings": now_key}))
    pid = mesh("filtered", made=current, tex=tex_now)
    (server.PROJECTS / pid / "relevance.json").write_text(json.dumps({"kept": [], "rejected": {"a.jpg": "notboulder"},
                                                                      "scores": {"a.jpg": {"boulder": 0.01}}, "settings": now_key}))
    pid = mesh("clipped", made=current, tex=tex_now, photos=("b.jpg",))
    (server.PROJECTS / pid / "relevance.json").write_text(json.dumps({"kept": ["a.jpg", "b.jpg"], "rejected": {"c.jpg": "card"},
                                                                      "scores": {"a.jpg": {}, "b.jpg": {"boulder": 0.9}}, "settings": now_key}))
    t = server.mesh_tiers(fresh=True)
    assert (t["meshes"], t["rules"], t["pipeline"], t["fallback"]["n"]) == (13, 1, 2, 1), t
    assert t["ids"]["vggt"] == ["novggt"] and t["vggt"] == 1, "VGGT needed where it couldn't run: a tier of its own"
    assert t["ids"]["clip"] == ["filtered"] and t["clip"] == 1 and t["clip_waits"] == 1, (t["ids"]["clip"], t["clip_waits"])
    assert set(t["ids"]["pipeline"]) == {"oldpipe", "before"} and t["ids"]["fallback"] == ["cpu"] and t["fallback"]["why"] == {"not-tried": 1}
    assert set(t["ids"]["texture"]) == {"oldtex", "before"} and t["texture_unkept"] == 1, t
    assert t["ids"]["new_data"] == ["newphotos"] and t["new_photos"] == 1, t
    assert server.mesh_tiers()["at"] == t["at"], "kept a while"
    # what made a mesh from before then, from its files: its kept reconstruction's features, counted as version 1
    made, tex = server.pipeline_backfill("before")
    assert made["version"] == 1 and made["backfilled"] and made["features"] == {"kept": "cpu", "why": "not-tried"} and tex["version"] == 1
    assert server.pipeline_existing_meshes() == 1 and meshinputs.load(server.MESHES / "before")["pipeline"]["version"] == 1
    assert server.pipeline_existing_meshes() == 0, "once"
    print("tiers OK")

    # ---- before it's meshed, a harvest's photos kept before the CLIP check are judged by it, other verdicts standing
    pid = "punclipped"
    asked = []

    def run_filter(paths, rejected, log, prior=None, assess=None, **kw):
        asked.append(([p.name for p in paths], prior))
        return {"version": 1, "methods": ["cards", "boulder", "same-rock"], "kept": [], "rejected": {"a.jpg": "notboulder"},
                "counts": {"notboulder": 1}, "scores": {"a.jpg": {"boulder": 0.1}}}
    real = server.relevance_mod.run_filter, server.diversity_mod.assess_unique
    server.relevance_mod.run_filter, server.diversity_mod.assess_unique = run_filter, lambda paths, log, progress=None: {"verdict": "poor", "n": len(paths)}
    server.q("UPDATE projects SET harvest=? WHERE id=?", json.dumps({"images": 1, "diversity": {"verdict": "good"}}), pid, write=True)
    logged = []
    try:
        assert server.clip_unchecked(pid, logged.append) and asked[0][0] == ["a.jpg"] and asked[0][1]["kept"] == ["a.jpg"], asked
        h = json.loads(server.q("SELECT harvest FROM projects WHERE id=?", pid, one=True)["harvest"])
        assert h["diversity"] == {"verdict": "poor", "n": 0} and h["relevance"]["counts"] == {"notboulder": 1}, h
        assert json.loads((server.PROJECTS / pid / "relevance.json").read_text())["rejected"] == {"a.jpg": "notboulder"}
        assert not server.clip_unchecked(pid, logged.append) and len(asked) == 1, "judged: not again"
        assert not server.clip_unchecked("pclipped", logged.append) and len(asked) == 1, "its unchecked photo isn't among its photos"
        # the sweep: a current mesh's project whose photos it was made from wait for today's CLIP, judged once
        asked.clear()
        pid = mesh("sweep", made=current, tex=tex_now)     # kept by another CLIP's prompts: all it kept waits
        (server.PROJECTS / pid / "relevance.json").write_text(json.dumps({"kept": ["a.jpg"], "rejected": {}, "scores": {"a.jpg": {"boulder": 0.8}},
                                                                          "settings": now_key.replace(server.relevance_mod._clip_key(), "old/old/0")}))
        assert server.filtered_from(server.meshinputs.load(server.MESHES / "sweep")) == (False, True)
        assert server.clip_sweep_once() == 1 and asked[0][0] == ["a.jpg"] and asked[0][1]["kept"] == ["a.jpg"], asked
        assert server.filtered_from(server.meshinputs.load(server.MESHES / "sweep")) == (True, False), "rejected now: in the tier"
        assert server.clip_sweep_once() == 0
    finally:
        server.relevance_mod.run_filter, server.diversity_mod.assess_unique = real
    print("judging photos kept before the CLIP check OK")

    # ---- remaking a tier: on an area's climb that isn't busy, at low priority, as the kind of mesh says
    server.q("INSERT INTO campaigns(id,name,created,status) VALUES('c1','Area',?,'done')", now, write=True)
    for sid, rank, project, status, boulder in (("1", 1, "poldpipe", "done", None), ("2", 2, "pcpu", "running", None)):
        server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,project_id,status,boulder) VALUES('c1',?,?,?,?,?,?)",
                 sid, rank, sid, project, status, boulder, write=True)
    got = server.queue_remakes(["oldpipe", "cpu", "ok"], "pipeline")
    assert got == {"queued": 1, "busy": 1, "not_in_area": 1}, got
    row = server.q("SELECT * FROM campaign_items WHERE source_id='1'", one=True)
    assert (row["status"], row["redo"], row["old_rerun"], row["was"]) == ("pending", "rebuild", 3, "done") and "older pipeline" in row["note"]
    # a boulder's mesh: its boulder meshed again, even from the same photos, on the area's highest-ranked climb on it
    leaf = json.dumps(["CA", "JT", "Rock"])
    mesh("rock", kind="boulder", made={**current, "version": 0}, tex=tex_now, leaf=leaf, climbs={})
    for sid, rank in (("4", 4), ("3", 3)):
        server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,status,boulder) VALUES('c1',?,?,?,'done',?)",
                 sid, rank, sid, leaf, write=True)
    assert server.queue_remakes(["rock"], "pipeline")["queued"] == 1
    assert server.q("SELECT source_id, redo, old_rerun FROM campaign_items WHERE redo LIKE 'boulder%'", one=True) == \
        {"source_id": "3", "redo": "boulder+again", "old_rerun": 3}
    # the admin page's action, and the order low-priority work is taken in
    try:
        server.admin_remake_tier({"tier": "nonesuch"})
        raise AssertionError("an unknown tier")
    except ValueError:
        pass
    assert server.REMAKE_RANKS["fallback"] > server.REMAKE_RANKS["pipeline"] > 1
    print("remaking a tier OK")

    # a boulder's mesh: photos written into a harvest it pooled since count, those that only left it (or came back
    # from the backup with their own times) don't; a harvest it didn't pool counts whole
    def harvest(hid: str, cid: str, names, when: float) -> None:
        server.q("INSERT INTO projects(id,name,created,status,kind,climb_id,harvest) VALUES(?,?,?,?,?,?,?)", hid, hid, when, "harvested",
                 "harvest", cid, "{}", write=True)
        (server.PROJECTS / hid / "photos").mkdir(parents=True)
        for n in names:
            f = server.PROJECTS / hid / "photos" / n
            f.write_bytes(b"jpg")
            os.utime(f, (when, when))
    rec = {"project": "pb", "kind": "boulder", "leaf": "[]", "climbs": {"c1": ["h1"], "c2": ["h2"]}, "photos": None}
    harvest("h1", "c1", ["a.jpg", "b.jpg"], now - 500)
    harvest("h2", "c2", ["c.jpg"], now - 500)
    (server.PROJECTS / "h1" / "photos" / "gone.jpg").write_bytes(b"x")
    (server.PROJECTS / "h1" / "photos" / "gone.jpg").unlink()      # the folder changed, nothing came
    assert server._new_photos(rec, now - 100) == 0
    f = server.PROJECTS / "h2" / "photos" / "yt_new_000.jpg"
    f.write_bytes(b"jpg")
    assert server._new_photos(rec, now - 100) == 1, "a new video's frame"
    harvest("h3", "c1", ["d.jpg", "e.jpg"], now - 50)
    assert server._new_photos(rec, now - 100) == 3, "and a newer harvest of a climb it pooled, whole"
    # where what it pooled is recorded by name: the same photos written anew (harvested again) aren't new
    for n in ("a.jpg", "b.jpg"):
        (server.PROJECTS / "h1" / "photos" / n).write_bytes(b"jpg")
    assert server._new_photos({**rec, "pooled": ["a.jpg", "b.jpg", "c.jpg"]}, now - 100) == 3, "the frame and the newer harvest"
    print("a boulder's new photos OK")

    # ---- OpenMVS's region of interest: each densify run in a job log, matched to its mesh by the faces TextureMesh saved
    import roicrop  # noqa: E402
    LOG = """== OpenMVS: densify
10:00:00 [App     ] Depth-maps fused and filtered: 31 depth-maps, 4521844 depths, 592321 points (13%%) (1s398ms)
== OpenMVS: texture mesh
10:05:00 [Mesh    ] Mesh 'scene_dense_mesh_texture.glb' saved: 50 vertices, 1000 faces (1ms)
== OpenMVS: densify
20:16:24 [Scene   ] ROI estimated with position (1.7,0.4,2.2) and extent (0.09,0.26,4.99): scale 1.100000, up axis -1
20:17:26 [ScnDense] Depth-maps fused and filtered: 137 depth-maps, 19808794 depths, 1600000 points (8%%), 11.22 hits
20:17:26 [ScnDense] Point-cloud trimmed to ROI: 1480000 points removed (52ms)
INTERRUPTED: the server restarted during this job: Retry carries on from the last step it finished
== OpenMVS: texture mesh
20:18:32 [Mesh    ] Mesh 'scene_dense_mesh_texture.glb' saved: 7900 vertices, 15757 faces (96ms)
== OpenMVS: densify
21:00:00 [Scene   ] ROI estimated with position (0,0,0) and extent (5,5,5): scale 1.100000, up axis -1
21:00:10 [ScnDense] Depth-maps fused and filtered: 10 depth-maps, 100 depths, 1000 points (8%%), 11.22 hits
21:00:10 [ScnDense] Point-cloud trimmed to ROI: 50 points removed (52ms)
21:00:20 [Mesh    ] Mesh 'scene_dense_mesh_texture.glb' saved: 10 vertices, 2000 faces (96ms)
"""
    found = roicrop.runs(LOG.splitlines())
    assert [(r["estimated"], r["fused"], r["cut"], r["faces"]) for r in found] == \
        [(False, 592321, 0, 1000), (True, 1600000, 1480000, 15757), (True, 1000, 50, 2000)], found
    assert roicrop.record(15757, now, found) == {"affected": True, "share": 0.925, "cut": 1480000, "fused": 1600000, "from": "log"}
    assert roicrop.record(1000, now, found)["affected"] is False, "densified without a region: 2.3"
    assert roicrop.record(7, now, found) == {"affected": True, "share": None, "cut": None, "fused": None, "from": "date"}
    assert roicrop.record(7, roicrop.SINCE - 1, found)["affected"] is False, "before 2.4 came in"

    def cropped_mesh(mid: str, faces: int, made: dict | None = None) -> str:
        pid = mesh(mid, made=made or current, tex=tex_now)
        (server.MESHES / mid / "cleanup.json").write_text(json.dumps({"faces": faces}))
        (server.PROJECTS / pid / "job.log").write_text(LOG)
        return pid
    cropped_mesh("sliver", 15757)
    cropped_mesh("trimmed", 2000)
    cropped_mesh("whole", 1000)
    cropped_mesh("fixed", 15757, {**current, "openmvs": [2, 4], "dense": pipeline.MVS24_DENSIFY})
    assert server.roi_existing_meshes() >= 4 and server.roi_existing_meshes() == 0, "worked out once, kept"
    assert meshinputs.load(server.MESHES / "sliver")["roi"]["share"] == 0.925 and server._roi["trimmed"]["share"] == 0.05
    assert not server._roi["whole"]["affected"] and server._roi["fixed"] == {"affected": False, "share": 0.0, "cut": 0, "fused": None,
                                                                              "from": "record"}
    t = server.mesh_tiers(fresh=True)
    ids = t["ids"]["roi"]
    assert ids[0] == "sliver" and ids[-1] == "trimmed" and "whole" not in ids and "fixed" not in ids, ids
    assert t["roi_urgent"] == 1 and t["roi_flagged"] == 1 and t["roi"] == len(ids) and t["roi_unknown"] == len(ids) - 2, t
    # remade: the one cut most with old meshes, the rest at low priority
    for sid, pid in (("5", "psliver"), ("6", "ptrimmed")):
        server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,project_id,status) VALUES('c1',?,9,?,?,'done')",
                 sid, sid, pid, write=True)
    assert server.queue_remakes(["sliver", "trimmed"], "roi")["queued"] == 2
    ranks = {r["source_id"]: (r["old_rerun"], r["redo"]) for r in server.q("SELECT * FROM campaign_items WHERE source_id IN ('5','6')")}
    assert ranks == {"5": (server.ROI_URGENT_RANK, "rebuild"), "6": (server.REMAKE_RANKS["roi"], "rebuild")}, ranks
    assert "cropped it" in server.q("SELECT note FROM campaign_items WHERE source_id='5'", one=True)["note"]
    print("OpenMVS's cropping OK")

    # a machine's job is kept over a restart by its inputs' fingerprint, or by its photos where that can't be told
    whole = {"photos": {"n": 1, "digest": "d1", "at": 0, "files": {}}, "filter": "none", "unknown": {}}
    assert server.job_identity(whole)["fingerprint"] == server.inputs_fingerprint(whole)
    old_filter = {**whole, "filter": {"version": None, "settings": "s", "methods": []}}
    assert server.inputs_fingerprint(old_filter) is None and server.job_identity(old_filter)["fingerprint"] == "photos:d1"
    assert server.job_identity(None)["fingerprint"] is None and server.job_identity(whole, "m1")["add_to"] == "m1"
    # and a filter's, beta's or size's, by what it was asked: its arguments, files' names, and photos' sizes
    ph = pathlib.Path(tmp) / "ph.jpg"
    ph.write_bytes(b"12345")
    one = server.machine_identity("filter", {"photos/ph.jpg": ph, "cache.sqlite": pathlib.Path(tmp) / "nonesuch"}, {"a": 1}, [1, 1])
    assert one == server.machine_identity("filter", {"photos/ph.jpg": ph, "cache.sqlite": pathlib.Path(tmp) / "x"}, {"a": 1}, [1, 1])
    assert one["fingerprint"] != server.machine_identity("filter", {"photos/ph.jpg": ph}, {"a": 2}, [1, 1])["fingerprint"]
    ph.write_bytes(b"123456")
    assert one["fingerprint"] != server.machine_identity("filter", {"photos/ph.jpg": ph, "cache.sqlite": ph}, {"a": 1}, [1, 1])["fingerprint"]
    print("jobs kept over a restart OK")
    print("ALL TIERS TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
