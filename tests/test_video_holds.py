#!/usr/bin/env python3
"""Boulders and climbs held up by YouTube videos volunteers are still fetching: they wait while any is still coming, however
long; the phone is told of holds past videos.hold_alert_hours, and again
at later marks; how long each lasted is kept for the day's summary; and
volunteers are given one boulder's videos before the next's. Offline: the volunteers' broker is a stand-in.

    python3 tests/test_video_holds.py
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
tmp = tempfile.mkdtemp(prefix="bouldermesh-holds-")
os.environ["DATA_DIR"] = tmp

import server  # noqa: E402

try:
    server.init_db()
    now = time.time()
    tasks: dict[str, list[dict]] = {}       # harvest project -> its pending tasks, each with "moved"
    server.VOLUNTEERS.pending_tasks = lambda purpose, refs: [t for r in refs for t in tasks.get(r, [])]
    server.VOLUNTEERS.pending = lambda purpose, ref: len(tasks.get(ref, []))
    told = []
    server.NOTIFIER.send = lambda title, message, *a, **k: told.append((title, message))

    # ---- what's still coming: nothing is given up on by time
    tasks["h1"] = [{"moved": now - 3600}]
    tasks["h2"] = [{"moved": now - 100 * 3600}]
    assert len(server.videos_moving(["h1", "h2"])) == 2, "nothing has moved on for 100 h: still waited for"
    assert server.may_wait({"waiting_since": now - 30 * 3600}, "h2"), "past VOLUNTEER_WAIT, still coming: waits on"
    assert not server.may_wait({"waiting_since": now - 30 * 3600}, "h9"), "nothing coming"
    print("still coming OK")

    # ---- a boulder isn't meshed while videos for any of its climbs are coming
    leaf = json.dumps(["CA", "JT", "Rock Boulder"])
    for cid, pid in (("c1", "h1"), ("c2", "h3")):
        server.q("INSERT INTO climbs(id,source,source_id,name,area_path) VALUES(?,?,?,?,?)", cid, "mountainproject", cid, cid, leaf, write=True)
        server.q("INSERT INTO projects(id,name,created,status,kind,climb_id,harvest) VALUES(?,?,?,?,?,?,?)", pid, pid, now, "harvested",
                 "harvest", cid, "{}", write=True)
        (server.PROJECTS / pid / "photos").mkdir(parents=True)
    server.q("INSERT INTO campaigns(id,name,created,status) VALUES('a','Area',?,'running')", now, write=True)
    for sid, cid, pid, status in (("1", "c1", "h1", "done"), ("2", "c2", "h3", "harvested")):
        server.q("INSERT INTO campaign_items(campaign_id,source_id,rank,name,climb_id,project_id,status,boulder) VALUES('a',?,?,?,?,?,?,?)",
                 sid, int(sid), cid, cid, pid, status, leaf, write=True)
    real_pools = server.pools_whole
    server.pools_whole = lambda b, kinds=None: True
    try:
        assert server.settle_boulder("a", leaf) is False, "c1's video is still coming, though c1 is done"
        held = server.q("SELECT * FROM video_holds", one=True)
        assert held["key"] == f"boulder|a|{leaf}" and held["what"] == "Rock Boulder" and held["coming"] == 1, held
        tasks["h1"] = []
        assert server.settle_boulder("a", leaf) is True, "it came"
        assert not server.q("SELECT 1 FROM video_holds"), "and the hold is let go"
    finally:
        server.pools_whole = real_pools
    print("a boulder held up OK")

    # ---- a climb waiting: past VOLUNTEER_WAIT, let go only once nothing more is coming
    server.q("UPDATE campaign_items SET status='waiting', waiting_since=?, redo=NULL WHERE source_id='2'", now - 30 * 3600, write=True)
    tasks["h3"] = [{"moved": now - 600}]
    server.settle_waiting("a")
    assert server.q("SELECT status FROM campaign_items WHERE source_id='2'", one=True)["status"] == "waiting"
    tasks["h3"] = [{"moved": now - 20 * 3600}]
    server.settle_waiting("a")
    assert server.q("SELECT status FROM campaign_items WHERE source_id='2'", one=True)["status"] == "waiting", "not given up on"
    tasks["h3"] = []
    server.settle_waiting("a")
    row = server.q("SELECT status, redo FROM campaign_items WHERE source_id='2'", one=True)
    assert (row["status"], row["redo"]) == ("pending", "more+rebuild"), row
    print("a climb waiting OK")

    # ---- the phone told of a hold past each mark of videos.hold_alert_hours (2, 6, 12, 24, then every 24 h), once
    # each; holds nothing looks at go, and how long each lasted is kept
    assert [server.hold_mark(2, m) for m in (None, 2, 6, 12, 24, 48)] == [2, 6, 12, 24, 48, 72]
    server.hold("climb|a|9", "Crimp Boulder", 3)
    server.q("UPDATE video_holds SET since=? WHERE key='climb|a|9'", now - 3 * 3600, write=True)
    assert server.hold_alerts() == 1 and "Crimp Boulder has waited 3.0 h for 3" in told[-1][1], told
    assert server.hold_alerts() == 0, "once"
    server.q("UPDATE video_holds SET since=? WHERE key='climb|a|9'", now - 7 * 3600, write=True)
    assert server.hold_alerts() == 1 and told[-1][0] == "Videos are still holding up a mesh", "again at 6 h"
    assert server.hold_alerts() == 0
    server.q("UPDATE video_holds SET since=? WHERE key='climb|a|9'", now - 50 * 3600, write=True)
    assert server.hold_alerts() == 1 and "50.0 h" in told[-1][1], "past 12, 24 and 48 at once: told once"
    assert server.q("SELECT told_h FROM video_holds WHERE key='climb|a|9'", one=True)["told_h"] == 48
    assert server.hold_alerts() == 0
    server.hold("boulder|a|q", "Quiet", 0, quiet=True)
    server.q("UPDATE video_holds SET since=? WHERE key='boulder|a|q'", now - 30 * 3600, write=True)
    assert server.hold_alerts() == 0, "a quiet hold is never told of"
    server.hold("climb|a|7", "Waited Long", 2, since=now - 40 * 3600)
    assert server.q("SELECT since FROM video_holds WHERE key='climb|a|7'", one=True)["since"] == now - 40 * 3600, \
        "a climb's hold begins when it began waiting"
    server.hold("climb|a|7", "Waited Long", 2, since=now - 3600)
    assert server.q("SELECT since FROM video_holds WHERE key='climb|a|7'", one=True)["since"] == now - 40 * 3600, "the earliest kept"
    note = server.hold_note(now)
    assert "Videos hold up 2 boulders and climbs now: 2 for 12 h or more, 2 for a day or more; longest Crimp Boulder, 50 h" in note, note
    server.unhold("climb|a|7")
    for i in range(5):
        server.hold(f"climb|b|{i}", f"Many {i}", 1, since=now - 3 * 3600)
    sent = len(told)
    assert server.hold_alerts() == 5 and len(told) == sent + 1 and told[-1][0] == "Videos are holding up 5 meshes", "together: one message"
    assert "Many 4: 3.0 h, 1 video(s)" in told[-1][1], told[-1]
    server.q("DELETE FROM video_holds WHERE key LIKE 'climb|b|%'", write=True)
    server.settings().update({"videos.hold_alert_hours": 0.0})
    server.hold("climb|a|8", "Other", 1)
    server.q("UPDATE video_holds SET since=? WHERE key='climb|a|8'", now - 5 * 3600, write=True)
    assert server.hold_alerts() == 0, "0: never"
    server.q("UPDATE video_holds SET seen=?", now - 7200, write=True)
    server.hold_alerts()
    assert not server.q("SELECT 1 FROM video_holds"), "gone once nothing looks at them"
    log = {r["key"]: r for r in server.q("SELECT * FROM hold_log")}
    assert log["climb|a|7"]["how"] == "came" and log["climb|a|9"]["how"] == "gone" and log["boulder|a|q"]["quiet"] == 1, log
    assert abs(log["climb|a|9"]["ended"] - log["climb|a|9"]["since"] - 48 * 3600) < 60, "gone: ended when last looked at"
    note = server.hold_note()
    # with the boulder's (0 h) and climb 2's (30 h, from its waiting_since) above; the quiet one apart
    assert note.startswith("Holds that ended in the last day: 5, median 30.0 h, longest 48 h (1 0-2 h, 1 2-6 h, 0 6-12 h, "
                           "0 12-24 h, 3 24 h+)."), note
    print("telling the phone OK")

    # ---- volunteers get one boulder's videos before the next's: the one waiting longest first
    wants = [{"ref": "h1", "t": now - 100}, {"ref": "h3", "t": now - 50}, {"ref": "hx", "t": now - 500}]
    server.VOLUNTEERS.q = lambda sql, *a, one=False: wants
    server.q("INSERT INTO projects(id,name,created,status,kind) VALUES('hx','x',?,'harvested','harvest')", now, write=True)
    server._focus["at"] = 0
    f1, f3, fx = server.volunteer_focus("h1"), server.volunteer_focus("h3"), server.volunteer_focus("hx")
    assert f1 == f3, "one boulder's climbs come together, by its earliest"
    assert fx < f1 < 0.1, "the one waiting longest first, within the rank"
    assert server.volunteer_rank("harvest", "h1") == f1
    # regular areas' harvests, then low-priority areas', then beta: beta wanted 2 days goes between the two
    real_low = server.low_projects
    server.low_projects = lambda fresh=False: frozenset({"h3"})
    try:
        beta = [{"ref": "m-new", "t": now - 3600}, {"ref": "m-old", "t": now - 49 * 3600}]
        server.VOLUNTEERS.q = lambda sql, *a, one=False: beta if "'beta'" in sql else wants
        server._beta_since["at"] = 0
        ranks = {k: server.volunteer_rank(*k) for k in (("harvest", "h1"), ("harvest", "h3"), ("beta", "m-new"), ("beta", "m-old"))}
        assert ranks[("harvest", "h1")] < ranks[("beta", "m-old")] < ranks[("harvest", "h3")] < ranks[("beta", "m-new")], ranks
        assert ranks[("beta", "m-new")] == 2 and server.volunteer_rank("beta", "unknown") == 2
    finally:
        server.low_projects = real_low
    print("a boulder at a time, harvests before beta OK")
    print("ALL VIDEO HOLD TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
