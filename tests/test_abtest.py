#!/usr/bin/env python3
"""A/B tests of meshing with COLMAP 3.9.1 against 4.2.1: 3.9.1's option names given it (pipeline.use_colmap()), a
machine's job making both from the same photos (remote_job.py --task abtest), and the server queueing them, in turns
of which goes first, and keeping what comes back. Offline: COLMAP and the pipeline stand in.

    python3 tests/test_abtest.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = tempfile.mkdtemp(prefix="bouldermesh-abtest-")
os.environ["DATA_DIR"] = tmp

import pipeline  # noqa: E402
import remote_job  # noqa: E402
import server  # noqa: E402

try:
    # ---- 3.9.1's option names where 4.2.1's differ, only within use_colmap("3.9.1")
    ran = []
    real = pipeline.jobctl.popen
    pipeline.COLMAP391, pipeline.COLMAP4 = "colmap391", "colmap4"

    class Proc:
        pid, stdout = 1, iter([])

        def wait(self):
            return 0
    pipeline.jobctl.popen = lambda cmd, **k: (ran.append(cmd), Proc())[1]
    try:
        with pipeline.use_colmap("3.9.1"):
            pipeline._run([pipeline.COLMAP, "feature_extractor", "--FeatureExtraction.use_gpu", "1", "--default_random_seed", "0"],
                          pathlib.Path(tmp), lambda s: None)
        with pipeline.use_colmap("4.2.1"):
            pipeline._run([pipeline.COLMAP, "feature_extractor", "--FeatureExtraction.use_gpu", "1"], pathlib.Path(tmp), lambda s: None)
    finally:
        pipeline.jobctl.popen = real
    assert ran[0] == ["colmap391", "feature_extractor", "--SiftExtraction.use_gpu", "1", "--random_seed", "0"], ran
    assert ran[1] == ["colmap4", "feature_extractor", "--FeatureExtraction.use_gpu", "1"], ran
    print("3.9.1's options OK")

    # ---- a machine's job: both made from the same photos, a summary of each
    job = pathlib.Path(tmp) / "job"
    (job / "photos").mkdir(parents=True)
    (job / "task.json").write_text(json.dumps({"order": ["3.9.1", "4.2.1"]}))
    made = []

    def reconstruct(images, work, out, log, gpu, internet=False, tracker=None):
        made.append(pipeline.COLMAP)
        if pipeline.COLMAP == "colmap391":
            raise RuntimeError("mapper failed")
        out.mkdir(parents=True)
        (out / "check.json").write_text(json.dumps({"ok": True, "why": "fine"}))
        log("== COLMAP: sparse reconstruction (mapper)")
        log("-- ran 30 s, using 60 s of CPU")
        log("registered 9/10 images in the largest model")
        return out / "mesh.glb", False
    real_rec = pipeline.reconstruct
    pipeline.reconstruct = reconstruct
    try:
        assert remote_job.abtest_task(job, False, lambda s: None) == 0
    finally:
        pipeline.reconstruct = real_rec
    s = json.loads((job / "out" / "summary.json").read_text())
    assert made == ["colmap391", "colmap4"] and s["order"] == ["3.9.1", "4.2.1"]
    assert "mapper failed" in s["runs"]["3.9.1"]["failed"] and s["runs"]["4.2.1"]["placed"] == 9 and s["runs"]["4.2.1"]["check"]["ok"]
    assert s["runs"]["4.2.1"]["tools_seconds"] == 30 and (job / "out" / "4.2.1" / "job.log").exists()
    import compute
    assert compute.follow('@@result {"mesh": "", "abtest": true}', None, lambda s: None), "the server takes it as a result"
    assert compute.follow('@@result {"abtest": true}', None, lambda s: None), "and the older image's"
    print("a machine's job OK")

    # ---- the server: queued in turns of which goes first, kept as it comes back
    server.init_db()
    for pid in ("p1", "p2"):
        (server.PROJECTS / pid / "photos").mkdir(parents=True)
    assert server.queue_abtests([("p1", "x"), ("p2", "y")]) == 2
    reqs = [json.loads((d / "request.json").read_text()) for d in sorted(server.ABTESTS.iterdir())]
    assert [r["order"][0] for r in reqs] == ["4.2.1", "3.9.1"], reqs
    try:
        server.queue_abtests([("nonesuch", "z")])
        raise AssertionError("no such project")
    except ValueError:
        pass
    d = sorted(server.ABTESTS.iterdir())[0]
    (d / "summary.json").write_text(json.dumps(s))
    st = server.abtests_state()
    done = next(t for t in st if t["id"] == d.name)
    assert done["state"] == "done" and done["runs"]["4.2.1"]["placed"] == 9 and done["runs"]["4.2.1"]["check"] is True, done
    assert sum(t["state"] == "waiting" for t in st) == 1
    print("queued and kept OK")
    print("ALL ABTEST TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
