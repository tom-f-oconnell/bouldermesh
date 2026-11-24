#!/usr/bin/env python3
"""A mesh made again from its cameras as they were (server.queue_kept(), run_kept(), carry_kept(); pipeline._use_kept()):
queued at low priority, the pipeline given its kept reconstruction, what's made from its cameras carried over to the
new mesh, and the meshes whose cameras can't be taken (none kept, placed collapsed) left for their photos to be placed
again. Offline: the pipeline is stubbed.

    python3 tests/test_kept.py
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import struct
import sys
import tarfile
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = tempfile.mkdtemp(prefix="bouldermesh-kept-")
os.environ["DATA_DIR"] = tmp

import compute  # noqa: E402
import meshinputs  # noqa: E402
import modelcheck  # noqa: E402
import pipeline  # noqa: E402
import reconstruction  # noqa: E402
import server  # noqa: E402

try:
    server.init_db()
    now = time.time()

    def mesh(mid: str, kept: bool = True, kind: str = "climb") -> str:
        """A current mesh of a project of its own, cropped by OpenMVS by half, with what its cameras made beside it."""
        pid = f"p{mid}"
        server.q("INSERT INTO meshes(id,name,filename,source,created,rules) VALUES(?,?,?,?,?,?)", mid, mid, "mesh.glb",
                 f"project:{pid}", now - 100, server.MESH_RULES, write=True)
        server.q("INSERT INTO projects(id,name,created,status,kind,mesh_id) VALUES(?,?,?,?,?,?)", pid, mid, now - 200, "done",
                 "harvest", mid, write=True)
        d, photos = server.MESHES / mid, server.PROJECTS / pid / "photos"
        d.mkdir(parents=True)
        photos.mkdir(parents=True)
        for n in ("a.jpg", "b.jpg", "late.jpg"):
            (photos / n).write_bytes(b"jpg")
        if kept:
            images = struct.pack("<Q", 2) + b"".join(struct.pack("<I7dI", i + 1, 1, 0, 0, 0, 0, 0, 5, 1) + n + b"\0" +
                                                     struct.pack("<Q", 0) for i, n in enumerate((b"a.jpg", b"b.jpg")))
            with tarfile.open(d / reconstruction.FILE, "w:gz") as tar:
                for name, data in (("build.json", json.dumps({"extract": {"device": "gpu"}}).encode()), ("model/images.bin", images),
                                   ("model/points3D.bin", struct.pack("<Q", 0)), ("model/cameras.bin", struct.pack("<Q", 0))):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            (d / reconstruction.FEATURES_FILE).write_bytes(b"features")
        for name, body in (("cameras.json", [{"name": "a.jpg"}, {"name": "b.jpg"}]), ("orientation.json", {"method": "photos"}),
                           ("scale.json", {"status": "done", "metres_per_unit": 2.0}), ("beta.json", {"sets": []}),
                           ("heading.json", {"edit": 123}), (pipeline.FALLBACKS_FILE, [{"step": "plain_sift"}]),
                           ("cleanup.json", {"faces": 1000})):
            (d / name).write_text(json.dumps(body))
        (d / "beta_0def1a7a.jpg").write_bytes(b"jpg")
        meshinputs.save(d, {"project": pid, "kind": kind, "unknown": {},
                            "photos": {"n": 2, "digest": "d", "at": now, "files": {"a.jpg": [3, "s"], "b.jpg": [3, "s"]}},
                            "pipeline": {"version": 1, "colmap": "COLMAP 3.9.1", "features": {"kept": "gpu", "why": None},
                                         "mapper": "incremental"},
                            "roi": {"affected": True, "share": 0.5, "cut": 5, "fused": 10, "from": "log"}})
        return pid

    pid = mesh("old")
    (server.MESHES / "old" / server.REST_FILE).write_text(json.dumps({"models": [
        {"name": "1", "counts": True, "climbs": ["c1"], "attempt": {"status": "done", "mesh": "r1"}},
        {"name": "2", "counts": True, "climbs": ["c2"], "attempt": {"status": "queued", "project": "gone"}}]}))
    asked: list[dict] = []

    def made_again(images, work, out_dir, log, use_gpu=False, internet=False, tracker=None, add_to=None, kept=None):
        """As the pipeline does from kept cameras: a mesh, its record and this run's fallbacks, none of its placing."""
        asked.append({"kept": kept, "add_to": add_to})
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "mesh.ply").write_text("ply")
        (out_dir / pipeline.PIPELINE_FILE).write_text(json.dumps({"version": pipeline.PIPELINE_VERSION, "colmap": "COLMAP 4.2.1",
                                                                  "dense": pipeline.MVS24_DENSIFY}))
        (out_dir / pipeline.FALLBACKS_FILE).write_text(json.dumps([{"step": "DensifyPointCloud"}]))
        return out_dir / "mesh.ply", False
    pipeline.reconstruct = made_again

    # queued at low priority: not taken while regular work is looked for, then taken
    got = server.queue_kept(["old", "old", "nonesuch"], "OpenMVS cropped it")
    assert got == {"queued": 1, "busy": 0, "unkept": 1}, got
    assert server.q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "queued"
    assert server.queue_kept(["old"], "again")["busy"] == 1, "queued already"
    assert server.take_job(False) is None
    job = server.take_job(True)
    assert job["type"] == "from-cameras" and job["mid"] == "old" and job["low"], job
    assert json.loads(server.q("SELECT job FROM projects WHERE id=?", pid, one=True)["job"])["type"] == "from-cameras", "kept over a restart"
    server.run_job(job)

    new = server.q("SELECT mesh_id FROM projects WHERE id=?", pid, one=True)["mesh_id"]
    assert new != "old" and server.q("SELECT replaced_by FROM meshes WHERE id='old'", one=True)["replaced_by"] == new
    assert asked == [{"kept": server.MESHES / "old", "add_to": None}], asked
    d, o = server.MESHES / new, server.MESHES / "old"
    assert os.path.samefile(d / reconstruction.FILE, o / reconstruction.FILE), "linked, not copied"
    assert os.path.samefile(d / reconstruction.FEATURES_FILE, o / reconstruction.FEATURES_FILE)
    for name in ("cameras.json", "orientation.json", "scale.json", "beta.json", "heading.json", "beta_0def1a7a.jpg"):
        assert (d / name).read_bytes() == (o / name).read_bytes(), name
    assert not (d / "cleanup.json").exists() or json.loads((d / "cleanup.json").read_text()).get("faces") != 1000, "its faces are its own"
    assert [f["step"] for f in json.loads((d / pipeline.FALLBACKS_FILE).read_text())] == ["plain_sift", "DensifyPointCloud"]
    assert [m["attempt"] for m in server.load_rest(new)["models"]] == [{"status": "done", "mesh": "r1"}, None], \
        "a scene meshed stays so; one cut short is to be meshed again"
    rec = meshinputs.load(d)
    assert set(rec["photos"]["files"]) == {"a.jpg", "b.jpg"}, "made from the photos the cameras are of, not late.jpg"
    made = rec["pipeline"]
    assert (made["version"], made["colmap"], made["cameras_from"]) == (1, "COLMAP 3.9.1", "old") and made["dense"] == pipeline.MVS24_DENSIFY, made
    assert json.loads((d / pipeline.PIPELINE_FILE).read_text())["cameras_from"] == "old"
    assert meshinputs.load(o)["roi"]["kept"] == "made" and meshinputs.load(o)["roi"]["by"] == new
    assert all(j.get("type") != "scale" for j in list(server.jobs.queue)), "its size carried over, not found again"
    assert "made again from the cameras of mesh old as they were: OpenMVS cropped it" in server.read_log(pid)
    print("made again from its cameras, what they made carried over OK")

    # one whose project's mesh is another now: left; one that kept no reconstruction: counted apart
    assert server.queue_kept(["old"], "x") == {"queued": 0, "busy": 1, "unkept": 0}, "replaced"
    mesh("bare", kept=False)
    assert server.queue_kept(["bare"], "x")["unkept"] == 1
    # placed collapsed: not made again from its cameras, its record says why, its project done with its mesh as it was
    pid = mesh("speck")
    saved = modelcheck.collapsed_kept
    modelcheck.collapsed_kept = lambda d: ["yt_76f7d17e_000.jpg"] if d.name == "speck" else []
    try:
        assert server.queue_kept(["speck"], "x")["queued"] == 1
        server.run_job(server.take_job(True))
    finally:
        modelcheck.collapsed_kept = saved
    roi = meshinputs.load(server.MESHES / "speck")["roi"]
    assert roi["kept"] == "unusable" and "collapsed" in roi["why"], roi
    assert server.q("SELECT status, mesh_id FROM projects WHERE id=?", pid, one=True) == {"status": "done", "mesh_id": "speck"}
    t = server.mesh_tiers(fresh=True)
    assert "speck" in t["ids"]["roi_replace"] and "speck" not in t["ids"]["roi"] and "bare" in t["ids"]["roi"], t["ids"]
    assert t["roi_replace"] == 1
    print("meshes whose cameras can't be taken left for placing again OK")

    # no machine to be had: it waits in the queue, to carry on
    pid = mesh("wait")

    def no_machine(*a, **k):
        raise compute.ComputeUnavailable("no machine has asked this server for work")
    pipeline.reconstruct = no_machine
    assert server.queue_kept(["wait"], "x")["queued"] == 1
    server.run_job(server.take_job(True))
    again = server.take_job(True)
    assert again and again["mid"] == "wait", again
    assert server.q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "queued"
    server.current_worker().wait_until = 0
    pipeline.reconstruct = made_again
    server.run_job(again)
    assert server.q("SELECT mesh_id FROM projects WHERE id=?", pid, one=True)["mesh_id"] != "wait"
    print("waiting for a machine OK")

    # the admin page's button: those cut most first, made again from their cameras; one that kept none, from scratch
    server.q("INSERT INTO campaigns(id,name,created,status) VALUES('c1','Area',?,'done')", now, write=True)
    server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,project_id,status) VALUES('c1','1',1,'1','pbare','done')", write=True)
    pid = mesh("worse")
    rec = meshinputs.load(server.MESHES / "worse")
    meshinputs.save(server.MESHES / "worse", {**rec, "roi": {**rec["roi"], "share": 0.9}})
    said = server.admin_remake_tier({"tier": "roi"})["result"]
    assert said.startswith("queued 1 to be made again from their cameras") and "1 that kept no cameras to be made from scratch" in said, said
    assert server.take_job(True)["mid"] == "worse"
    assert server.q("SELECT redo FROM campaign_items WHERE source_id='1'", one=True)["redo"] == "rebuild"
    print("the admin page's Remake the cropped OK")

    # meshes that placed photos collapsed: found once in their kept reconstructions, placed again from scratch with old meshes
    pid = mesh("shrunk")
    server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,project_id,status) VALUES('c1','7',7,'7',?,'done')", pid, write=True)
    saved = modelcheck.collapsed_kept
    looked: list[str] = []
    modelcheck.collapsed_kept = lambda d: looked.append(d.name) or (["yt_1_000.jpg", "yt_1_001.jpg"] if d.name == "shrunk" else [])
    try:
        assert server.collapsed_existing_meshes() >= 1 and "shrunk" in looked
        looked.clear()
        assert server.collapsed_existing_meshes() == 0 and not looked, "looked in once"
    finally:
        modelcheck.collapsed_kept = saved
    assert meshinputs.load(server.MESHES / "shrunk")["collapsed"] == {"v": server.COLLAPSED_CHECK, "n": 2, "names": ["yt_1_000.jpg", "yt_1_001.jpg"]}
    t = server.mesh_tiers(fresh=True)
    assert t["ids"]["collapsed"] == ["shrunk"] and "shrunk" not in t["ids"]["roi"] and t["collapsed"] == 1, t["ids"]
    assert server._collapsed_scan["state"] == "done" and server._collapsed_scan["done"] == server._collapsed_scan["of"]
    assert server.admin_remake_tier({"tier": "collapsed"})["result"].startswith("queued 1 to be placed again from scratch, with old meshes")
    row = server.q("SELECT redo, old_rerun, note FROM campaign_items WHERE source_id='7'", one=True)
    assert (row["redo"], row["old_rerun"]) == ("rebuild", 1) and "collapsed" in row["note"], row
    print("meshes that placed photos collapsed, placed again OK")
    print("ALL KEPT TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
