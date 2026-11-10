"""Offline tests of the pipeline's control flow and progress parsing, with COLMAP/OpenMVS stubbed out.

Run: python3 tests/test_pipeline.py
"""
import json, os, pathlib, struct, sys, tempfile, time, types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
import occlusion
import pipeline
from climbs.common import Cancelled
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
# ending with too little gets another seed too, because the mapper can throw away a model it had grown
assert stops_at([{"models": {"0": 3}}, {"models": {"0": 23}}]).endswith("sparse/0")
assert stops_at([{"models": {"0": 2}}, {"models": {"0": 1}}, {"models": {"0": 9}}]).endswith("sparse/0")
assert stops_at([{"models": {"0": 9}}]).endswith("sparse/0"), "enough first time shouldn't try again"
with tempfile.TemporaryDirectory() as td:
    try:
        run_with_mapper([{"models": {"0": 2}}, {"models": {"0": 3}}, {"models": {}}], pathlib.Path(td))
        raise AssertionError("too small a model should fail")
    except RuntimeError as e:
        assert "only 3 image(s)" in str(e), e
print("mapper abort OK")


# ---- people masks reach every step that can use them, and only cost the masking if they fail
def commands_with(write_masks, setup=None, check=None) -> tuple[list[list[str]], list[str]]:
    """The commands a reconstruction runs. setup(tmp) runs before it, and check(tmp) after."""
    with tempfile.TemporaryDirectory() as td:
        tmp, cmds, logged = pathlib.Path(td), [], []
        images, work = tmp / "images", tmp / "work"
        images.mkdir()
        for i in range(8):
            (images / f"{i}.jpg").write_bytes(b"x")
        if setup:
            setup(tmp)

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
        if check:
            check(tmp)
        return cmds, logged


def by_tool(cmds, name):
    return next(c for c in cmds if name in c[:2])


cmds, _ = commands_with(lambda *a, **k: {})
assert "--ImageReader.mask_path" in by_tool(cmds, "feature_extractor"), cmds
for tool in ("DensifyPointCloud", "TextureMesh"):
    c = by_tool(cmds, tool)
    assert c[c.index("--ignore-mask-label") + 1] == "0", c
assert "--mask-path" in by_tool(cmds, "DensifyPointCloud")
mask_dir = by_tool(cmds, "feature_extractor")[by_tool(cmds, "feature_extractor").index("--ImageReader.mask_path") + 1]
assert mask_dir.endswith("/masks") and "/work/" not in mask_dir, "kept next to the photos, as work/ is deleted"


def legacy(tmp):    # masks from before they were kept, in work/
    (tmp / "work" / "masks").mkdir(parents=True)
    (tmp / "work" / "masks" / "0.jpg.png").write_bytes(b"m")


def moved(tmp):
    assert (tmp / "masks" / "0.jpg.png").read_bytes() == b"m" and not (tmp / "work" / "masks").exists()


commands_with(lambda *a, **k: {}, setup=legacy, check=moved)


def failing(*a, **k):
    raise OSError("no network")


cmds, logged = commands_with(failing)
assert "--ImageReader.mask_path" not in by_tool(cmds, "feature_extractor"), cmds
for tool in ("DensifyPointCloud", "TextureMesh"):
    assert "--ignore-mask-label" not in by_tool(cmds, tool), cmds
assert any("not masking people" in l for l in logged), logged
print("people masks OK")

# ---- masks are only made for photos without an up-to-date one, and say how much of each is people
try:
    import numpy as np
    import cv2  # noqa: F401
except ImportError:
    print("numpy or OpenCV not installed: skipping the mask files test")
else:
    with tempfile.TemporaryDirectory() as td:
        images, out, looked = pathlib.Path(td) / "photos", pathlib.Path(td) / "masks", []
        images.mkdir()
        for i in range(3):
            (images / f"{i}.jpg").write_bytes(b"x")

        def fake_mask(path, log, grow=0.02):
            looked.append(path.name)
            m = np.zeros((4, 4), bool)
            m[:2, :1] = True     # an eighth of it
            return m
        saved, occlusion.person_mask = occlusion.person_mask, fake_mask
        try:
            shares = occlusion.write_masks(images, out, lambda f: f"{f.name}.png", print)
            assert looked == ["0.jpg", "1.jpg", "2.jpg"] and shares == {f"{i}.jpg": 0.125 for i in range(3)}, (looked, shares)
            assert json.loads((out / occlusion.SHARES).read_text()) == shares
            looked.clear()
            assert occlusion.write_masks(images, out, lambda f: f"{f.name}.png", print) == shares and looked == [], looked
            future = time.time() + 5
            os.utime(images / "1.jpg", (future, future))     # changed since its mask was made
            (images / "3.jpg").write_bytes(b"x")
            occlusion.write_masks(images, out, lambda f: f"{f.name}.png", print)
            assert looked == ["1.jpg", "3.jpg"], looked
        finally:
            occlusion.person_mask = saved
    print("mask files OK")


# ---- the GPU is used if it's there and has room, and failing on it only costs the time
def gpu_run(cuda, free, internet=True, fails=(), raises=None) -> tuple[list[list[str]], list[str]]:
    with tempfile.TemporaryDirectory() as td:
        tmp, cmds, logged = pathlib.Path(td), [], []
        images, work = tmp / "images", tmp / "work"
        images.mkdir()
        for i in range(8):
            (images / f"{i}.jpg").write_bytes(b"x")

        def fake_run(cmd, cwd, log, watch=None):
            cmds.append(cmd)
            if cmd[1] == "exhaustive_matcher" and "1" == cmd[-1] and raises:
                raise raises
            if cmd[1] in fails and cmd[-1] == "1":
                raise RuntimeError("colmap exited with -6")
            if cmd[1] == "mapper":
                raise Stop()
        saved = pipeline._run, pipeline.COLMAP, pipeline.colmap_has_cuda, pipeline.gpu_free_mb, occlusion.available
        pipeline._run, pipeline.COLMAP = fake_run, "colmap"
        pipeline.colmap_has_cuda, pipeline.gpu_free_mb, occlusion.available = (lambda: cuda), (lambda: free), (lambda: False)
        try:
            pipeline.reconstruct_colmap_openmvs(images, work, tmp / "out", logged.append, True, internet=internet)
        except Stop:
            pass
        finally:
            pipeline._run, pipeline.COLMAP, pipeline.colmap_has_cuda, pipeline.gpu_free_mb, occlusion.available = saved
        return cmds, logged


def flags(cmds, tool, flag):
    return [c[c.index(flag) + 1] for c in cmds if c[1] == tool]


MATCH, EXTRACT = "--SiftMatching.use_gpu", "--SiftExtraction.use_gpu"
cmds, logged = gpu_run(True, 17000)
assert flags(cmds, "exhaustive_matcher", MATCH) == ["1"], cmds
assert flags(cmds, "feature_extractor", EXTRACT) == ["0"], "COLMAP's GPU SIFT can't do the internet settings"
assert any("exhaustive_matcher: using the GPU (17000 MB" in l for l in logged), logged
cmds, _ = gpu_run(True, 17000, internet=False)
assert flags(cmds, "feature_extractor", EXTRACT) == ["1"] and flags(cmds, "exhaustive_matcher", MATCH) == ["1"], cmds
for cuda, free, why in ((False, 17000, "without CUDA"), (True, None, "no GPU visible"), (True, 228, "only 228 MB")):
    cmds, logged = gpu_run(cuda, free)
    assert flags(cmds, "exhaustive_matcher", MATCH) == ["0"], cmds
    assert any(why in l for l in logged if "exhaustive_matcher" in l), logged
assert pipeline.gpu_choice(False)[0] is False
cmds, logged = gpu_run(True, 17000, fails=("exhaustive_matcher",))   # out of memory partway: carries on, then on to the mapper
assert flags(cmds, "exhaustive_matcher", MATCH) == ["1", "0"] and any(c[1] == "mapper" for c in cmds), cmds
gpu_cmd, cpu_cmd = [c for c in cmds if c[1] == "exhaustive_matcher"]
assert gpu_cmd[gpu_cmd.index("--TwoViewGeometry.min_num_inliers") + 1] == "12", gpu_cmd
assert "--TwoViewGeometry.min_num_inliers" not in cpu_cmd, "its lower minimum is only for the GPU's matcher"
assert any("GPU run failed" in l for l in logged), logged
try:
    gpu_run(True, 17000, raises=Cancelled())
    raise AssertionError("stopping a job mustn't turn into a CPU run")
except Cancelled:
    pass

saved = pipeline.subprocess.run
pipeline.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="6518\n17610\n")
assert pipeline.gpu_free_mb() == 17610
pipeline.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=9, stdout="")
assert pipeline.gpu_free_mb() is None, "nvidia-smi fails when a container has no GPU"
pipeline.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("nvidia-smi"))
assert pipeline.gpu_free_mb() is None
pipeline.subprocess.run = saved
print("gpu OK")


# ---- levelling: a reading it believes turns the model and is kept; anything else keeps the estimate
def levelled(status, fails=False):
    """_level_from_readings() with a model of 8 placed photos, whose check says this."""
    logged, cmds = [], []
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        model = tmp / "aligned"
        model.mkdir()

        def fake_run(cmd, cwd, log, watch=None):
            cmds.append(cmd)
            if fails:
                raise RuntimeError("colmap exited with 1")
        phone = {"status": status, "kept_with_readings": 1, "placed_with_readings": 1}
        if status in ("used", "rejected"):
            phone.update(down=[0.0, -0.8, 0.6], off_estimate_deg=11.5, readings=[{"photo": "a.jpg"}], readings_agree_deg=0.0)
        fake = {"placed": 8, "method": "photos", "estimate": {"down": [0.0, -1.0, 0.0]}, "iphone": phone}
        saved = pipeline._run, pipeline.COLMAP, pipeline.orientation.check, pipeline.orientation.available
        pipeline._run, pipeline.COLMAP = fake_run, "colmap"
        pipeline.orientation.check, pipeline.orientation.available = (lambda m, i: fake), (lambda: True)
        try:
            report = pipeline._level_from_readings(model, tmp, tmp, logged.append)
            transform = (tmp / "gravity.txt").read_text() if (tmp / "gravity.txt").exists() else None
        finally:
            pipeline._run, pipeline.COLMAP, pipeline.orientation.check, pipeline.orientation.available = saved
        return report, cmds, transform, logged


report, cmds, transform, logged = levelled("used")
assert [c[1] for c in cmds] == ["model_transformer"] and transform.startswith("1 ") and transform.strip().endswith("0 0 0"), (cmds, transform)
assert report["method"] == "iphone" and abs(report["iphone"]["down"][1] + 1) < 1e-9, report
assert abs(report["estimate"]["down"][1] + 1) > 0.01, "the estimate moves with the model, to compare in the mesh's coordinates"
assert any("11.5 degrees away" in line for line in logged), logged
report, cmds, _, logged = levelled("rejected")
assert not cmds and report["method"] == "photos" and any("too far to believe" in line for line in logged), (cmds, logged)
report, cmds, _, logged = levelled("no_readings")
assert not cmds and report["method"] == "photos" and any("none of the 8 placed" in line for line in logged), (cmds, logged)
report, cmds, _, logged = levelled("used", fails=True)
assert report is None and any("keeping the estimate" in line for line in logged), logged
with tempfile.TemporaryDirectory() as td:
    assert pipeline._level_from_readings(pathlib.Path(td) / "sparse0", pathlib.Path(td), pathlib.Path(td), print) is None, "levelling failed: nothing to check"
print("levelling OK")

cmds, _ = commands_with(lambda *a, **k: {})
c = by_tool(cmds, "TextureMesh")
assert c[c.index("--max-texture-size") + 1] == str(pipeline.MAX_TEXTURE) == "4096", c
print("texture size OK")


# ---- an interrupted job carries on from the last step it finished
def run_until(tmp, stop_at=None, faces=None):
    """reconstruct_colmap_openmvs() on the same work/, raising Stop at a tool as a restart would. Returns the tools it ran.

    Args:
        faces: the faces ReconstructMesh's mesh says it has; without, a file with no header.
    """
    images, work, tools = tmp / "images", tmp / "work", []
    images.mkdir(exist_ok=True)
    for i in range(8):
        (images / f"{i}.jpg").write_bytes(b"x")

    def fake_run(cmd, cwd, log, watch=None):
        name = cmd[1] if cmd[0] == "colmap" else cmd[0]
        tools.append(name)
        if name == stop_at:
            if name == "mapper":
                model(work / "sparse", "half", 3)    # what a killed mapper leaves
            raise Stop()
        if name == "mapper":
            model(work / "sparse", "0", 8)
        if name == "model_orientation_aligner":
            (work / "aligned").mkdir(exist_ok=True)
            (work / "aligned" / "images.bin").write_bytes(struct.pack("<Q", 0))
        if name == "image_undistorter":
            (work / "dense" / "images").mkdir(parents=True, exist_ok=True)
        if name == "ReconstructMesh":
            (work / "scene_dense_mesh.ply").write_bytes(b"ply" if faces is None else
                                                        f"ply\nformat ascii 1.0\nelement vertex 6\nelement face {faces}\nend_header\n".encode())
            (work / "scene_dense_mesh.ply").chmod(0o600)     # as OpenMVS leaves it
    saved = pipeline._run, pipeline.COLMAP, pipeline._mvs, occlusion.available, occlusion.write_masks, pipeline.orientation.check
    pipeline._run, pipeline.COLMAP, pipeline._mvs = fake_run, "colmap", lambda name: name
    occlusion.available, occlusion.write_masks = (lambda: True), (lambda *a, **k: {})
    pipeline.orientation.check = lambda m, i: {"placed": 8, "method": "photos", "marker": "kept", "estimate": {"down": [0, -1, 0]}, "iphone": {"status": "no_readings"}}
    try:
        pipeline.reconstruct_colmap_openmvs(images, work, tmp / "out", lambda s: None, False)
    except Stop:
        pass
    finally:
        pipeline._run, pipeline.COLMAP, pipeline._mvs, occlusion.available, occlusion.write_masks, pipeline.orientation.check = saved
    return tools


ALL = ["feature_extractor", "exhaustive_matcher", "mapper", "model_orientation_aligner", "model_transformer", "image_undistorter",
       "InterfaceCOLMAP", "DensifyPointCloud", "ReconstructMesh", "TextureMesh"]
for stop in ALL[:2] + ALL[5:]:
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        first = run_until(tmp, stop)
        assert first == ALL[:ALL.index(stop) + 1], (stop, first)
        second = run_until(tmp)
        assert second == ALL[ALL.index(stop):], (stop, second)
        assert run_until(tmp)[:1] == ["TextureMesh"] or (tmp / "out" / "mesh.ply").exists()
with tempfile.TemporaryDirectory() as td:   # the mapper starts over, without trusting what it left
    tmp = pathlib.Path(td)
    assert run_until(tmp, "mapper")[-1] == "mapper"
    assert run_until(tmp)[0] == "mapper"
    assert (tmp / "work" / "interrupted_sparse0" / "half").is_dir() and not (tmp / "work" / "sparse" / "half").exists()
    assert (tmp / "work" / "sparse" / "0").is_dir()
    assert json.loads((tmp / "out" / "orientation.json").read_text())["marker"] == "kept"
with tempfile.TemporaryDirectory() as td:   # levelling isn't done twice, and its report survives to reach the mesh
    tmp = pathlib.Path(td)
    run_until(tmp, "DensifyPointCloud")
    assert "model_orientation_aligner" not in run_until(tmp)
    assert json.loads((tmp / "out" / "orientation.json").read_text())["marker"] == "kept"
print("resuming OK")

# a mesh with too few faces isn't one: the reconstruction fails, saying why, rather than saving it
for n in (0, pipeline.MIN_FACES - 1):
    with tempfile.TemporaryDirectory() as td:
        try:
            run_until(pathlib.Path(td), faces=n)
            raise AssertionError("an empty mesh should fail")
        except RuntimeError as e:
            assert f"only {n} face(s)" in str(e), e
            assert not (pathlib.Path(td) / "out").exists(), "nothing saved"
with tempfile.TemporaryDirectory() as td:
    assert run_until(pathlib.Path(td), faces=50_000)[-1] == "TextureMesh"
    assert (pathlib.Path(td) / "out" / "mesh.ply").stat().st_mode & 0o044 == 0o044, "the untextured mesh readable by all, as the rest"
assert pipeline.ply_faces(pathlib.Path("/nonexistent")) is None
print("empty meshes OK")
print("ALL PIPELINE TESTS PASSED")
