#!/usr/bin/env python3
"""A mesh's climbs' lines as the server keeps and serves them (server.mesh_lines() and around it): which videos are of
which climb, made once asked for and again when its beta or its climbs' videos change, not while beta is being found.
Offline: a wall and tracks built here, no harvests or pose model.

    python3 tests/test_climb_lines.py
"""
from __future__ import annotations

import json
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


def video(url: str, x: float, holds=()) -> dict:
    return {"url": url, "tag": url[-2:], "status": "done", "holds": list(holds), "tracks": {}, "centre": up(x)}


def hold(i: int, role: str, p: list, *held) -> dict:
    """A hold as beta.json has it; held: (limb, t0, t1)."""
    return {"id": i, "role": role, "p": p, "contacts": [{"limb": limb, "t0": t0, "t1": t1} for limb, t0, t1 in held]}


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
    none = server.mesh_lines("aa11")
    assert (none["lines"], none["climbs"], none["pending"]) == ([], 3, False), none
    assert [(x["climb"], x["name"], x["drawn"]) for x in none["of"]] == [("ca", "Left One", None), ("cc", "No Videos", None), ("cb", "Right One", None)]
    assert not server.make_lines("aa11") and server._lines_wanted.empty()

    # ---- which videos are of which climb: those in its beta, by climb_videos, for the climbs the mesh is of
    # a1's holds: one touched 2.45 from its climb's line (walking in), one 0.85 from it, the rest on it
    a1 = [hold(0, "start", [3.5, 0.5, 0], ("lh", 0, 1), ("rh", 0, 1)), hold(1, "hand", [1.0, 1.0, 0], ("lh", 3, 6)),
          hold(2, "finish", [1.1, 3.0, 0], ("rh", 5, 9)), hold(3, "finish", [1.9, 2.0, 0], ("lh", 7, 9)),
          hold(4, "foot", [0.9, 0.6, 0], ("lf", 3, 6)), hold(5, "foot", [1.1, 1.4, 0], ("rf", 5, 9)), hold(6, "foot", [1.0, 2.2, 0], ("lf", 7, 9))]
    record = {"status": "done", "videos": [video("yt/a1", 1.0, a1), video("yt/a2", 1.1), video("yt/b1", 3.0), video("yt/stray", 2.0)]}
    beta_mod.save(d, record)
    assert server.lines_of("aa11", record) == {"ca": ["yt/a1", "yt/a2"], "cb": ["yt/b1"], "cc": []}
    as_found = server.beta_shown("aa11", beta_mod.load(d))
    assert as_found == record and server._lines_wanted.get_nowait() == "aa11", "no lines yet: its beta as found, and they're asked for"
    server._lines_queued.clear()

    # ---- asked for before they're made: none yet, pending, and queued once however often asked
    first = server.mesh_lines("aa11")
    assert (first["lines"], first["climbs"], first["pending"]) == ([], 3, True), first
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

    # ---- its beta as shown: the hold far from its climb's line off, and the roles of the rest from what's left
    # (the wall's size is 3.6 and it has no scale, so a reach is 1.08 of its units)
    seen = {h["id"]: h for h in server.beta_shown("aa11", beta_mod.load(d))["videos"][0]["holds"]}
    assert [i for i, h in seen.items() if h.get("off")] == [0] and seen[0]["role"] == "start"
    assert [(seen[i]["role"], seen[i].get("found_role")) for i in (1, 2, 3)] == [("start", "hand"), ("start", "finish"), ("finish", None)], seen
    assert all("off" not in h and "found_role" not in h for h in beta_mod.load(d)["videos"][0]["holds"]), "beta.json itself is as found"
    made = beta_mod.new_set(d, source={"video": "yt/a1"}, found=server.beta_shown("aa11", beta_mod.load(d)))
    assert [(h["p"][0], h["role"]) for h in made["holds"]] == [(1.0, "start"), (1.1, "start"), (1.9, "finish"), (0.9, "foot"), (1.1, "foot"), (1.0, "foot")], \
        "a copy of a video's holds: those shown"
    assert len(beta_mod.new_set(d, source={"video": "yt/a1"})["holds"]) == 7, "or, not given what's shown, as found"
    # its size found, 3 m a unit: a reach is 0.58 of its units, so the hold 0.85 from the line is off too, once made again
    (d / "scale.json").write_text('{"status": "done", "metres_per_unit": 3.0}')
    stale = server.beta_shown("aa11", beta_mod.load(d))
    assert not any(h.get("off") for h in stale["videos"][0]["holds"]) and server._lines_wanted.get_nowait() == "aa11", "not by lines made for another scale"
    server._lines_queued.clear()
    assert server.mesh_lines("aa11")["pending"] and server._lines_wanted.get_nowait() == "aa11"
    server._lines_queued.clear()
    assert server.make_lines("aa11") and not server.mesh_lines("aa11")["pending"]
    seen = {h["id"]: h for h in server.beta_shown("aa11", beta_mod.load(d))["videos"][0]["holds"]}
    assert [i for i, h in seen.items() if h.get("off")] == [0, 3] and (seen[1]["role"], seen[2]["role"]) == ("start", "start")
    assert next(x for x in server.mesh_lines("aa11")["lines"] if x["climb"] == "ca")["astray"] == round(2 / 7, 3)
    assert server.mesh_lines("aa11")["astray_fails"] == betalines.ASTRAY, "what the viewer colours an editor's lines by"
    kept = betalines.load(d)
    doubted = json.loads(json.dumps(kept)); doubted["lines"]["ca"]["astray"] = betalines.ASTRAY
    (d / betalines.FILE).write_text(json.dumps(doubted))
    assert "ca" in {x["climb"] for x in server.mesh_lines("aa11")["lines"]}, "an editor sees a line in doubt"
    assert "ca" not in {x["climb"] for x in server.mesh_lines("aa11", editor=False)["lines"]}, "a visitor doesn't"
    assert len(server.mesh_lines("aa11", editor=False)["lines"]) == len(kept["lines"]) - 1, "the rest they do"
    (d / betalines.FILE).write_text(json.dumps(kept))
    (d / "scale.json").unlink()
    assert server.make_lines("aa11")

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

    # ---- a line drawn by hand: for a climb with no videos, and in place of a climb's videos' line; it speaks for
    # its climb's videos' holds; taken away, the videos' line is the climb's again
    time.sleep(0.02)
    beta_mod.save(d, record)
    server.make_lines("aa11")
    drawn = server.draw_line("aa11", "cc", [[2.0, 0.5, 0.3], [2.2, 2.0, 0.3], [2.0, 3.5, 0.3]], "Tom")
    assert drawn["pending"] and server._lines_wanted.get_nowait() == "aa11" and next(x for x in drawn["of"] if x["climb"] == "cc")["drawn"][1] == [2.2, 2.0, 0.3]
    server._lines_queued.clear()
    assert server.make_lines("aa11") and not server.make_lines("aa11")
    cc = next(x for x in server.mesh_lines("aa11")["lines"] if x["climb"] == "cc")
    assert cc["drawn"] and (cc["videos"], cc["agree"]) == (0, 0) and len(cc["p"]) > 20 and max(abs(p[2]) for p in cc["p"]) < 0.01, "laid on the wall"
    assert abs(cc["p"][0][1] - 0.5) < 0.05 and abs(cc["p"][-1][1] - 3.5) < 0.05 and max(p[0] for p in cc["p"]) > 2.15, "through its points, start to top"
    before = next(x for x in server.mesh_lines("aa11")["lines"] if x["climb"] == "ca")
    assert "drawn" not in before
    server.draw_line("aa11", "ca", [[1.6, 0.5, 0], [1.6, 3.5, 0]], None)
    server._lines_wanted.get_nowait(); server._lines_queued.clear()
    assert server.make_lines("aa11")
    ca = next(x for x in server.mesh_lines("aa11")["lines"] if x["climb"] == "ca")
    assert ca["drawn"] and all(abs(p[0] - 1.6) < 0.01 for p in ca["p"])
    seen = {h["id"]: h for h in server.beta_shown("aa11", beta_mod.load(d))["videos"][0]["holds"]}
    assert [i for i, h in seen.items() if h.get("off")] == [0], "by the drawn line: the hold 1.9 from it off, that 0.3 from it not"
    server.draw_line("aa11", "ca", None, None)
    server._lines_wanted.get_nowait(); server._lines_queued.clear()
    assert server.make_lines("aa11") and "drawn" not in next(x for x in server.mesh_lines("aa11")["lines"] if x["climb"] == "ca")
    for bad in ([[1, 2, 3]], "x", [[1, 2]], [[1, 2, float("nan")], [1, 2, 3]], [[0, 0, 0]] * 201):
        try:
            server.draw_line("aa11", "ca", bad, None)
            raise AssertionError(f"{str(bad)[:30]} isn't a line")
        except ValueError:
            pass
    try:
        server.draw_line("aa11", "cz", [[1, 1, 0], [1, 2, 0]], None)
        raise AssertionError("not a climb of this mesh")
    except ValueError:
        pass
    # a mesh with no beta at all can have one
    server.q("INSERT INTO meshes(id,name,filename,source,created) VALUES('cc33','No Beta','mesh.ply','project:p3',?)", now, write=True)
    server.q("INSERT INTO mesh_links(mesh_id,climb_id,note,created) VALUES('cc33','cz',NULL,?)", now, write=True)
    d3 = server.MESHES / "cc33"
    d3.mkdir()
    wall(d3)
    assert not server.make_lines("cc33") and server.mesh_lines("cc33")["lines"] == []
    server.draw_line("cc33", "cz", [[1, 1, 0], [3, 3, 0]], None)
    assert server.make_lines("cc33") and [x["climb"] for x in server.mesh_lines("cc33")["lines"]] == ["cz"] and not server.mesh_lines("cc33")["pending"]

    # ---- videos cut at 150 s are looked at again, once: those in the cache, the others' beta kept as it is
    full = int(beta_mod.LEAST_SECONDS * beta_mod.FPS)
    vids = [{**video("yt/x1", 2.0), "frames": full}, {**video("yt/x2", 2.1), "frames": 120}, {**video("yt/x3", 2.2), "frames": full},
            {**video("yt/x4", 2.3), "frames": full, "left": None}]
    beta_mod.save(d2, {"status": "done", "videos": vids})
    real, server.youtube_mod.cached = server.youtube_mod.cached, lambda u: pathlib.Path("v.mp4") if u != "yt/x3" else None
    try:
        assert server.requeue_cut_videos() == 0, "not in a mesh made under older rules: what replaces it gets its own"
        server.q("UPDATE meshes SET rules=? WHERE id='bb22'", server.MESH_RULES, write=True)
        assert server.requeue_cut_videos() == 1, "x1: cut, and here; x2 was shorter, x3 isn't here, x4 was looked at since"
        rec = beta_mod.load(d2)
        assert rec["status"] == "queued" and rec["resume"] and [v["url"] for v in rec["videos"]] == ["yt/x2", "yt/x3", "yt/x4"], rec
        assert list(json.loads((d2 / server.CUT_FILE).read_text())) == ["yt/x1"], "its beta as it was, kept"
        beta_mod.save(d2, {"status": "done", "videos": vids})       # as a machine on an older image would leave it
        assert server.requeue_cut_videos() == 0, "not again"
    finally:
        server.youtube_mod.cached = real

    # ---- what's kept from a version of the rules since changed is made again
    betalines.VERSION += 1
    assert server.mesh_lines("bb22")["pending"] and server.make_lines("bb22") and not server.mesh_lines("bb22")["pending"]
    assert not server.make_lines("nosuchmesh")
    print("ALL CLIMB LINES TESTS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
