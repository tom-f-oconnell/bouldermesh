#!/usr/bin/env python3
"""Mesh timings (backend/timings.py): what a job log says of a reconstruction's tools, wherever it ran; the progress
tracker's step times; the last run of an old log, for meshes made before timings were kept; and the admin page's
medians. Offline.

    python3 tests/test_timings.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import progress  # noqa: E402
import timings  # noqa: E402

REMOTE = """remote: 25 MB ready for the machines that connect here, job c5b71fca3a12
remote: hal-2 (GPU) took it after 166 s
remote: running on hal with its GPU
== Finding people to ignore
-- ran 4 s, using 9 s of CPU
== COLMAP: feature extraction (internet-collection settings)
I1110 23:01:02 feature_extraction.cc:250] Processed file [1/39]
-- ran 45 s, using up to 18.7 GB of memory, 800 s of CPU
== COLMAP: matching
-- ran 5 s, using up to 0.3 GB of memory, 7 s of CPU
== COLMAP: sparse reconstruction (mapper)
-- ran 17 s, using up to 0.1 GB of memory, 18 s of CPU
-- ran 21 s, using up to 0.1 GB of memory, 22 s of CPU
== VGGT: placing the photos, as COLMAP's mapper placed only 4
VGGT: placing 9 photos linked by verified matches, on the GPU
VGGT: placed them in 64 s
== COLMAP: points from those placements, round 1 of 3 (up to 8° and 150 px off)
-- ran 3 s, using up to 0.1 GB of memory, 5 s of CPU
-- ran 10 s, using up to 0.1 GB of memory, 95 s of CPU
VGGT and COLMAP placed 9 photos
registered 9/39 images in the largest model
== OpenMVS: densify
-- ran 17 s, using up to 0.3 GB of memory, 319 s of CPU
== OpenMVS: texture mesh
23:00:08 [App     ] Mesh saved: 44517 vertices, 88883 faces (130ms)
-- ran 6 s, using up to 0.4 GB of memory, 99 s of CPU
remote: hal-2 finished in 5 min
cleanup: hiding 1 of 2 pieces, 0.57% of the faces; the mesh itself is unchanged
"""

tmp = Path(tempfile.mkdtemp(prefix="bouldermesh-timings-"))
try:
    # a job another machine ran, which VGGT placed
    p = timings.parse_log(REMOTE)
    assert (p["machine"], p["gpu"], p["waited"], p["photos_in"], p["placed"]) == ("hal-2", True, 166, 39, 9), p
    assert p["vggt_seconds"] == 64 + 3 + 10, "its own time and its rounds of points"
    assert [t["step"] for t in p["tools"]][:2] == ["Finding people to ignore", "COLMAP: feature extraction (internet-collection settings)"]
    assert p["tools"][1] == {"step": "COLMAP: feature extraction (internet-collection settings)", "seconds": 45, "cpu": 800, "peak_gb": 18.7}
    assert p["tools"][0] == {"step": "Finding people to ignore", "seconds": 4, "cpu": 9}, "no memory when too short to see"

    # the tracker's steps: when each began, and for how long it ran
    t = progress.Tracker([("download", "Download photos", 1), ("video", "YouTube", 1, True), ("features", "Find features", 1),
                          ("mapper", "Place cameras", 1)])
    t.update("download", 0.5)
    t.update("video", 0.1)
    time.sleep(0.2)
    t.update("features", 0.2)      # ends the download, not the video alongside it
    t.end("video")
    t.finish()
    steps = {s["key"]: s for s in t.snapshot()["steps"]}
    assert steps["download"]["seconds"] >= 0.2 and "started" in steps["features"] and "seconds" in steps["video"], steps
    assert "started" not in steps["mapper"] and "seconds" not in steps["mapper"], "never ran"

    # a mesh's record: where it was made, what its tools took, and its steps' times
    rec = timings.record(REMOTE, t.snapshot(), 1000.0, 1400.0, "this server", "abc1234", True)
    assert rec["where"] == "hal-2" and rec["remote"] and rec["wall"] == 400 and rec["version"] == "abc1234"
    assert rec["tools_seconds"] == 4 + 45 + 5 + 17 + 21 + 3 + 10 + 17 + 6 and rec["cpu_seconds"] == 9 + 800 + 7 + 18 + 22 + 5 + 95 + 319 + 99
    assert rec["peak_gb"] == 18.7 and [s["key"] for s in rec["steps"]] == ["download", "video", "features"], rec["steps"]
    local = timings.record("== COLMAP: feature extraction\n-- ran 30 s, using 60 s of CPU\nregistered 30/31 images\n", None, 0, 90,
                           "this server", None, False)
    assert local["where"] == "this server" and local["vggt_seconds"] is None and local["waited"] is None and local["peak_gb"] is None
    timings.save(tmp, rec)
    assert timings.load(tmp) == json.loads(json.dumps(rec)) and timings.load(tmp / "nonesuch") is None

    # an old log, of a failed run and then one that saved a mesh: the last, with what the machine said first
    old = "harvest for 'x'\n== COLMAP: feature extraction (x)\n-- ran 99 s, using 9 s of CPU\nFAILED: too few\n-- trying again\n" + REMOTE
    run = timings.last_run(old)
    assert run.startswith("remote: 25 MB ready") and "-- ran 99 s" not in run and "finished in 5 min" in run, run
    assert timings.last_run("== COLMAP: feature extraction\n-- ran 3 s, using 1 s of CPU\nFAILED: x\n") is None, "no mesh saved"
    projects, meshes = tmp / "projects", tmp / "meshes"
    (projects / "p1").mkdir(parents=True)
    (projects / "p1" / "job.log").write_text(old)
    for mid in ("m1", "m2", "m3"):
        (meshes / mid).mkdir(parents=True)
    timings.save(meshes / "m3", {"already": True})
    rows = [{"id": "m1", "source": "project:p1", "created": 5000.0}, {"id": "m2", "source": "upload", "created": 1.0},
            {"id": "m3", "source": "project:p1", "created": 1.0}]
    assert timings.backfill(rows, projects, meshes) == 1 and timings.load(meshes / "m3") == {"already": True}
    back = timings.load(meshes / "m1")
    assert back["from_log"] and back["where"] == "hal-2" and back["ended"] == 5000 and back["wall"] is None and back["placed"] == 9
    assert timings.backfill(rows, projects, meshes) == 0, "once"

    # the admin page's medians, by what made them and how many photos went in
    recs = [rec, {**rec, "tools_seconds": 300, "ended": 2000}, local, {**local, "photos_in": 85}, back]
    rows = timings.summarize(recs)
    assert [(r["where"], r["photos"], r["n"]) for r in rows] == [("hal-2", "20-40", 3), ("this server", "20-40", 1),
                                                                 ("this server", "80+", 1)], rows
    assert rows[0]["vggt"] == 3 and rows[0]["waited"] == 166 and rows[0]["wall_minutes"] == round(400 / 60, 1) and rows[0]["from_log"] == 1
    assert [r["n"] for r in timings.summarize(recs, since=1500)] == [2], "only those since"
    print("ALL TIMINGS TESTS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
