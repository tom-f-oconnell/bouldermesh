#!/usr/bin/env python3
"""A boulder with more problems than its area lists is meshed whole, not one climb on its own (pooled() by its page and
the climbs known on it), and a climb's own mesh made before that, on such a boulder with no mesh of its own, is in the
admin page's "grouping" remake tier, queued as its boulder's mesh. Offline: no harvests or meshing.

    python3 tests/test_grouping.py
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
tmp = tempfile.mkdtemp(prefix="bouldermesh-grouping-")
os.environ["DATA_DIR"] = tmp

import server  # noqa: E402

try:
    server.init_db()
    now = time.time()
    server.settings().update({"areas.meshes": "boulders"})
    ROCK = json.dumps(["CA", "JT", "Rock Boulder"])
    server.q("INSERT INTO boulders(key, name, kind, routes) VALUES(?, 'Rock Boulder', 'boulder', 1)", ROCK, write=True)
    server._kinds_gen += 1
    server.q("INSERT INTO climbs(id,source,source_id,name,area_path) VALUES('c1','mountainproject','1','One',?)", ROCK, write=True)
    server.q("INSERT INTO projects(id,name,created,status,kind,climb_id,mesh_id) VALUES('h1','One (harvest)',?,'done','harvest','c1','m1')",
             now, write=True)
    server.q("INSERT INTO campaigns(id,name,created,status) VALUES('big','Big area',?,'done')", now, write=True)
    server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,climb_id,project_id,status,boulder,mesh_id) "
             "VALUES('big','1',1,'One','c1','h1','done',?,'m1')", ROCK, write=True)

    # ---- one problem on its page, one listed: its own mesh is its boulder's
    assert not server.pooled("big", ROCK) and not server.boulders_to_mesh("big")
    # ---- more on its page than the area lists: harvested for its boulder's mesh
    server.q("UPDATE boulders SET routes=4", write=True)
    assert server.pooled("big", ROCK) and [r["source_id"] for r in server.boulders_to_mesh("big")] == ["1"]
    server.q("UPDATE boulders SET routes=1", write=True)
    server.q("INSERT INTO climbs(id,source,source_id,name,area_path) VALUES('c2','mountainproject','2','Two',?)", ROCK, write=True)
    assert server.pooled("big", ROCK), "or 2 known on it (from another area's list, or its page)"
    server.settings().update({"areas.meshes": "climbs"})
    assert not server.pooled("big", ROCK), "every climb on its own, as the admin page can say"
    server.settings().update({"areas.meshes": "boulders"})
    GROUP = json.dumps(["CA", "JT", "Manx Boulders"])
    server.q("INSERT INTO boulders(key, name, kind, routes) VALUES(?, 'Manx Boulders', 'group', 9)", GROUP, write=True)
    server._kinds_gen += 1
    assert not server.pooled("big", GROUP), "not where its page says it holds several rocks"
    print("pooled by its page OK")

    # ---- its climb's own mesh, made before: in the grouping tier, until its boulder has a mesh of its own
    server.q("INSERT INTO meshes(id,name,filename,source,created,rules) VALUES('m1','One','mesh.glb','project:h1',?,?)", now,
             server.MESH_RULES, write=True)
    assert server.grouping_tier() == ["m1"]
    server.q("UPDATE meshes SET old_data='m9' WHERE id='m1'", write=True)
    assert server.grouping_tier() == [], "a newer mesh covers it already"
    server.q("UPDATE meshes SET old_data=NULL WHERE id='m1'", write=True)
    server.q("INSERT INTO meshes(id,name,filename,source,created,rules,boulder) VALUES('mb','Rock','mesh.glb','project:b1',?,?,?)", now,
             server.MESH_RULES, ROCK, write=True)
    assert server.grouping_tier() == [], "its boulder has a mesh"
    server.q("DELETE FROM meshes WHERE id='mb'", write=True)
    assert server.mesh_tiers(fresh=True)["grouping"] == 1
    print("the grouping tier OK")

    # ---- queued as its boulder's mesh, at low priority
    (server.MESHES / "m1").mkdir(parents=True)
    server.meshinputs.save(server.MESHES / "m1", {"kind": "climb", "project": "h1"})
    got = server.admin_remake_tier({"tier": "grouping"})
    row = server.q("SELECT * FROM campaign_items WHERE source_id='1'", one=True)
    assert (row["status"], row["redo"], row["was"], row["old_rerun"]) == ("pending", "boulder", "done", server.REMAKE_RANKS["grouping"]), row
    assert "queued 1" in got["result"], got
    print("queued as its boulder's OK")
    print("ALL GROUPING TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
