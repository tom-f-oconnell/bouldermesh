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
    real_queue = server.queue_job
    server.queue_job = lambda job: (queued.append(job), real_queue(job))
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
    # kept with its project, so a restart runs it again as it was, its boulder meshed after (init_db(), requeue_waiting())
    assert json.loads(server.q("SELECT job FROM projects WHERE id=?", queued[-1]["pid"], one=True)["job"])["then_boulder"] == leaf
    # and a Retry after it failed keeps it; its boulder is meshed only once the harvest gets through (then_boulder())
    hp = queued[-1]["pid"]
    server.q("UPDATE projects SET status='failed' WHERE id=?", hp, write=True)
    assert server.retry_project(hp)[1] == 202 and queued[-1] == {"type": "retry", "pid": hp, "then_boulder": leaf}, queued[-1]
    meshed = []
    server.mesh_boulder = lambda b, wait=False: (meshed.append(b), ({"queued": True}, 202))[1]
    server.then_boulder(queued[-1])
    assert not meshed, "not while it's queued"
    server.q("UPDATE projects SET status='harvested' WHERE id=?", hp, write=True)
    server.then_boulder(queued[-1])
    assert meshed == [leaf]
    # ---- a group, outside an area: not here
    server.leaf_kind = lambda b, kinds=None: "group"
    assert server.reprocess_whole("c3", "rebuild")[1] == 409
    try:
        server.reprocess_whole("c3", "nonesuch")
        raise AssertionError("an unknown what")
    except ValueError:
        pass
    # ---- a climb past its area's limit, queued to add what volunteers fetched for it, is taken all the same, and an area
    # that finished with one is taken up again as the server starts
    server.q("INSERT INTO campaigns(id,name,created,status,max_meshes) VALUES('lim','Limited',?,'done',1)", now, write=True)
    for sid, rank, status, redo in (("1", 1, "done", None), ("2", 2, "pending", None), ("3", 3, "pending", "more+rebuild"),
                                    ("4", 4, "pending", "new+rebuild")):
        server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,status,redo) VALUES('lim',?,?,?,?,?)",
                 sid, rank, sid, status, redo, write=True)
    camp = server.q("SELECT * FROM campaigns WHERE id='lim'", one=True)
    assert [i["source_id"] for i in server.ordered_pending(camp)] == ["3", "4"], server.ordered_pending(camp)
    server.init_db()
    assert server.q("SELECT status, only_retries FROM campaigns WHERE id='lim'", one=True) == {"status": "running", "only_retries": 1}
    print("ALL WHOLE TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
