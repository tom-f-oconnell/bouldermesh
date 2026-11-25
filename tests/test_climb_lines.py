#!/usr/bin/env python3
"""A mesh's climbs' lines as the server keeps and serves them (server.mesh_lines() and around it): which videos are of
which climb, made once asked for and again when its beta or its climbs' videos change, not while beta is being found.
Offline: a wall and tracks built here, no harvests or pose model.

    python3 tests/test_climb_lines.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = tempfile.mkdtemp(prefix="bouldermesh-lines-")
os.environ["DATA_DIR"] = tmp
try:
    import numpy as np
except ImportError:
    print("numpy not installed: skipping")
    sys.exit(0)

import server  # noqa: E402
from server import beta_mod, betalines  # noqa: E402


def wall(d: pathlib.Path) -> None:
    """A flat wall 4 x 4 at z = 0, as a mesh.ply."""
    n = 41
    gx, gy = np.meshgrid(np.linspace(0, 4, n), np.linspace(0, 4, n), indexing="ij")
    idx = np.arange(n * n).reshape(n, n)
    F = np.concatenate([np.stack([idx[:-1, :-1].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()], axis=1),
                        np.stack([idx[:-1, :-1].ravel(), idx[1:, 1:].ravel(), idx[:-1, 1:].ravel()], axis=1)])
    with open(d / "mesh.ply", "w") as f:
        f.write(f"ply\nformat ascii 1.0\nelement vertex {n * n}\nproperty float x\nproperty float y\nproperty float z\n"
                f"element face {len(F)}\nproperty list uchar int vertex_indices\nend_header\n")
        f.write("\n".join(f"{x:.4f} {y:.4f} 0" for x, y in zip(gx.ravel(), gy.ravel())) + "\n")
        f.write("\n".join(f"3 {a} {b} {c}" for a, b, c in F) + "\n")


def up(x: float) -> list:
    """A centre track straight up the wall at x, in one piece."""
    return [[[0.5 * k, x, 0.5 + 0.1 * k, 0.0] for k in range(31)]]


def video(url: str, x: float) -> dict:
    return {"url": url, "tag": url[-2:], "status": "done", "holds": [], "tracks": {}, "centre": up(x)}


try:
    server.init_db()
    now = time.time()
    for cid, name, grade in (("ca", "Left One", "V3 6A"), ("cb", "Right One", "V5 6C"), ("cc", "No Videos", None), ("cz", "Elsewhere", "V1")):
        server.q("INSERT INTO climbs(id,source,source_id,name,grade,area_path) VALUES(?,?,?,?,?,'[]')", cid, "test", cid, name, grade, write=True)
    server.q("INSERT INTO meshes(id,name,filename,source,created) VALUES('aa11','A Boulder','mesh.ply','project:p1',?)", now, write=True)
    for cid in ("ca", "cb", "cc"):
        server.q("INSERT INTO mesh_links(mesh_id,climb_id,note,created) VALUES('aa11',?,NULL,?)", cid, now, write=True)
    for cid, url in (("ca", "yt/a1"), ("ca", "yt/a2"), ("cb", "yt/b1"), ("cz", "yt/a1"), ("ca", "yt/not-in-its-beta")):
        server.q("INSERT INTO climb_videos(climb_id,url,found_by,created) VALUES(?,?,'search',?)", cid, url, now, write=True)
    d = server.MESHES / "aa11"
    d.mkdir(parents=True)
    wall(d)

    # ---- no beta: no lines, and nothing asked for
    assert server.mesh_lines("aa11") == {"lines": [], "climbs": 0, "pending": False}
    assert not server.make_lines("aa11") and server._lines_wanted.empty()

    # ---- which videos are of which climb: those in its beta, by climb_videos, for the climbs the mesh is of
    record = {"status": "done", "videos": [video("yt/a1", 1.0), video("yt/a2", 1.1), video("yt/b1", 3.0), video("yt/stray", 2.0)]}
    beta_mod.save(d, record)
    assert server.lines_of("aa11", record) == {"ca": ["yt/a1", "yt/a2"], "cb": ["yt/b1"], "cc": []}

    # ---- asked for before they're made: none yet, pending, and queued once however often asked
    first = server.mesh_lines("aa11")
    assert first == {"lines": [], "climbs": 3, "pending": True}, first
    server.mesh_lines("aa11")
    assert server._lines_wanted.qsize() == 1 and server._lines_wanted.get_nowait() == "aa11"
    server._lines_queued.clear()

    # ---- made: each climb's with its name and grade, in its climbs' order; the climb with no videos has none
    assert server.make_lines("aa11") and not server.make_lines("aa11"), "made once"
    got = server.mesh_lines("aa11")
    assert not got["pending"] and got["climbs"] == 3 and server._lines_wanted.empty(), got
    assert [(x["climb"], x["name"], x["grade"], x["videos"], x["agree"]) for x in got["lines"]] == \
        [("ca", "Left One", "V3 6A", 2, 2), ("cb", "Right One", "V5 6C", 1, 1)], got["lines"]
    a, b = got["lines"]
    assert all(abs(p[0] - 1.05) < 0.1 and abs(p[2]) < 0.01 for p in a["p"]) and all(abs(p[0] - 3) < 0.05 for p in b["p"])
    assert len(a["p"]) == len(a["n"]) and 0 < a["at"] < len(a["p"]) and min(p[1] for p in a["p"]) < 0.7 < 3.3 < max(p[1] for p in a["p"])

    # ---- beta.json written again with the same tracks (as it is every few seconds while more is found): still theirs
    time.sleep(0.02)
    beta_mod.save(d, {**record, "status": "queued", "waiting": now + 60})
    assert not server.mesh_lines("aa11")["pending"] and not server.make_lines("aa11")

    # ---- a video found to be of another climb: made again, the old ones served meanwhile
    server.q("INSERT INTO climb_videos(climb_id,url,found_by,created) VALUES('cc','yt/stray','search',?)", now, write=True)
    stale = server.mesh_lines("aa11")
    assert stale["pending"] and len(stale["lines"]) == 2 and server._lines_wanted.get_nowait() == "aa11"
    server._lines_queued.clear()
    assert server.make_lines("aa11")
    got = server.mesh_lines("aa11")
    assert [x["climb"] for x in got["lines"]] == ["ca", "cc", "cb"] and not got["pending"], "by name: Left One, No Videos, Right One"

    # ---- while its beta is being found, its tracks are still coming: not made, nor asked for
    time.sleep(0.02)
    beta_mod.save(d, {"status": "running", "videos": record["videos"][:1]})
    assert not server.make_lines("aa11")
    running = server.mesh_lines("aa11")
    assert not running["pending"] and len(running["lines"]) == 3 and server._lines_wanted.empty()

    # ---- a mesh of one climb: every video in its beta is of it, whatever climb_videos has
    server.q("INSERT INTO meshes(id,name,filename,source,created) VALUES('bb22','One Climb','mesh.ply','project:p2',?)", now, write=True)
    server.q("INSERT INTO mesh_links(mesh_id,climb_id,note,created) VALUES('bb22','cz',NULL,?)", now, write=True)
    d2 = server.MESHES / "bb22"
    d2.mkdir()
    wall(d2)
    beta_mod.save(d2, {"status": "done", "videos": [video("yt/x1", 2.0), video("yt/x2", 2.1)]})
    assert server.lines_of("bb22", beta_mod.load(d2)) == {"cz": ["yt/x1", "yt/x2"]}
    assert server.make_lines("bb22")
    one = server.mesh_lines("bb22")
    assert one["climbs"] == 1 and [(x["climb"], x["agree"]) for x in one["lines"]] == [("cz", 2)], one

    # ---- what's kept from a version of the rules since changed is made again
    betalines.VERSION += 1
    assert server.mesh_lines("bb22")["pending"] and server.make_lines("bb22") and not server.mesh_lines("bb22")["pending"]
    assert not server.make_lines("nosuchmesh")
    print("ALL CLIMB LINES TESTS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
