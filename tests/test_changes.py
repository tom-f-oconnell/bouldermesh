#!/usr/bin/env python3
"""Who changed what (the changes table), and putting it back from the admin page (revert_change()): a line drawn by
hand, a climb's and a mesh's name and place, and which version of a mesh is current. In-process, with no jobs run;
test_accounts.py covers facings and sets of beta through a running server, and test_api.py a cleanup.

    python3 tests/test_changes.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = tempfile.mkdtemp(prefix="bouldermesh-changes-")
os.environ["DATA_DIR"] = tmp

import beta  # noqa: E402
import betalines  # noqa: E402
import server  # noqa: E402

server.init_db()
ANA = {"id": "a1", "name": "Ana", "role": "editor"}
q = server.q


def mesh(mid: str, replaced_by: str | None = None) -> None:
    (server.MESHES / mid).mkdir(parents=True, exist_ok=True)
    q("INSERT INTO meshes(id,name,filename,source,created,lat,lng) VALUES(?,?,?,?,?,?,?)", mid, f"Mesh {mid}", "mesh.glb", "upload", 1.0, 34.0, -116.0, write=True)
    if replaced_by:
        q("UPDATE meshes SET replaced_by=? WHERE id=?", replaced_by, mid, write=True)


def last(kind: str) -> dict:
    return q("SELECT * FROM changes WHERE kind=? ORDER BY id DESC LIMIT 1", kind, one=True)


mesh("aaaaaaaaaaaa")
cid = q("INSERT INTO climbs(id,source,source_id,url,name,kind,grade,lat,lng) VALUES('c0ffee000001','manual','1','','Dish Mantel','boulder','V2',34.0,-116.0)", write=True) and "c0ffee000001"
server.link_mesh("aaaaaaaaaaaa", cid, None)

# --- a climb's and a mesh's own fields
server.changing_row(ANA, "climbs", cid, server.CLIMB_FIELDS, lambda: q("UPDATE climbs SET name='Dish', lat=35.0, coord_source='manual' WHERE id=?", cid, write=True))
c = last("climb")
assert c["who"] == "Ana" and c["who_id"] == "a1" and c["target"] == cid and "name 'Dish Mantel' → 'Dish'" in c["what"], c
server.changing_row(ANA, "meshes", "aaaaaaaaaaaa", server.MESH_FIELDS, lambda: q("UPDATE meshes SET name='Renamed' WHERE id='aaaaaaaaaaaa'", write=True))
server.changing_row(ANA, "meshes", "aaaaaaaaaaaa", server.MESH_FIELDS, lambda: None)
assert len(q("SELECT 1 FROM changes WHERE kind='mesh'")) == 1, "nothing changed: nothing noted"
server.revert_change(c["id"])
row = q("SELECT name, lat, coord_source FROM climbs WHERE id=?", cid, one=True)
assert row == {"name": "Dish Mantel", "lat": 34.0, "coord_source": None}, row
assert q("SELECT reverted_by FROM changes WHERE id=?", c["id"], one=True)["reverted_by"] == last("climb")["id"]
assert last("climb")["who"] == "the owner (admin page)"
server.revert_change(last("mesh")["id"])
assert q("SELECT name FROM meshes WHERE id='aaaaaaaaaaaa'", one=True)["name"] == "Mesh aaaaaaaaaaaa"
try:
    server.revert_change(c["id"])
    raise AssertionError("nothing left to put back")
except ValueError as e:
    assert "already" in str(e), e
print("climbs and meshes put back OK")

# --- a line drawn by hand: drawn, drawn again, taken away; each put back
d = server.MESHES / "aaaaaaaaaaaa"
first, second = [[0, 0, 0], [0, 1, 0]], [[0, 0, 0], [0.2, 1, 0]]
server.changing_line(ANA, "aaaaaaaaaaaa", cid, "line drawn by hand", lambda: server.draw_line("aaaaaaaaaaaa", cid, first, "Ana"))
server.changing_line({"id": "b2", "name": "Ben", "role": "editor"}, "aaaaaaaaaaaa", cid, "line drawn by hand",
                     lambda: server.draw_line("aaaaaaaaaaaa", cid, second, "Ben"))
assert last("line")["who"] == "Ben" and "Dish Mantel" in last("line")["what"]
server.revert_change(last("line")["id"])
assert betalines.load_drawn(d)[cid]["p"] == [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0]] and betalines.load_drawn(d)[cid]["by"] == "Ana", "Ana's again"
first_change = q("SELECT id FROM changes WHERE kind='line' ORDER BY id LIMIT 1", one=True)["id"]
server.revert_change(first_change)
assert cid not in betalines.load_drawn(d), "none drawn, as before Ana's"
print("lines put back OK")

# --- an older version made current, and put back
mesh("bbbbbbbbbbbb")
mesh("cccccccccccc")
q("UPDATE meshes SET replaced_by='cccccccccccc' WHERE id='bbbbbbbbbbbb'", write=True)
server.link_mesh("cccccccccccc", cid, None)
server.changing_current(ANA, "bbbbbbbbbbbb", "an older version made current", lambda: server.make_current("bbbbbbbbbbbb"))
assert q("SELECT replaced_by FROM meshes WHERE id='cccccccccccc'", one=True)["replaced_by"] == "bbbbbbbbbbbb"
ch = last("current")
assert ch["who"] == "Ana" and ch["mesh_id"] == "cccccccccccc" and ch["target"] == "bbbbbbbbbbbb", ch
server.revert_change(ch["id"])
assert q("SELECT replaced_by FROM meshes WHERE id='bbbbbbbbbbbb'", one=True)["replaced_by"] == "cccccccccccc"
assert q("SELECT replaced_by FROM meshes WHERE id='cccccccccccc'", one=True)["replaced_by"] is None, "the newer one current again"
listed = server.admin_changes({})["changes"]
assert all(r["revertable"] for r in listed if r["kind"] in ("line", "climb", "mesh", "current")) and "Ana" in server.admin_changes({})["people"]
print("versions put back OK")

# --- a card's beta: only the holds a visitor sees, from the betas they're in (videos and shared sets)
d = server.MESHES / "aaaaaaaaaaaa"
hs = [{"id": i, "p": [0, i, 0], "role": "hand"} for i in range(4)]
beta.save(d, {"status": "done", "videos": [{"url": "u1", "tag": "11111111", "holds": hs[:2] + [{**hs[2], "off": True}]},
                                          {"url": "u2", "tag": "22222222", "holds": hs[:3]},
                                          {"url": "u3", "tag": "33333333", "holds": [{**hs[0], "off": True}]},
                                          {"url": "u4", "tag": "44444444", "holds": []}]})
sets = [{"id": "s1", "holds": hs[:2], "shared": True}, {"id": "s2", "holds": hs, "shared": False}, {"id": "s3", "holds": [], "shared": True}]
pub = lambda: server.beta_public("aaaaaaaaaaaa", beta.load(d), sets)    # noqa: E731
assert pub() == {"holds": 2 + 3 + 2, "betas": 3}, pub()
beta.edit_video(d, "22222222", {"hidden": True})
beta.edit_video(d, "11111111", {"hold": {"id": 0, "drop": True}})
assert pub() == {"holds": 1 + 2, "betas": 2}, pub()
beta.edit_video(d, "11111111", {"hold": {"id": 2, "keep": True}})
beta.edit_video(d, "33333333", {"hold": {"id": 0, "keep": True}})
assert pub() == {"holds": 2 + 1 + 2, "betas": 3}, pub()
print("cards' beta OK")
print("ALL CHANGES TESTS PASSED")
