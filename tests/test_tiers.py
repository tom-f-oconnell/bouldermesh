#!/usr/bin/env python3
"""What's out of date, by tier (server.mesh_tiers()), and remaking a tier at low priority (server.queue_remakes()):
meshes made by an older pipeline, from the CPU's SIFT without GPU SIFT's being tried or judged, textured by older
texturing, and with photos they weren't made from; each mesh's pipeline record (pipeline.write_record()) and the record
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
    t = server.mesh_tiers(fresh=True)
    assert (t["meshes"], t["rules"], t["pipeline"], t["fallback"]["n"]) == (9, 1, 2, 1), t
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
    print("ALL TIERS TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
