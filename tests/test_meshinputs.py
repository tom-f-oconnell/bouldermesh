#!/usr/bin/env python3
"""What each mesh was made from (meshinputs.py), and the dry run of whether it would be made from the same now
(server.inputs_check()), on a climb's, a boulder's and a pool's meshes built here. Offline: the reconstruction is
stubbed, and the off-topic filter's cache is filled by hand."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = pathlib.Path(tempfile.mkdtemp(prefix="bouldermesh-inputs-"))
os.environ["DATA_DIR"] = str(tmp)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import meshinputs  # noqa: E402
import pipeline  # noqa: E402
import server  # noqa: E402
from climbs import relevance  # noqa: E402
from climbs.common import Climb  # noqa: E402

relevance.CACHE_FILE = tmp / "cache" / "relevance.sqlite"


def fake_reconstruct(images, work, out_dir, log, use_gpu=False, internet=False, tracker=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mesh.ply").write_text("ply")
    return out_dir / "mesh.ply", False


pipeline.reconstruct = fake_reconstruct
server.pipeline.steps = lambda: []
server.queue_pack = server.queue_scale = server.queue_splat = lambda *a, **k: None
server.init_db()
log = lambda s: None  # noqa: E731
P = server.PROJECTS


def jpeg(path: pathlib.Path, seed: int) -> None:
    """A photo dedupe keeps: big enough, and unlike any other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    Image.fromarray(rng.integers(0, 255, (60, 60, 3), dtype=np.uint8)).resize((480, 480)).save(path, quality=80)


SEED = iter(range(1000))


def climb(name: str, sid: str, path: list[str]) -> str:
    return server.upsert_climb(Climb(source="mountainproject", source_id=sid, url=f"https://mp/route/{sid}/{name}", name=name,
                                     kind="boulder", area_path=path))


def report(kept: list[str], rejected: dict | None = None) -> dict:
    return {"version": relevance.VERSION, "settings": relevance.settings_key(), "methods": list(relevance.METHODS),
            "kept": kept, "rejected": rejected or {}, "scores": {}, "fallback": False}


def harvest(cid: str, own: list[str], web: list[str], nomatch: tuple[str, ...] = ()) -> str:
    """A climb's harvest: its own photos, web photos the filter kept, and some it found matching nothing."""
    pid = server.new_id()
    server.q("INSERT INTO projects(id,name,created,kind,climb_id,status,harvest) VALUES(?,?,?,?,?,?,?)", pid, f"{pid} (harvest)",
             time.time(), "harvest", cid, "done", json.dumps({"images": len(own) + len(web)}), write=True)
    for n in own + web:
        jpeg(P / pid / "photos" / n, next(SEED))
    for n in nomatch:
        jpeg(P / pid / "rejected" / f"nomatch_{n}", next(SEED))
    (P / pid / "relevance.json").write_text(json.dumps(report(own + web, {n: "nomatch" for n in nomatch})))
    (P / pid / "sources.json").write_text(json.dumps({"files": [{"file": n} for n in own + web + list(nomatch)], "created": time.time()}))
    return pid


def pooled(key: str, climbs: dict[str, str], reject: tuple[str, ...] = ()) -> str:
    """A boulder's or a pool's project, pooled from these climbs' harvests ({climb: harvest}) as run_boulder() does,
    with what its filter rejected."""
    pid = server.new_id()
    server.q("INSERT INTO projects(id,name,created,kind,boulder,status) VALUES(?,?,?,?,?,?)", pid, f"{pid} (boulder)", time.time(),
             "boulder", key, "new", write=True)
    pdir = P / pid
    (pdir / "photos").mkdir(parents=True)
    record = [{"id": c, "name": c, "projects": [h], "own": sorted(server.viewpoints.own_photos(P / h))} for c, h in climbs.items()]
    (pdir / server.CLIMBS_FILE).write_text(json.dumps({"boulder": key, "climbs": record, "created": time.time()}))
    added = server.harvest_mod.pool(pdir, [P / h for h in climbs.values()], log)
    (pdir / "sources.json").write_text(json.dumps({"files": added, "created": time.time()}))
    (pdir / "rejected").mkdir()
    for n in reject:
        (pdir / "photos" / n).rename(pdir / "rejected" / f"nomatch_{n}")
    names = sorted(p.name for p in (pdir / "photos").iterdir())
    (pdir / "relevance.json").write_text(json.dumps(report(names, {n: "nomatch" for n in reject})))
    return pid


def check() -> dict[str, dict]:
    got = server.inputs_check()
    return {m["id"]: {**m, "status": status} for status in ("changed", "unknown", "same") for m in got[status]}


# --- a boulder (Rock) with two climbs, and an area that isn't one (Gully) with three ---------------------------------
ROCK, GULLY = json.dumps(["Area", "Rock"]), json.dumps(["Area", "Gully"])
before = time.time() - 100
server.q("INSERT INTO boulders(key,name,kind,why,checked) VALUES(?,?,?,?,?)", ROCK, "Rock", "boulder", "test", before, write=True)
server.q("INSERT INTO boulders(key,name,kind,why,checked) VALUES(?,?,?,?,?)", GULLY, "Gully", "group", "test", before, write=True)
A, B = climb("Alpha", "1", ["Area", "Rock"]), climb("Beta", "2", ["Area", "Rock"])
C, D, E = (climb(n, s, ["Area", "Gully"]) for n, s in (("Cee", "3"), ("Dee", "4"), ("Eee", "5")))
hA = harvest(A, ["mp_a1.jpg", "mp_a2.jpg", "mp_a3.jpg"], ["ddg_a1.jpg", "ddg_a2.jpg"], ("ddg_a3.jpg",))
hB = harvest(B, ["mp_b1.jpg", "mp_b2.jpg", "mp_b3.jpg"], ["ddg_b1.jpg"])
hC, hD, hE = (harvest(c, [f"mp_{c}{i}.jpg" for i in range(3)], []) for c in (C, D, E))
server.link(C, D, 3, "photos")      # of one rock
server.link(C, E, 0, "photos")      # compared, and not
server.link(D, E, 0, "photos")

# --- a mesh records what it was made from, as it's made ---------------------------------------------------------
mA = server.run_reconstruct(hA)
rec = meshinputs.load(server.MESHES / mA)
assert rec and rec["kind"] == "climb" and rec["project"] == hA and rec["mesh"] == mA, rec
assert sorted(rec["photos"]["files"]) == ["ddg_a1.jpg", "ddg_a2.jpg", "mp_a1.jpg", "mp_a2.jpg", "mp_a3.jpg"], rec["photos"]
assert rec["photos"]["files"]["mp_a1.jpg"][1] == hashlib.sha1((P / hA / "photos" / "mp_a1.jpg").read_bytes()).hexdigest()
assert rec["filter"] == {"version": relevance.VERSION, "settings": relevance.settings_key(), "methods": sorted(relevance.METHODS)}, rec
assert rec["meshopts"] == server.meshopts.MAPPER and rec["mesh_rules"] == server.MESH_RULES and not rec["unknown"], rec
assert "leaf" not in rec and "pool" not in rec, "a climb's own mesh has no pooling"
fp = rec["fingerprint"]
assert fp and meshinputs.finish(server.inputs_record(hA))["fingerprint"] == fp, "the same inputs, the same fingerprint"
jpeg(P / hA / "photos" / "mp_a1.jpg", 999)
assert meshinputs.finish(server.inputs_record(hA))["fingerprint"] != fp, "a photo's contents are in it"
shutil.copy2(server.MESHES / mA / "inputs.json", tmp / "a.json")
jpeg(P / hA / "photos" / "mp_a1.jpg", 0)    # as it was
assert meshinputs.finish(server.inputs_record(hA))["fingerprint"] == fp

kB = pooled(ROCK, {A: hA, B: hB}, reject=("ddg_a3.jpg",))
mB = server.run_reconstruct(kB)
rec = meshinputs.load(server.MESHES / mB)
assert rec["kind"] == "boulder" and rec["leaf"] == ROCK and rec["leaf_kind"] == "boulder", rec
assert rec["climbs"] == {A: [hA], B: [hB]} and "ddg_a3.jpg" in rec["pooled"] and "ddg_a3.jpg" not in rec["photos"]["files"], rec
assert len(rec["pooled"]) == 10 and rec["photos"]["n"] == 9 and rec["fingerprint"], rec
pool_key = server.pool_for(GULLY, [C, D])
kP = pooled(pool_key, {C: hC, D: hD})
mP = server.run_reconstruct(kP)
rec = meshinputs.load(server.MESHES / mP)
assert rec["kind"] == "pool" and rec["pool"] == sorted([C, D]) and rec["leaf"] == GULLY and rec["leaf_kind"] == "group", rec
brief = meshinputs.brief(rec)
assert "files" not in brief["photos"] and len(brief["photos"]["names"]) == 6, brief
print("recording OK")

# --- the dry run: nothing changed, all the same ----------------------------------------------------------------------
got = check()
assert {m: got[m]["status"] for m in (mA, mB, mP)} == {mA: "same", mB: "same", mP: "same"}, got
assert json.loads(server.INPUTS_CHECK.read_text())["counts"] == {"changed": 0, "unknown": 0, "same": 3}

# a photo added to a climb's harvest: the boulder would pool it, and only the cache could say whether the filter keeps it
jpeg(P / hB / "photos" / "ddg_b2.jpg", next(SEED))
got = check()
assert got[mB]["status"] == "changed" and got[mB]["changed"]["pooled"] == {"added": ["ddg_b2.jpg"], "removed": []}, got[mB]
assert "CLIP" in got[mB]["unknown"]["photos"], got[mB]
assert got[mA]["status"] == "same" and got[mP]["status"] == "same", got
(P / hB / "photos" / "ddg_b2.jpg").unlink()

# a photo the climb's filter rejects now: its own mesh changes; the boulder pools it all the same, for its own filter
(P / hA / "photos" / "ddg_a2.jpg").rename(P / hA / "rejected" / "nomatch_ddg_a2.jpg")
got = check()
assert got[mA]["status"] == "changed" and got[mA]["changed"] == {"photos": {"added": [], "removed": ["ddg_a2.jpg"], "contents": []}}, got[mA]
assert got[mB]["status"] == "same", got[mB]
(P / hA / "rejected" / "nomatch_ddg_a2.jpg").rename(P / hA / "photos" / "ddg_a2.jpg")
# a photo's contents changed
jpeg(P / hA / "photos" / "mp_a2.jpg", 998)
got = check()
assert got[mA]["changed"]["photos"]["contents"] == ["mp_a2.jpg"] and got[mB]["changed"]["photos"]["contents"] == ["mp_a2.jpg"], got
jpeg(P / hA / "photos" / "mp_a2.jpg", 1)

# the pool's climbs: one more of them matches now
server.link(D, E, 3, "photos")
got = check()
assert got[mP]["status"] == "changed" and got[mP]["changed"]["pool"] == {"added": [E], "removed": []}, got[mP]
assert got[mP]["changed"]["climbs"]["added"] == [E], got[mP]
server.q("UPDATE climb_links SET pairs=0 WHERE a=? AND b=?", *sorted((D, E)), write=True)
# a climb of it never compared with the others: can't tell without matching
server.q("DELETE FROM climb_links WHERE ? IN (a, b)", E, write=True)
got = check()
assert got[mP]["status"] == "unknown" and "matching the photos of 1 climb" in got[mP]["unknown"]["pool"], got[mP]
server.link(C, E, 0, "photos")
# the area read again as one boulder: a boulder's mesh, not this pool
server.q("UPDATE boulders SET kind='boulder' WHERE key=?", GULLY, write=True)
got = check()
assert got[mP]["changed"]["leaf_kind"] == {"was": "group", "now": "boulder"} and got[mP]["changed"]["pool"]["removed"] == sorted([C, D]), got[mP]
server.q("UPDATE boulders SET kind='group' WHERE key=?", GULLY, write=True)
# and the boulder read as a group
server.q("UPDATE boulders SET kind='unknown' WHERE key=?", ROCK, write=True)
got = check()
assert got[mB]["changed"] == {"leaf_kind": {"was": "boulder", "now": "unknown"}}, got[mB]
server.q("UPDATE boulders SET kind='boulder' WHERE key=?", ROCK, write=True)
assert {m["status"] for m in check().values()} == {"same"}
print("dry run OK")

# --- the filter's version and settings ----------------------------------------------------------------------------
relevance.VERSION = 2
got = check()
for m in (mA, mB, mP):
    assert got[m]["status"] == "changed" and got[m]["changed"]["filter"] == {"version": {"was": 1, "now": 2}}, got[m]
relevance.VERSION = 1

# what the cache knows: every photo looks like a boulder; ddg_a1 matches the rock's photos with 20 consistent matches,
# ddg_a2 with 40, and the rest of the web photos with none
store = relevance.cache()
photos = {p.name: p for h in (hA, hB) for p in list((P / h / "photos").iterdir()) + list((P / h / "rejected").glob("*"))}
digest = {n.split("_", 1)[1] if n.startswith("nomatch_") else n: store.digest(p) for n, p in photos.items()}
store.put("clip", [(d, relevance._clip_key(), 0.9) for d in digest.values()])
store.put("keypoints", [(d, relevance._sift_key(), 500) for d in digest.values()])
strong = {"ddg_a1.jpg": 20, "ddg_a2.jpg": 40}
store.put("pairs", [(digest[x], digest[y], relevance._how(), strong.get(x, 0) if relevance.is_trusted(pathlib.Path(y)) else 0, 0.5)
                    for x in digest for y in digest if x != y])
relevance.MIN_INLIERS = 30      # stricter: ddg_a1 no longer passes
got = check()
assert got[mA]["status"] == "changed" and set(got[mA]["changed"]) == {"filter", "photos"}, got[mA]
assert got[mA]["changed"]["filter"]["settings"]["now"] == relevance.settings_key(), got[mA]
assert got[mA]["changed"]["photos"]["removed"] == ["ddg_a1.jpg"], got[mA]
assert got[mB]["changed"]["photos"]["removed"] == ["ddg_a1.jpg", "ddg_b1.jpg"], got[mB]
relevance.MIN_INLIERS = 16
# a new version doesn't make the filter judge photos again: their verdicts stand, so only the filter changed
relevance.VERSION = 2
got = check()
for m in (mA, mB):
    assert set(got[m]["changed"]) == {"filter"} and not got[m]["unknown"], got[m]
relevance.VERSION = 1
print("filter OK")

# --- meshes made before inputs.json: what their projects still tell ----------------------------------------------
for m in (mA, mB):
    (server.MESHES / m / "inputs.json").unlink()
for p in (P / hA / "photos").iterdir():
    os.utime(p, (before, before))
os.utime(P / hA / "photos", (before, before))
r = json.loads((P / hA / "relevance.json").read_text())
del r["version"]    # as the filter's reports were before it was kept
(P / hA / "relevance.json").write_text(json.dumps(r))
os.utime(P / hA / "relevance.json", (before, before))
rec = server.inputs_of(mA)
assert rec["backfilled"] and rec["photos"]["n"] == 5 and rec["filter"]["version"] is None and rec["fingerprint"] is None, rec
assert set(rec["unknown"]) == {"meshopts", "filter.version"} and rec["meshopts"] is None, rec["unknown"]
rec = server.inputs_of(mB)
assert rec["climbs"] == {A: [hA], B: [hB]} and len(rec["pooled"]) == 10 and rec["leaf_kind"] == "boulder", rec
assert set(rec["unknown"]) == {"meshopts"}, rec["unknown"]
got = check()
assert got[mA]["status"] == "unknown" and set(got[mA]["unknown"]) == {"meshopts", "filter.version"}, got[mA]
assert got[mB]["status"] == "unknown" and set(got[mB]["unknown"]) == {"meshopts"}, got[mB]
# photos written after it was made, its area's page read again since, its climbs pooled again since: unknown
(server.MESHES / mA / "inputs.json").unlink()
(server.MESHES / mB / "inputs.json").unlink()
jpeg(P / hA / "photos" / "mp_a3.jpg", 997)
server.q("UPDATE boulders SET checked=? WHERE key=?", time.time() + 10, ROCK, write=True)
c = json.loads((P / kB / server.CLIMBS_FILE).read_text())
(P / kB / server.CLIMBS_FILE).write_text(json.dumps({**c, "created": time.time() + 10}))
rec = server.inputs_of(mA)
assert rec["photos"] is None and "changed after" in rec["unknown"]["photos"], rec
rec = server.inputs_of(mB)
assert set(rec["unknown"]) == {"meshopts", "leaf_kind", "climbs", "photos"} and rec["climbs"] is None and rec["leaf_kind"] is None, rec["unknown"]
assert len(rec["pooled"]) == 10, "what was pooled, recorded before it was made, is still known"
got = check()
assert got[mA]["status"] == "unknown" and "photos" in got[mA]["unknown"], got[mA]
# a project gone
shutil.rmtree(P / hB)
server.q("DELETE FROM projects WHERE id=?", hB, write=True)
got = check()
assert got[mB]["status"] == "changed" and "mp_b1.jpg" in got[mB]["changed"]["pooled"]["removed"], got[mB]
assert any("never harvested" in n for n in got[mB]["notes"]), got[mB]
# and a mesh whose record lost everything: a changed photo still says it changed
(server.MESHES / mA / "inputs.json").write_text((tmp / "a.json").read_text())
got = check()
assert got[mA]["status"] == "changed" and got[mA]["changed"]["photos"]["contents"] == ["mp_a3.jpg"], got[mA]
print("backfill OK")
shutil.rmtree(tmp, ignore_errors=True)
print("ALL MESH INPUTS TESTS PASSED")
