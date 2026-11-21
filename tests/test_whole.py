#!/usr/bin/env python3
"""Harvesting or meshing a climb again with its boulder or group (server.reprocess_whole()): in its area, as that
area's redo of it, its boulder or pools made again after; outside one, harvested and then its boulder meshed; a group
only within an area. Offline.

    python3 tests/test_whole.py
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
tmp = tempfile.mkdtemp(prefix="bouldermesh-whole-")
os.environ["DATA_DIR"] = tmp

import server  # noqa: E402

try:
    server.init_db()
    now = time.time()
    queued = []
    server.queue_job = lambda job: queued.append(job)
    leaf = json.dumps(["CA", "JT", "Rock Boulder"])
    for cid in ("c1", "c2", "c3"):
        server.q("INSERT INTO climbs(id,source,source_id,name,area_path) VALUES(?,?,?,?,?)", cid, "mountainproject", cid, cid, leaf, write=True)
    # ---- in an area: its redo, the boulder's mesh after
    server.q("INSERT INTO projects(id,name,created,status,kind,climb_id,harvest) VALUES('h1','h1',?,'done','harvest','c1','{}')", now, write=True)
    server.q("INSERT INTO campaigns(id,name,created,status) VALUES('a','Area',?,'done')", now, write=True)
    server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,climb_id,project_id,status,boulder) VALUES('a','1',1,'c1','c1','h1','done',?)",
             leaf, write=True)
    r, code = server.reprocess_whole("c1", "harvest")
    item = server.q("SELECT status, redo FROM campaign_items WHERE source_id='1'", one=True)
    assert code == 202 and item == {"status": "pending", "redo": "harvest+rebuild"}, (r, item)
    assert server.q("SELECT status FROM campaigns WHERE id='a'", one=True)["status"] == "running"
    assert server.reprocess_whole("c1", "new")[1] == 409, "its area is on it"
    # ---- outside an area: harvested, then its boulder meshed
    r, code = server.reprocess_whole("c2", "harvest")
    assert code == 202 and queued[-1]["type"] == "harvest" and queued[-1]["then_boulder"] == leaf and not queued[-1]["reconstruct"], queued
    # ---- a group, outside an area: not here
    server.leaf_kind = lambda b, kinds=None: "group"
    assert server.reprocess_whole("c3", "rebuild")[1] == 409
    try:
        server.reprocess_whole("c3", "nonesuch")
        raise AssertionError("an unknown what")
    except ValueError:
        pass
    print("ALL WHOLE TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
