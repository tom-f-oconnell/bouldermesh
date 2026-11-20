#!/usr/bin/env python3
"""Adding photos to a mesh's kept model (backend/incremental.py): the gates that call for a full re-solve instead, with
what each measured; photos linked and joined to a model; and the pipeline's side of an add (pipeline._add_photos()),
which leaves work/ as placing every photo would have, or as if nothing had been tried. Offline: COLMAP and pycolmap
stand in. The add itself, through pycolmap, is tried in the machines' image (README, "Adding photos to a model").

    python3 tests/test_incremental.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import incremental  # noqa: E402
import pipeline  # noqa: E402
from progress import Tracker  # noqa: E402

# ---- the gates
due = incremental.resolve_due
d = due(40, 40, 3, 3, 0, 43, {"ok": True}, {"ok": True})
assert not d["due"] and d["gate"] is None and d["measured"]["growth"] == 0.075 and d["measured"]["placed_share"] == 1.0, d
assert due(40, 40, 25, 21, 0, 61, None, None)["gate"] == "growth", "grown by more than half since the last full solve"
assert due(40, 60, 10, 4, 0, 44, None, None)["gate"] == "placed_share", "under half of 6 or more linked new photos placed"
assert not due(40, 60, 5, 1, 0, 41, None, None)["due"], "too few tried to judge by"
assert due(40, 60, 2, 2, 11, 42, None, None)["gate"] == "joined", "new photos joined but left out"
assert not due(100, 150, 2, 2, 5, 102, None, None)["due"], "under a tenth of those placed"
assert due(40, 60, 2, 2, 0, 42, {"ok": False, "why": "x"}, {"ok": True})["gate"] == "check", "passed before, fails now"
assert not due(40, 60, 2, 2, 0, 42, {"ok": False, "why": "x"}, {"ok": False})["due"], "failed before as well"
print("gates OK")

# ---- photos linked and joined to a model
ids = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}
link = {1: {2: 50}, 2: {1: 50, 3: 30}, 3: {2: 30}, 4: {5: 40}, 5: {4: 40}}
assert incremental.linked_to(["b", "c", "d"], {"a"}, ids, link) == ["b"]
assert incremental.joined(["b", "c", "d"], {"a"}, ids, link) == ["b", "c"], "through another photo"
print("links OK")

# ---- the pipeline's side: an add, a re-solve, or none to be had
with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    images, mesh = td / "photos", td / "mesh"
    images.mkdir()
    mesh.mkdir()
    real_add, real_unpack = incremental.add, pipeline.reconstruction.unpack

    def unpack(mesh_dir, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "build.json").write_text(json.dumps({"extract": {"args": [], "device": "gpu"}, "mapper": {"mapper": "incremental"}}))
        return {}
    pipeline.reconstruction.unpack = unpack
    try:
        for outcome in ("add", "resolve", "unavailable", "failed", "none"):
            work = td / f"work-{outcome}"
            (work / "sparse").mkdir(parents=True)

            def add(kept, unpacked, images_, masks, work_, log, colmap, run, gpu, threads=0):
                (work_ / "database.db").write_bytes(b"db")
                (work_ / "sparse" / "0").mkdir(parents=True, exist_ok=True)
                if outcome == "resolve":
                    raise incremental.Resolve({"kind": "resolve", "why": "grown", "decision": {"gate": "growth"}})
                if outcome == "unavailable":
                    raise incremental.Unavailable("no pycolmap")
                if outcome == "failed":
                    raise ValueError("Check failed")
                if outcome == "none":
                    return {"kind": "none", "why": "no photos it doesn't have"}
                return {"kind": "add", "added": 3, "placed": 2, "decision": {"due": False, "gate": None}}
            incremental.add = add
            said = []
            got = pipeline._add_photos(mesh, images, None, work, said.append, Tracker([("add", "add", 1)]), False)
            rec = json.loads((work / incremental.ADD_FILE).read_text()) if (work / incremental.ADD_FILE).exists() else None
            if outcome == "add":
                assert got["kind"] == "add" and rec["kind"] == "add", (got, rec)
                assert all(pipeline._finished(work, s) for s in ("features", "matching", "mapper", "rest", "sift-judged"))
                assert json.loads((work / pipeline.MAPPER_FILE).read_text())["mapper"] == "add"
                assert json.loads((work / pipeline.EXTRACT_FILE).read_text())["device"] == "gpu", "as the mesh's features were made"
                assert (work / "database.db").exists() and (work / "sparse" / "0").is_dir()
            else:
                assert got is None and not pipeline._finished(work, "features"), outcome
                assert not (work / "database.db").exists() and not any((work / "sparse").iterdir()), "as if nothing was tried"
                assert (rec or {}).get("kind") == {"resolve": "resolve", "unavailable": "unavailable", "failed": "failed"}.get(outcome), (outcome, rec)
                assert any("placing every photo" in s or "FULL RE-SOLVE" in s for s in said), said
    finally:
        incremental.add, pipeline.reconstruction.unpack = real_add, real_unpack
print("the pipeline's side OK")
print("ALL INCREMENTAL TESTS PASSED")
