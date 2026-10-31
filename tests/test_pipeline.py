"""Offline tests of the pipeline's control flow and progress parsing, with COLMAP/OpenMVS stubbed out.

Run: python3 tests/test_pipeline.py
"""
import pathlib, struct, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
import occlusion
import pipeline
from progress import Tracker

# ---- progress, from lines as COLMAP and OpenMVS print them
t = Tracker(pipeline.steps() if pipeline.real_pipeline_ready() else [(k, k, 1) for k in
            ("features", "matching", "mapper", "undistort", "dense", "mesh", "texture")])
frac = lambda key: next(s["frac"] for s in t.snapshot()["steps"] if s["key"] == key)
detail = lambda key: next(s["detail"] for s in t.snapshot()["steps"] if s["key"] == key)

w = pipeline._phases(t, "features", (r"Processed file \[(\d+)/(\d+)\]", 0, 1))
w("I20261030 07:44:04.486254   105 feature_extraction.cc:254] Processed file [18/72]")
assert frac("features") == 0.25, frac("features")
w("  Name:            yt_05c6e817_001.jpg")
assert frac("features") == 0.25, "other lines must not move it"

w = pipeline._matching(t)
w("I20261030 07:44:25.833164   107 feature_matching.cc:231] Matching block [1/2, 1/2]")
assert frac("matching") == 0 and detail("matching") == "block 1 of 4", (frac("matching"), detail("matching"))
w("I20261030 07:48:14.438452   107 feature_matching.cc:231] Matching block [2/2, 1/2]")
assert frac("matching") == 0.5, frac("matching")

w = pipeline._mapper(t, 72)
w("Registering image #47 (18)")
assert frac("mapper") == 0.25 and "18 of 72" in detail("mapper"), detail("mapper")
w("Registering image #39 (3)")   # a new model starts over: the bar stays put
assert frac("mapper") == 0.25, frac("mapper")

w = pipeline._geometric(t)
w("Estimated depth-maps 16 (50.00%, 2s, ETA 1m)...")
assert abs(frac("dense") - 0.225) < 1e-6, frac("dense")
w("Geometric-consistent estimated depth-maps 33 (100.00%, 9s, ETA 0ms)...")
assert abs(frac("dense") - 0.65) < 1e-6, frac("dense")
w("Geometric-consistent estimated depth-maps 1 (10.00%, 1s, ETA 9s)...")      # the second pass
assert abs(frac("dense") - 0.67) < 1e-6, frac("dense")
w("Fused depth-maps 33 (100.00%, 2s)")
assert frac("dense") == 1, frac("dense")

w = pipeline._phases(t, "mesh", (r"Points inserted \d+ \(([\d.]+)%", 0, 0.5), (r"Points weighted \d+ \(([\d.]+)%", 0.5, 0.9))
w("Points inserted 600000 (50.00%, 3s, ETA 3s)...")
assert frac("mesh") == 0.25, frac("mesh")
w = pipeline._phases(t, "texture", (r"Assigning the best view .*completed", 0.3, 0.6))
w("18:47:25 [App     ] Assigning the best view to each face completed: 139254 faces (8s500ms)")
assert frac("texture") == 0.6, frac("texture")
print("progress parsing OK")


# ---- a mapper abort falls back on the models it finished
class Stop(Exception):
    pass


def model(sparse: pathlib.Path, name: str, n: int) -> None:
    d = sparse / name
    d.mkdir(parents=True)
    (d / "images.bin").write_bytes(struct.pack("<Q", n))


def run_with_mapper(attempts: list, tmp: pathlib.Path) -> tuple[list[str], list[list[str]]]:
    """reconstruct_colmap_openmvs(), with each mapper attempt leaving models {name: n} behind.

    An attempt fails if it says "abort": True. Stops at levelling the model, raising Stop with the
    model it was given.
    """
    images, work, logged, seen = tmp / "images", tmp / "work", [], []
    images.mkdir()
    for i in range(8):
        (images / f"{i}.jpg").write_bytes(b"x")
    attempts = list(attempts)

    def fake_run(cmd, cwd, log, watch=None):
        if cmd[1] == "mapper":
            seen.append(cmd)
            attempt = attempts.pop(0)
            for name, n in attempt["models"].items():
                model(work / "sparse", name, n)
            if attempt.get("abort"):
                raise RuntimeError("colmap exited with -6")
        if cmd[1] == "model_orientation_aligner":
            raise Stop(cmd[cmd.index("--input_path") + 1])
    run, pipeline._run, pipeline.COLMAP = pipeline._run, fake_run, "colmap"
    try:
        pipeline.reconstruct_colmap_openmvs(images, work, tmp / "out", logged.append, False)
    finally:
        pipeline._run = run
    return logged, seen


def stops_at(attempts: list) -> str:
    with tempfile.TemporaryDirectory() as td:
        try:
            run_with_mapper(attempts, pathlib.Path(td))
        except Stop as e:
            return str(e)
    raise AssertionError("should have carried on to levelling the model")


ABORT = {"abort": True}
# a retry succeeds: its model is used, and each attempt had its own seed
with tempfile.TemporaryDirectory() as td:
    try:
        run_with_mapper([{**ABORT, "models": {"0": 5}}, {"models": {"0": 30}}], pathlib.Path(td))
        raise AssertionError("should have carried on")
    except Stop as e:
        assert str(e).endswith("sparse/0"), e
assert stops_at([{**ABORT, "models": {"0": 25}}, {"models": {"0": 3}}]).endswith("aborted0-0"), \
    "a larger model from an aborted attempt should win"
# every attempt aborts: the best of what they finished is used
assert stops_at([{**ABORT, "models": {"0": 3}}, {**ABORT, "models": {"0": 20}}, {**ABORT, "models": {"0": 7}}]).endswith("aborted1-0")
# only too-small models
with tempfile.TemporaryDirectory() as td:
    try:
        run_with_mapper([{**ABORT, "models": {"0": 2}}] * pipeline.MAPPER_ATTEMPTS, pathlib.Path(td))
        raise AssertionError("too small a model should fail")
    except RuntimeError as e:
        assert "only 2 image(s)" in str(e), e
print("mapper abort OK")


# ---- people masks reach every step that can use them, and only cost the masking if they fail
def commands_with(write_masks) -> tuple[list[list[str]], list[str]]:
    with tempfile.TemporaryDirectory() as td:
        tmp, cmds, logged = pathlib.Path(td), [], []
        images, work = tmp / "images", tmp / "work"
        images.mkdir()
        for i in range(8):
            (images / f"{i}.jpg").write_bytes(b"x")

        def fake_run(cmd, cwd, log, watch=None):
            cmds.append(cmd)
            if cmd[1] == "mapper":
                model(work / "sparse", "0", 8)
            if cmd[1] == "image_undistorter":
                (work / "dense" / "images").mkdir(parents=True)
            if "ReconstructMesh" in cmd[0]:
                (work / "scene_dense_mesh.ply").write_bytes(b"ply")
        saved = pipeline._run, pipeline.COLMAP, pipeline._mvs, occlusion.available, occlusion.write_masks
        pipeline._run, pipeline.COLMAP, pipeline._mvs = fake_run, "colmap", lambda name: name
        occlusion.available, occlusion.write_masks = (lambda: True), write_masks
        try:
            pipeline.reconstruct_colmap_openmvs(images, work, tmp / "out", logged.append, False)
        finally:
            pipeline._run, pipeline.COLMAP, pipeline._mvs, occlusion.available, occlusion.write_masks = saved
        return cmds, logged


def by_tool(cmds, name):
    return next(c for c in cmds if name in c[:2])


cmds, _ = commands_with(lambda *a, **k: {})
assert "--ImageReader.mask_path" in by_tool(cmds, "feature_extractor"), cmds
for tool in ("DensifyPointCloud", "TextureMesh"):
    c = by_tool(cmds, tool)
    assert c[c.index("--ignore-mask-label") + 1] == "0", c
assert "--mask-path" in by_tool(cmds, "DensifyPointCloud")


def failing(*a, **k):
    raise OSError("no network")


cmds, logged = commands_with(failing)
assert "--ImageReader.mask_path" not in by_tool(cmds, "feature_extractor"), cmds
for tool in ("DensifyPointCloud", "TextureMesh"):
    assert "--ignore-mask-label" not in by_tool(cmds, tool), cmds
assert any("not masking people" in l for l in logged), logged
print("people masks OK")
print("ALL PIPELINE TESTS PASSED")
