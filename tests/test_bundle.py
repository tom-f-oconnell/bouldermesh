#!/usr/bin/env python3
"""Moving a boulder's mesh, and the harvests behind it, from a copy of the data to another (bundle.py), through
cli.py as it's run. Offline."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
tmp = pathlib.Path(tempfile.mkdtemp(prefix="bouldermesh-bundle-"))
src, dest = tmp / "copy", tmp / "server"
PATH = ["California", "Joshua Tree National Park", "Outback", "Dino's Egg"]
STUB = {"raw": {"stub": True}}


def py(data: pathlib.Path, code: str) -> str:
    """Run code with the server module on a data folder, as a separate process: each reads DATA_DIR once."""
    head = f"import sys, json, time; sys.path.insert(0, {str(ROOT / 'backend')!r})\nimport server as S\nS.init_db()\n"
    r = subprocess.run([sys.executable, "-c", head + textwrap.dedent(code)], env={**os.environ, "DATA_DIR": str(data)},
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout


def cli(*args) -> str:
    r = subprocess.run([sys.executable, str(ROOT / "cli.py"), *args], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout


def climb(name: str, sid: str, data: dict) -> str:
    return (f"S.upsert_climb(S.Climb.from_dict({{'source': 'mountainproject', 'source_id': {sid!r}, 'url': 'u', 'name': {name!r}, "
            f"'area_path': {PATH!r}, 'boulder_name': \"Dino's Egg\", 'lat': 34.0, 'lng': -116.0, **{data!r}}}))")


# the server: two climbs on the boulder from an area's list, one with a mesh of its own; the copy is made from it
py(dest, f"""
a = {climb("West Face", "1", {"images": [{"url": "x", "kind": "image"}]})}
b = {climb("Egg Timer", "2", STUB)}
S.q("INSERT INTO campaigns(id,name,status) VALUES('c1','Outback','done')", write=True)
for sid, cid in (("1", a), ("2", b)):
    S.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,boulder,climb_id,status) VALUES('c1',?,1,'x',?,?,'done')",
        sid, S.boulder_key({PATH!r}), cid, write=True)
S.q("INSERT INTO meshes(id,name,filename,source,created,size,demo) VALUES('m0','West Face','mesh.glb','upload',1,1,0)", write=True)
S.link_mesh("m0", a, "linked at upload")
""")
(tmp / "copy").mkdir()
for f in ("bouldermesh.sqlite",):
    (src / f).write_bytes((dest / f).read_bytes())
made = py(src, "print(time.time())").strip()

# the copy: the stub climb's page read and harvested, a climb the server hasn't seen, and the boulder's own mesh
out = py(src, f"""
ids = {{r["name"]: r["id"] for r in S.q("SELECT id, name FROM climbs")}}
S.upsert_climb(S.Climb.from_dict({{'source': 'mountainproject', 'source_id': '2', 'url': 'u', 'name': 'Egg Timer', 'area_path': {PATH!r},
               'lat': 34.0, 'lng': -116.0, 'images': [{{'url': 'y', 'kind': 'image'}}]}}))
c = {climb("Dino's Egg", "3", {})}
S.q("UPDATE climbs SET id='zz' WHERE id=?", c, write=True)   # a climb first seen here gets an id the server may give another
boulder = S.boulder_of({PATH!r})
for pid, cid in (("h1", ids["West Face"]), ("h2", ids["Egg Timer"]), ("h3", "zz")):
    (S.PROJECTS / pid / "photos").mkdir(parents=True)
    (S.PROJECTS / pid / "photos" / f"mountainproject_{{pid}}.jpg").write_bytes(pid.encode() * 100)
    (S.PROJECTS / pid / "work").mkdir()
    (S.PROJECTS / pid / "work" / "big.bin").write_bytes(b"x" * 1000)
    S.q("INSERT INTO projects(id,name,created,kind,climb_id,status,harvest) VALUES(?,?,?,?,?,'harvested','{{}}')", pid, pid, time.time(),
        "harvest", cid, write=True)
pid = S.boulder_project_made(boulder)
for h in ("h1", "h2", "h3"):     # pooled photos are links to the harvests' own
    import os; os.link(S.PROJECTS / h / "photos" / f"mountainproject_{{h}}.jpg", S.PROJECTS / pid / "photos" / f"mountainproject_{{h}}.jpg")
S.q("INSERT INTO meshes(id,name,filename,source,created,size,demo,boulder) VALUES('bm1',?,'mesh.glb',?,?,1,0,?)",
    "Dino's Egg (boulder)", f"project:{{pid}}", time.time(), boulder, write=True)
S.q("UPDATE projects SET mesh_id='bm1', status='done' WHERE id=?", pid, write=True)
(S.MESHES / "bm1").mkdir(parents=True)
(S.MESHES / "bm1" / "mesh.glb").write_bytes(b"glb")
(S.MESHES / "bm1" / "views.json").write_text(json.dumps({{"climbs": {{"zz": {{"eye": [1, 0, 0], "target": [0, 0, 0], "photos": 1}}}}}}))
S.q("INSERT INTO videos(url,title) VALUES('v1','Egg Timer V4')", write=True)
S.q("INSERT INTO climb_videos(climb_id,url,found_by,created) VALUES(?, 'v1', 'search', 1)", ids["Egg Timer"], write=True)
print(pid)
""")
boulder_pid = out.strip()

bundle = tmp / "b.tar"
assert "exported 1 boulder(s)" in cli("export-boulders", str(bundle), "--boulder", "Dino's Egg", "--since", made, "--data", str(src))
with tarfile.open(bundle) as tar:
    names = tar.getnames()
    rows = json.load(tar.extractfile("manifest.json"))["rows"]
assert not any("/work" in n for n in names), "work folders stay behind"
assert {p["id"] for p in rows["projects"]} == {"h1", "h2", "h3", boulder_pid} and [m["id"] for m in rows["meshes"]] == ["bm1"], rows
assert "meshes/bm1/views.json" in names and f"projects/{boulder_pid}/photos/mountainproject_h3.jpg" in names

assert "imported" in cli("import-bundle", str(bundle), "--data", str(dest))
db = sqlite3.connect(dest / "bouldermesh.sqlite")
db.row_factory = sqlite3.Row
one = lambda sql, *a: db.execute(sql, a).fetchone()  # noqa: E731
ids = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM climbs")}
assert set(ids) == {"West Face", "Egg Timer", "Dino's Egg"}, ids
assert one("SELECT climb_id FROM projects WHERE id='h3'")["climb_id"] == ids["Dino's Egg"]
assert "stub" not in json.loads(one("SELECT data FROM climbs WHERE id=?", ids["Egg Timer"])["data"]).get("raw", {}), "its page, read in the copy"
assert one("SELECT boulder FROM meshes WHERE id='bm1'")["boulder"] == json.dumps(PATH)
assert one("SELECT kind, boulder, mesh_id FROM projects WHERE id=?", boulder_pid)["mesh_id"] == "bm1"
assert one("SELECT project_id FROM campaign_items WHERE source_id='2'")["project_id"] == "h2", "the area's climb takes its harvest"
assert one("SELECT project_id FROM campaign_items WHERE source_id='1'")["project_id"] == "h1"
assert one("SELECT climb_id FROM climb_videos WHERE url='v1'")["climb_id"] == ids["Egg Timer"]
assert one("SELECT mesh_id FROM mesh_links WHERE climb_id=?", ids["West Face"])["mesh_id"] == "m0", "what was there stays"
assert (dest / "projects" / boulder_pid / "photos" / "mountainproject_h3.jpg").read_bytes() == b"h3" * 100
assert (dest / "projects" / "h1" / "photos" / "mountainproject_h1.jpg").exists() and not (dest / "projects" / "h1" / "work").exists()
assert json.loads((dest / "meshes" / "bm1" / "views.json").read_text())["climbs"]
assert not [p for p in dest.iterdir() if p.name.startswith("import-")], "its staging folder is gone"

# the server shows it: the climb's card has its boulder's mesh
card = json.loads(py(dest, f"print(json.dumps(S.climb_row({ids['Egg Timer']!r}, full=True)['boulder']))"))
assert [m["id"] for m in card["meshes"]] == ["bm1"] and {c["name"] for c in card["climbs"]} == {"West Face", "Dino's Egg"}, card

# again: nothing new
again = cli("import-bundle", str(bundle), "--data", str(dest))
assert "nothing new" in again, again
shutil.rmtree(tmp)
print("ALL BUNDLE TESTS PASSED")
