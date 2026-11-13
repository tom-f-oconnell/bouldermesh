#!/usr/bin/env python3
"""Checking the backup holds what was offloaded to it (offload.py): nothing deleted here until a manifest from the backup
confirms each file; what a manifest shows it lost marked so, with your phone told at once; a rotating sample of SHA-1s;
offloading paused, and your phone told, after MANIFEST_HOURS without one; a job needing lost data blocked until the admin
page says to re-mesh without it or leave it as it is. Offline: the harvest and reconstruction are stubbed.

    python3 tests/test_backup_checks.py
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
tmp = tempfile.mkdtemp(prefix="bouldermesh-backupchecks-")
os.environ["DATA_DIR"] = tmp

import offload  # noqa: E402
import pipeline  # noqa: E402
import server  # noqa: E402
from climbs.common import Climb, Media  # noqa: E402

D = server.DATA
told: list[tuple[str, int]] = []
server.NOTIFIER.send = lambda title, text, actions=None, priority=3, tags=None: told.append((title, priority)) or True
harvested: list[str] = []


def fake_fetch(url, include_children=False):
    rid = url.split("/")[-2]
    item = server.q("SELECT data FROM campaign_items WHERE source_id=? LIMIT 1", rid, one=True)
    data = json.loads(item["data"]) if item else {}
    return [Climb(source="mountainproject", source_id=rid, url=url, name=url.split("/")[-1].capitalize(), kind="boulder", lat=34.0,
                  lng=-116.0, area_path=data.get("area_path") or [], images=[Media(url="http://x/1.jpg", kind="image")])]


def fake_harvest(c, project_dir, log, sources, relevance_filter=True, tracker=None, pool_from=None, anchors_from=None, want=None,
                 want_search=None, add=False, refresh=False, rejudge=False):
    harvested.append(c.name)
    (project_dir / "photos").mkdir(parents=True, exist_ok=True)
    (project_dir / "photos" / "new.jpg").write_bytes(b"jpg")
    (project_dir / "sources.json").write_text(json.dumps({"files": []}))
    return {"images": 20, "diversity": {"verdict": "good"}, "video_frames": 0, "queries": []}


def fake_reconstruct(images, work, out_dir, log, use_gpu=False, internet=False, tracker=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mesh.ply").write_text("ply")
    return out_dir / "mesh.ply", False


server.climbs_mod.fetch_climb_url = fake_fetch
server.harvest_mod.harvest = fake_harvest
pipeline.reconstruct = fake_reconstruct
server.pipeline.steps = lambda: []
server.CAMPAIGN_MIN_FREE = 0
server.init_db()
server.settings().update({"areas.meshes": "climbs"})


def put(rel: str, body: bytes = b"x" * 1000) -> pathlib.Path:
    f = D / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(body)
    return f


def manifest(text: str, at: float | None = None) -> None:
    """The handshake leaving the backup's manifest, sent at at (now)."""
    (D / offload.MANIFEST).write_text(text)
    t = time.time() if at is None else at
    os.utime(D / offload.MANIFEST, (t, t))


def row(path: str) -> dict:
    return server.q("SELECT * FROM offloads WHERE path=?", path, one=True)


def still(_):
    return True


try:
    # --- deleted here only once a manifest confirms each file as recorded ------------------------------------------
    A, B = "projects/aaaaaaaaaaaa/photos", "cache/videos/vvvvvvvvvvv.mp4"
    put(f"{A}/1.jpg")
    put(f"{A}/2.jpg", b"y" * 2000)
    put(B, b"video")
    for p, kind in ((A, "photos"), (B, "videos")):
        offload.mark(D, server.q, p, kind, "x")
    files = {r["file"]: r for r in server.q("SELECT * FROM offload_files")}
    assert set(files) == {f"{A}/1.jpg", f"{A}/2.jpg", B} and files[f"{A}/2.jpg"]["bytes"] == 2000 and len(files[B]["sha1"]) == 40, files
    (D / "backed-up").write_text(str(time.time() + 5))     # a good copy since, by time: no longer enough
    assert offload.finish(D, server.q, still, print) == 0 and (D / A / "1.jpg").exists()
    copy = pathlib.Path(tempfile.mkdtemp(prefix="bm-backup-copy-", dir=tmp))    # the backup's copy of data/
    for rel in (f"{A}/1.jpg", B):          # it lacks 2.jpg: A isn't confirmed, B is
        put_ = copy / rel
        put_.parent.mkdir(parents=True, exist_ok=True)
        put_.write_bytes((D / rel).read_bytes())
        os.utime(put_, (os.stat(D / rel).st_mtime,) * 2)
    got = offload.check_manifest(D, server.q, offload.read_manifest(offload.manifest_of(copy, [A, B])))
    assert (got["confirmed"], got["lost"]) == (1, []) and row(B)["confirmed"] and not row(A)["confirmed"], got
    assert offload.finish(D, server.q, still, print) == 5 and not (D / B).exists() and (D / A / "1.jpg").exists(), "only what it holds"
    # held with another size: not confirmed either
    (copy / A / "2.jpg").write_bytes(b"y" * 1999)
    offload.check_manifest(D, server.q, offload.read_manifest(offload.manifest_of(copy, [A, B])))
    assert not row(A)["confirmed"]
    (copy / A / "2.jpg").write_bytes((D / A / "2.jpg").read_bytes())
    os.utime(copy / A / "2.jpg", (os.stat(D / A / "2.jpg").st_mtime,) * 2)
    offload.check_manifest(D, server.q, offload.read_manifest(offload.manifest_of(copy, [A, B])))
    assert row(A)["confirmed"]
    # changed here since: recorded again, to be confirmed again, not deleted
    put(f"{A}/2.jpg", b"z" * 3000)
    assert offload.finish(D, server.q, still, print) == 0 and not row(A)["confirmed"]
    assert server.q("SELECT bytes FROM offload_files WHERE file=?", f"{A}/2.jpg", one=True)["bytes"] == 3000
    shutil_copy = (copy / A / "2.jpg")
    shutil_copy.write_bytes(b"z" * 3000)
    os.utime(shutil_copy, (os.stat(D / A / "2.jpg").st_mtime,) * 2)
    offload.check_manifest(D, server.q, offload.read_manifest(offload.manifest_of(copy, [A, B])))
    assert offload.finish(D, server.q, still, print) == 4000 and not list((D / A).iterdir()) and (D / A).is_dir()
    assert offload.away(server.q, A) and row(A)["done"]
    # a manifest that isn't whole isn't read
    text = offload.manifest_of(copy, [A, B])
    assert offload.read_manifest(text.replace("end 3", "end 4")) is None and offload.read_manifest(text.rsplit("\n", 2)[0]) is None
    print("deleted only once a manifest confirms it OK")

    # --- lost: a file the backup lacks, or holds otherwise; your phone told at once; a rotating sample of SHA-1s -------
    sample = offload.sample(server.q, budget=10**9)
    assert sorted(sample) == sorted([f"{A}/1.jpg", f"{A}/2.jpg", B]), sample
    assert offload.sample(server.q, budget=1500) == [B, f"{A}/1.jpg"], "within its budget (5 and 1000 bytes; 2.jpg is 3000)"
    got = offload.check_manifest(D, server.q, offload.read_manifest(offload.manifest_of(copy, [A, B], sample)))
    assert got["hashes"] == 3 and got["lost"] == [], got
    assert not server.q("SELECT 1 FROM offload_files WHERE checked IS NULL"), "each checked"
    first = offload.sample(server.q, budget=1500)
    server.q("UPDATE offload_files SET checked=checked+100 WHERE file=?", first[0], write=True)
    assert offload.sample(server.q, budget=1500) != first, "checked longest ago first: it rotates"
    (copy / B).write_bytes(b"vidxo")    # same size, other contents, its time kept: only its SHA-1 tells
    os.utime(copy / B, (files[B]["mtime"],) * 2)
    (copy / A / "1.jpg").unlink()
    manifest(offload.manifest_of(copy, [A, B], [B]))
    did = server.offload_step()
    assert sorted(did["manifest"]["lost"]) == sorted([A, B]), did
    assert "1 of its 2 file(s) missing" in row(A)["lost_why"] and "other contents" in row(B)["lost_why"], (row(A), row(B))
    assert told and told[-1][1] == 5 and "lost" in told[-1][0], told
    assert server.manifest_state()["received"] and not server.manifest_quiet()
    st = offload.state(D, server.q)
    assert sorted(r["path"] for r in st["lost"]) == sorted([A, B]) and "photos" not in st["away"], st
    assert offload.MANIFEST and A not in (D / offload.WANTED).read_text().split()
    # one offloaded before files were recorded takes what the backup holds as its record; holding nothing, it's lost
    C, E = "projects/cccccccccccc/photos", "projects/eeeeeeeeeeee/photos"
    for p in (C, E):
        server.q("INSERT INTO offloads(path, kind, ref, bytes, marked, done) VALUES(?,?,?,?,?,?)", p, "photos", "x", 10, 1, 2, write=True)
    (copy / C).mkdir(parents=True)
    (copy / C / "c.jpg").write_bytes(b"c")
    got = offload.check_manifest(D, server.q, offload.read_manifest(offload.manifest_of(copy, [C, E])))
    assert got["lost"] == [E] and row(E)["lost_why"] == "the backup holds none of it" and not row(C)["lost"], got
    assert server.q("SELECT file FROM offload_files WHERE path=?", C) == [{"file": f"{C}/c.jpg"}]
    print("lost from the backup, told at once, and SHA-1s by rotation OK")

    # --- no good manifest for more than MANIFEST_HOURS: offloading pauses, and your phone is told once ------------------
    server.settings().update({"storage.cap_gb": 0.000001, "storage.offload_percent": 1.0})
    server.measure_usage()
    later = time.time() + (server.MANIFEST_HOURS + 1) * 3600
    assert server.manifest_quiet(later) and "no manifest from the backup for 27 h" in server.manifest_quiet(later)
    assert not server.manifest_quiet(time.time() + (server.MANIFEST_HOURS - 1) * 3600)
    real_time = time.time
    told.clear()
    server.time.time = lambda: later
    try:
        did = server.offload_step()
        assert "paused" in did and not did.get("freed"), did
        server.offload_step()
    finally:
        server.time.time = real_time
    assert [t for t in told if "no word" in t[0]] == [("BoulderMesh: no word from the backup", 4)], told
    did = server.offload_step()
    assert "paused" not in did, "back once there's word"
    # before any manifest: since this server began waiting for one
    server.MANIFEST_DONE.unlink()
    server.MANIFEST_WAIT.write_text(str(time.time() - 30 * 3600))
    assert "yet" in server.manifest_quiet()
    server.MANIFEST_WAIT.write_text(str(time.time()))
    server.settings().update({"storage.cap_gb": 0, "storage.offload_percent": 70.0})
    print("a quiet backup pauses offloading OK")

    # --- a job needing data the backup lost is blocked, listed for the admin page, and goes on only when approved ------
    for t in ("campaigns", "campaign_items", "projects", "meshes", "mesh_links", "climbs", "offloads", "offload_files"):
        server.q(f"DELETE FROM {t}", write=True)
    head = 'Route,Location,URL,"Avg Stars","Your Stars","Route Type",Rating,Pitches,Length,"Area Latitude","Area Longitude"\n'
    csv = head + "".join(f'{n},"{n} Boulder > Sector > JT > California",https://www.mountainproject.com/route/{i}/{n.lower()},3,-1,Boulder,V1,,,34,-116\n'
                         for i, n in ((1, "Beta"), (2, "Gamma")))
    c = server.create_campaign("", "popularity", csv)
    for _ in range(5):
        server.next_campaign_step()
    items = lambda: {i["name"]: i for i in server.q("SELECT * FROM campaign_items WHERE campaign_id=?", c["id"])}  # noqa: E731
    got = items()
    assert got["Beta"]["status"] == got["Gamma"]["status"] == "done", got
    meshes = {n: got[n]["mesh_id"] for n in got}
    for n in ("Beta", "Gamma"):     # their photos offloaded, and lost by the backup
        path = f"projects/{got[n]['project_id']}/photos"
        server.q("INSERT INTO offloads(path, kind, ref, bytes, marked, done, lost, lost_why) VALUES(?,?,?,?,?,?,?,?)", path, "photos",
                 got[n]["project_id"], 3, 1, 2, time.time(), "1 of its 1 file(s) missing", write=True)
        for f in (server.PROJECTS / got[n]["project_id"] / "photos").iterdir():
            f.unlink()
    try:
        server.need_back([got["Beta"]["project_id"]], "meshing it")
        raise AssertionError("lost")
    except server.LostData as e:
        assert not isinstance(e, server.compute.ComputeUnavailable) and "lost from the backup" in str(e) and e.paths, e
    assert not (D / offload.WANTED).exists() or not (D / offload.WANTED).read_text().strip(), "not asked for back"
    server.queue_redo(c["id"], "rebuild")
    harvested.clear()
    for _ in range(4):
        server.next_campaign_step()
    got = items()
    assert all(got[n]["blocked"] and got[n]["status"] == "pending" for n in got), got
    assert "lost from the backup" in got["Beta"]["note"] and server.campaign_status(c["id"]) == "running"
    assert {n: got[n]["mesh_id"] for n in got} == meshes and not server.q("SELECT 1 FROM meshes WHERE replaced_by IS NOT NULL"), \
        "nothing replaced"
    assert server.ordered_pending(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True)) == [], "not taken"
    server.next_campaign_step()
    assert "blocked, as what they need was lost" in server.q("SELECT note FROM campaigns WHERE id=?", c["id"], one=True)["note"]
    view = server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))
    assert view["blocked"] == 2, view["blocked"]
    blocks = server.lost_blocks()
    assert sorted(b["what"] for b in blocks) == [f"{c['name']}: Beta", f"{c['name']}: Gamma"] and blocks[0]["paths"][0]["kind"] == "photos", blocks
    assert server.admin_state()["lost_blocks"] == blocks
    # re-mesh without it: Beta goes on, and its new mesh takes over
    beta = next(b for b in blocks if b["what"].endswith("Beta"))
    assert "goes on" in server.admin_lost_decide({"verdict": "remesh", "campaign": beta["campaign"], "source": beta["source"]})["result"]
    for _ in range(3):
        server.next_campaign_step()
    got = items()
    assert not got["Beta"]["blocked"] and got["Beta"]["status"] == "done" and got["Beta"]["mesh_id"] != meshes["Beta"], got["Beta"]
    assert server.q("SELECT replaced_by FROM meshes WHERE id=?", meshes["Beta"], one=True)["replaced_by"] == got["Beta"]["mesh_id"]
    # leave it as it is: Gamma back as it was, its mesh current
    gamma = next(b for b in server.lost_blocks() if b["what"].endswith("Gamma"))
    server.lost_decide("leave", gamma["campaign"], gamma["source"])
    got = items()
    assert (got["Gamma"]["status"], got["Gamma"]["blocked"], got["Gamma"]["redo"], got["Gamma"]["mesh_id"]) == ("done", None, None, meshes["Gamma"]), got
    assert "left as it was" in got["Gamma"]["note"] and server.lost_blocks() == []
    try:
        server.lost_decide("maybe", gamma["campaign"], gamma["source"])
        raise AssertionError("only remesh or leave")
    except ValueError:
        pass
    # a job outside an area: listed by its path, and approving lets the next try go on
    gpath = f"projects/{got['Gamma']['project_id']}/photos"
    try:
        server.need_back([got["Gamma"]["project_id"]], "finding its beta")
        raise AssertionError("lost")
    except server.LostData:
        pass
    assert [(b.get("path"), b["what"]) for b in server.lost_blocks()] == [(gpath, "finding its beta")]
    server.lost_decide("remesh", path=gpath)
    server.need_back([got["Gamma"]["project_id"]], "finding its beta")      # goes on: nothing to wait for
    assert server.lost_blocks() == [] and not offload.away(server.q, gpath)
    print("lost data blocks a job until approved OK")

    # --- ahead of time: what's still to go whose photos are offloaded --------------------------------------------------
    server.q("DELETE FROM offloads", write=True)
    pid = got["Beta"]["project_id"]
    server.q("INSERT INTO offloads(path, kind, ref, bytes, marked, done) VALUES(?,?,?,?,?,?)", f"projects/{pid}/photos", "photos", pid, 3, 1, 2,
             write=True)
    server.q("UPDATE campaign_items SET status='pending' WHERE name='Beta'", write=True)
    view = server.campaign_view(server.q("SELECT * FROM campaigns WHERE id=?", c["id"], one=True))
    assert view["needs_backup"] == 1, view["needs_backup"]
    assert server._needs_backup([got["Beta"]["climb_id"], got["Gamma"]["climb_id"]]) == 1
    print("needs data from the backup OK")
    print("ALL BACKUP CHECKS TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
