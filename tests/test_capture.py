#!/usr/bin/env python3
"""What the phone apps are given of a mesh (captureplan.py), and what a mesh learns from the photos they take
(phonedata.py): offline, on a made-up rock."""
from __future__ import annotations

import json
import math
import os
import pathlib
import struct
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = pathlib.Path(tempfile.mkdtemp(prefix="bouldermesh-capture-"))
os.environ["DATA_DIR"] = str(tmp)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
import captureplan  # noqa: E402
from capture_fixture import camera, cameras, wall  # noqa: E402
import orientation  # noqa: E402
import phonedata  # noqa: E402
import scale  # noqa: E402

rng = np.random.default_rng(7)


# --- a wall of rock: part of a cylinder 2 units in radius and 3 tall, facing +Z, open at its sides and top -------------
V, F = wall()
C, N, area = captureplan.geometry(V, F)
assert float((N * C * [1, 0, 1]).sum(axis=1).min()) > 0, "its faces face outward"
cams = cameras()
best, views = captureplan.coverage(C, N, cams)
front = np.abs(np.arctan2(C[:, 0], C[:, 2])) < 0.4
assert np.isfinite(best[front]).all() and views[front].min() >= 2, "what faces the cameras is seen, from more than one direction"
# 4 units off, 3000 pixels of focal length: about 1.3 thousandths of a unit a pixel, more where it's seen at a slant
assert 0.0011 < float(np.median(best[front])) < 0.0018, float(np.median(best[front]))
behind = [camera("back.jpg", [0, 1.5, -6], [0, 1.5, 0])]
assert not np.isfinite(captureplan.coverage(C, N, behind)[0]).any(), "nothing is seen from behind: its faces face away"
print("coverage OK")

holds = [{"p": [0.0, 1.0, 2.0], "role": "start", "from": "video", "climbs": ["c1"]},
         {"p": [0.02, 1.0, 2.0], "role": "start", "from": "marked", "climbs": []},        # the same hold, marked too
         {"p": [0.5, 2.2, 1.936], "role": "finish", "from": "video", "climbs": ["c1"]}]
plan, guide = captureplan.build(V, F, cams, holds, [{"id": "c1", "name": "Alpha", "grade": "V2", "view": None}], 1.2)
T = plan["targets"]
kinds = {k: [t for t in T if t["kind"] == k] for k in ("patch", "hold", "edge")}
assert len(kinds["hold"]) == 2 and all(t["prio"] == 3 and t["climbs"] == ["c1"] for t in kinds["hold"]), kinds["hold"]
assert all(t["have_gsd"] and t["have_views"] >= 2 for t in kinds["hold"]), kinds["hold"]
assert kinds["edge"] and all(t["out"][1] > -0.4 for t in kinds["edge"]), "its open sides and top are edges, its foot isn't"
near = [t for t in kinds["patch"] if t["prio"] == 2]
assert near and all(min(np.linalg.norm(np.array(t["p"]) - np.array(h["p"])) for h in holds) * 1.2 < 1.5 for t in near), "by a hold"
assert any(t["prio"] == 1 for t in kinds["patch"]), "the rest is rock all the same"
assert plan["frame"]["metres_per_unit"] == 1.2 and not plan["frame"]["scale_guessed"] and plan["need"]["hold"]["gsd_mm"] < plan["need"]["patch"]["gsd_mm"]
magic, nv, nf, npatch = struct.unpack("<4sIII", guide[:16])
assert magic == b"BMG1" and (nv, nf) == (len(V), len(F)) and npatch == len(kinds["patch"])
assert len(guide) == 16 + nv * 12 + nf * 12 + nf * 2 + nf * 4 + nf
patch_of = np.frombuffer(guide, "<u2", nf, 16 + nv * 12 + nf * 12)
assert set(patch_of.tolist()) - {65535} == set(range(npatch)), "every patch has faces"
guessed, _ = captureplan.build(V, F, cams, [], [], None)
assert guessed["frame"]["scale_guessed"] and abs(guessed["frame"]["metres_per_unit"] * guessed["frame"]["size"] - captureplan.GUESS_ACROSS_M) < 1e-3
assert all(t["prio"] == 2 for t in guessed["targets"] if t["kind"] == "patch" and t["have_views"]), "no holds: what was seen matters"
big_v, big_f = captureplan.decimate(np.repeat(V, 1, axis=0), np.concatenate([F] * 1), 200)
assert len(big_f) <= 200 and big_f.max() < len(big_v)
print("plans and guides OK")

# --- the same plan and guide, as the apps' own tests read them (app/shared/src/jvmTest) ---------------------------------
# kept in the repository: if these no longer match, the format changed, and the apps have to change with it (app/FORMAT.md)
FIXTURES = ROOT / "app" / "shared" / "src" / "jvmTest" / "resources"
sent = {**plan, "made": 0, "mesh": "ca" * 6, "name": "A Wall", "lat": 34.0, "lng": -116.0, "heading_deg": None,
        "guide_url": f"/api/capture/guide/{'ca' * 6}.bin?v={plan['guide']['sha1']}"}
if "--write-fixtures" in sys.argv:
    (FIXTURES / "server").mkdir(parents=True, exist_ok=True)
    (FIXTURES / "server" / "plan.json").write_text(json.dumps(sent, indent=1, sort_keys=True) + "\n")
    (FIXTURES / "server" / "guide.bin").write_bytes(guide)
assert json.loads((FIXTURES / "server" / "plan.json").read_text()) == json.loads(json.dumps(sent)) and (FIXTURES / "server" / "guide.bin").read_bytes() == guide, \
    "a plan or guide isn't what the apps' tests have: if the format was meant to change, run this with --write-fixtures and change the apps"
# and what the apps write, as their tests check they still do: a photo's readings and a capture's record
theirs = phonedata.parse((FIXTURES / "app" / "shot.txt").read_text())
assert theirs["v"] == 1 and theirs["ar"] == {"s": "a", "p": [1.0, 2.0, 3.0], "q": [1.0, 0.0, 0.0, 0.0], "track": "normal"}, theirs
assert theirs["gps"]["acc"] == 3.0 and theirs["down_src"] == "ar" and theirs["k"]["fx"] == 3000.0 and theirs["q"]["ok"] is True, theirs
assert phonedata.down(theirs) and abs(phonedata.down(theirs)[1] - 0.98 / math.hypot(0.98, 0.2)) < 1e-9
sent_record = json.loads((FIXTURES / "app" / "capture.json").read_text())
assert sent_record["mesh"] == "ca" * 6 and sent_record["shots"][0]["name"] == "cap_k1_001.jpg" and sent_record["shots"][0]["blob"] == theirs, sent_record
print("what the apps read and write OK")

# --- a photo's readings, in its own file --------------------------------------------------------------------------------
photos = tmp / "photos"
photos.mkdir()


def save(name: str, blob: dict | None, code: bytes = b"ASCII\0\0\0") -> pathlib.Path:
    exif = Image.Exif()
    if blob is not None:
        exif.get_ifd(phonedata.EXIF_IFD)[phonedata.USER_COMMENT] = code + phonedata.MARK + json.dumps(blob).encode()
    Image.new("RGB", (64, 48), (90, 90, 90)).save(photos / name, exif=exif)
    return photos / name


p = save("cap_000.jpg", {"v": 1, "down": [0.0, 0.98, 0.2], "ar": {"s": "a", "p": [1, 2, 3]}})
assert phonedata.read(p)["ar"]["p"] == [1, 2, 3] and phonedata.read(save("plain.jpg", None)) is None
assert phonedata.parse("BMSH1:{\"down\": [0, 1, 0]}\0\0") == {"down": [0, 1, 0]} and phonedata.parse(b"BMSH1:not json") is None
assert phonedata.parse(b"UNICODE\0" + 'BMSH1:{"down": [0, 1, 0]}'.encode("utf-16-le")) == {"down": [0, 1, 0]}, "a comment written two bytes a character"
r = orientation.reading(p)
assert r["from"] == "app" and abs(np.linalg.norm(r["down"]) - 1) < 1e-9 and abs(r["pitch"] + math.degrees(math.asin(0.2 / math.hypot(0.98, 0.2)))) < 0.01, r
assert phonedata.down({"down": [0, 0, 0]}) is None and phonedata.down({"down": "x"}) is None and orientation.reading(photos / "plain.jpg") is None
print("readings in photos OK")

# --- how big: the phone's own places against the model's ---------------------------------------------------------------
true_scale, turn = 1.3, 0.7
Ry = np.array([[math.cos(turn), 0, math.sin(turn)], [0, 1, 0], [-math.sin(turn), 0, math.cos(turn)]])
ring = [camera(f"cap_{i:03d}.jpg", [5 * math.sin(a), 1.2 + 0.3 * math.sin(3 * a), 5 * math.cos(a)], [0, 1.5, 0]) for i, a in enumerate(np.linspace(-1.2, 1.2, 14))]
blobs = {}
for i, cam in enumerate(ring):
    c = phonedata._centre(cam)
    blobs[cam["name"]] = {"t": 1000.0 + 12 * i, "ar": {"s": "one", "p": list(true_scale * Ry @ c + [3, 0.1, -2] + rng.normal(0, 0.01, 3)), "track": "normal"}}
blobs[ring[4]["name"]]["ar"]["p"] = [9.0, 9.0, 9.0]      # tracking that jumped
blobs[ring[9]["name"]]["ar"]["track"] = "limited"        # and one it said not to trust
(est,) = phonedata.scale_estimates(ring, blobs)
assert abs(est["metres_per_unit"] / true_scale - 1) < 0.01 and est["n"] == 12 and est["of"] == 13 and est["sd"] < 0.03, est
assert phonedata.scale_estimates(ring[:3], blobs) == [], "too few"
still = {c["name"]: {"ar": {"s": "s", "p": [0.01 * i, 0, 0]}} for i, c in enumerate(ring)}
assert phonedata.scale_estimates(ring, still) == [], "taken from one spot: nothing to go by"
both = scale.combine([{"kind": "video", "metres_per_unit": 1.6, "sd": 0.12}, est])
assert abs(both["metres_per_unit"] / true_scale - 1) < 0.02 and both["method"] == "phone" and both["sd"] < 0.05, both
print("size from the phone's places OK")

# --- where: their GPS fixes, around it -----------------------------------------------------------------------------------
lat0, lng0, heading = 34.01, -116.16, 40.0
for cam in ring:
    c = phonedata._centre(cam) * true_scale
    h = math.radians(heading)
    east, north = c[0] * math.cos(h) - c[2] * math.sin(h), -c[0] * math.sin(h) - c[2] * math.cos(h)
    east, north = east + rng.normal(0, 1.0), north + rng.normal(0, 1.0)
    blobs[cam["name"]]["gps"] = {"lat": lat0 + math.degrees(north / phonedata.EARTH),
                                 "lng": lng0 + math.degrees(east / (phonedata.EARTH * math.cos(math.radians(lat0)))), "acc": 3.0}
blobs[ring[2]["name"]]["gps"]["lat"] += 0.001      # a fix 110 m off
where = phonedata.position(ring, blobs, true_scale)
off = math.hypot((where["lat"] - lat0) * 111320, (where["lng"] - lng0) * 111320 * math.cos(math.radians(lat0)))
assert off < 1.5 and where["n"] == 13 and where["heading_from"] == "gps", (off, where)
assert abs((where["heading_deg"] - heading + 180) % 360 - 180) < 12, where
close = {k: {**v, "gps": {**v["gps"], "lat": lat0, "lng": lng0}} for k, v in blobs.items()}
told = phonedata.position(ring, close, true_scale, north=heading)
assert told["heading_deg"] is None and told["heading_from"] == "given", "fixes all at one point say where, not which way"
assert phonedata.position(ring[:2], blobs, true_scale) is None
print("place from GPS OK")

# --- the server's: a mesh's plan made, kept, and made again when its holds change ------------------------------------------
import pipeline  # noqa: E402
import server  # noqa: E402

server.init_db()
mid = "ca" * 6
mdir = server.MESHES / mid
mdir.mkdir(parents=True)
pipeline.write_ply(mdir / "mesh.ply", [tuple(v) for v in V], [(120, 110, 100)] * len(V), [tuple(int(i) for i in f) for f in F])
(mdir / "cameras.json").write_text(json.dumps(cams))
server.q("INSERT INTO meshes(id,name,filename,source,created,lat,lng) VALUES(?,?,?,?,?,?,?)", mid, "A Wall", "mesh.ply", "project:x", 1.0,
         34.0, -116.0, write=True)
assert server.capture_plan("nosuchmesh00") is None
first = server.capture_plan(mid)
assert first["mesh"] == mid and first["name"] == "A Wall" and first["lat"] == 34.0 and first["guide_url"].endswith(first["guide"]["sha1"])
assert not [t for t in first["targets"] if t["kind"] == "hold"] and (mdir / "capture" / "guide.bin").exists()
made = (mdir / "capture" / "plan.json").stat().st_mtime_ns
assert server.capture_plan(mid)["made"] == first["made"] and (mdir / "capture" / "plan.json").stat().st_mtime_ns == made, "kept"
(mdir / "holds.json").write_text(json.dumps({"holds": [{"p": [0.0, 1.0, 2.0], "role": "start"}]}))
again = server.capture_plan(mid)
assert [t["role"] for t in again["targets"] if t["kind"] == "hold"] == ["start"] and again["made"] != first["made"], "made again with its hold"
print("the server's plans OK")

# --- the server's: the meshes a capture can add to, and a capture meshed together with the photos of the one it adds to ----
(row,) = server.capture_meshes()
assert row["id"] == mid and row["name"] == "A Wall" and row["holds"] == 1 and row["climbs"] == [], row
server.q("INSERT INTO climbs(id,source,source_id,name,grade,created) VALUES('c1','test','1','Alpha','V2',1.0)", write=True)
server.link_mesh(mid, "c1", None)
assert server.capture_meshes()[0]["climbs"] == ["Alpha V2"]
theirs, mine = "aa" * 6, "bb" * 6
for pid, names in ((theirs, ["mp_1.jpg", "yt_abc_0001.jpg", "notes.txt"]), (mine, ["cap_k1_001.jpg", "cap_k1_002.jpg", "cap_k1_003.jpg"])):
    (server.PROJECTS / pid / "photos").mkdir(parents=True)
    for n in names:
        if n.endswith(".jpg"):
            Image.new("RGB", (32, 24), (80, 80, 80)).save(server.PROJECTS / pid / "photos" / n)
        else:
            (server.PROJECTS / pid / "photos" / n).write_text("not a photo")
    server.q("INSERT INTO projects(id,name,created,kind) VALUES(?,?,?,'user')", pid, pid, 1.0, write=True)
(server.PROJECTS / theirs / "masks").mkdir()
(server.PROJECTS / theirs / "masks" / "mp_1.jpg.png").write_bytes(b"mask")
(server.PROJECTS / theirs / "sources.json").write_text(json.dumps({"files": [{"file": "mp_1.jpg", "source": "mountainproject", "url": "https://example.org/1"}]}))
server.q("UPDATE meshes SET source=? WHERE id=?", f"project:{theirs}", mid, write=True)
said = []
assert server.merge_capture(mine, said.append) is None and not said, "no record: not a capture"
(server.PROJECTS / mine / server.CAPTURE_FILE).write_text(json.dumps({"mesh": "0123456789ab", "shots": []}))
assert server.merge_capture(mine, said.append) is None and "on their own" in said[-1], "a mesh that's gone: meshed on its own"
(server.PROJECTS / mine / server.CAPTURE_FILE).write_text(json.dumps({"mesh": mid, "shots": []}))
assert server.merge_capture(mine, said.append) == mid, said
now = sorted(f.name for f in (server.PROJECTS / mine / "photos").iterdir())
assert now == ["cap_k1_001.jpg", "cap_k1_002.jpg", "cap_k1_003.jpg", "mp_1.jpg", "yt_abc_0001.jpg"], now
assert (server.PROJECTS / mine / "masks" / "mp_1.jpg.png").read_bytes() == b"mask"
merged = json.loads((server.PROJECTS / mine / server.MERGED_FILE).read_text())
assert merged["mesh"] == mid and merged["project"] == theirs and merged["photos"] == ["mp_1.jpg", "yt_abc_0001.jpg"], merged
came = {f["file"]: f for f in json.loads((server.PROJECTS / mine / "sources.json").read_text())["files"]}
assert came["mp_1.jpg"]["url"] == "https://example.org/1" and came["yt_abc_0001.jpg"]["source"] == "youtube", came
assert "3 photos taken with the app" in said[-1] and "2 photos of that mesh's" in said[-1], said[-1]
Image.new("RGB", (32, 24), (80, 80, 80)).save(server.PROJECTS / theirs / "photos" / "mp_2.jpg")
assert server.merge_capture(mine, said.append) == mid and "1 joined just now" in said[-1], said[-1]
assert json.loads((server.PROJECTS / mine / server.MERGED_FILE).read_text())["photos"] == ["mp_1.jpg", "mp_2.jpg", "yt_abc_0001.jpg"]
new_mid = "cb" * 6
(server.MESHES / new_mid).mkdir()
server.q("INSERT INTO meshes(id,name,filename,source,created) VALUES(?,?,?,?,?)", new_mid, "A Wall (capture)", "mesh.ply", f"project:{mine}", 2.0, write=True)
server.link_capture(new_mid, mid, said.append)
(link,) = server.q("SELECT climb_id, note FROM mesh_links WHERE mesh_id=?", new_mid)
assert link["climb_id"] == "c1" and link["note"] == f"{server.CAPTURE_LINK} {mid}", link
assert server.q("SELECT lat, lng FROM meshes WHERE id=?", new_mid, one=True) == {"lat": 34.0, "lng": -116.0}, "where the mesh it adds to is, until its photos say"
assert server.q("SELECT replaced_by FROM meshes WHERE id=?", mid, one=True)["replaced_by"] is None, "the mesh added to stays"
print("captures meshed with the mesh they add to OK")

# --- the server's: a mesh put where its app photos' fixes say ---------------------------------------------------------------
assert server.place_from_phone(new_mid) is None, "no size found yet: nothing to go by"
scale.save(server.MESHES / new_mid, {"status": "done", "metres_per_unit": 1.2, "phone": {"photos": 9, "position": {"lat": 34.0001, "lng": -116.0002, "acc_m": 9.0, "n": 9}}})
assert server.place_from_phone(new_mid) is None, "fixes that only say to within 9 m don't move it"
scale.save(server.MESHES / new_mid, {"status": "done", "metres_per_unit": 1.2, "phone": {"photos": 9, "position": {"lat": 34.0001, "lng": -116.0002, "acc_m": 1.4, "n": 9}}})
put = server.place_from_phone(new_mid, said.append)
assert put["was"] == [34.0, -116.0] and 20 < put["moved_m"] < 24 and "21 m from" in said[-1].replace("22 m", "21 m"), (put, said[-1])
assert server.q("SELECT lat, lng FROM meshes WHERE id=?", new_mid, one=True) == {"lat": 34.0001, "lng": -116.0002}
assert scale.load(server.MESHES / new_mid)["phone"]["placed"]["was"] == [34.0, -116.0] and server.place_from_phone(new_mid) is None, "once"
print("placed by its app photos OK")
print("ALL CAPTURE TESTS PASSED")
