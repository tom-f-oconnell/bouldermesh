"""Offline tests of the pipeline's control flow and progress parsing, with COLMAP/OpenMVS stubbed out.

Run: python3 tests/test_pipeline.py
"""
import json, os, pathlib, struct, sys, tempfile, time, types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
os.environ["VGGT_FALLBACK"] = "0"      # VGGT stays out of it, whatever this machine has, unless a test stands it in
import occlusion
import pipeline
from climbs.common import Cancelled
from progress import Tracker

# the incremental mapper's tests below run as on a machine with no COLMAP 4, whatever this one has; the global
# mapper's own tests (near the end) stand it in
pipeline.COLMAP4 = None

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
w("I20261120 21:15:16.901552   266 pairing.cc:214] Processing block [2/2, 2/2]")     # COLMAP 4.2.1's
assert frac("matching") == 0.75, frac("matching")

w = pipeline._mapper(t, 72)
w("Registering image #47 (18)")
assert frac("mapper") == 0.25 and "18 of 72" in detail("mapper"), detail("mapper")
w("Registering image #39 (3)")   # a new model starts over: the bar stays put
assert frac("mapper") == 0.25, frac("mapper")
w("I20261120 21:15:36.754166   322 incremental_pipeline.cc:620] Registering image #6 (num_reg_frames=36)")     # COLMAP 4.2.1's
assert frac("mapper") == 0.5, frac("mapper")

w = pipeline._geometric(t)
w("Estimated depth-maps 16 (50.00%, 2s, ETA 1m)...")
assert abs(frac("dense") - 0.225) < 1e-6, frac("dense")
w("Geometric-consistent estimated depth-maps 33 (100.00%, 9s, ETA 0ms)...")
assert abs(frac("dense") - 0.65) < 1e-6, frac("dense")
w("Geometric-consistent estimated depth-maps 1 (10.00%, 1s, ETA 9s)...")      # the second pass
assert abs(frac("dense") - 0.67) < 1e-6, frac("dense")
w("Fused depth-maps 33 (100.00%, 2s)")
assert frac("dense") == 1, frac("dense")
t1 = Tracker([("dense", "dense", 1)])
w = pipeline._geometric(t1, passes=1)       # GEOMETRIC_ITERS: one pass is the whole of that phase
w("Geometric-consistent estimated depth-maps 33 (50.00%, 9s, ETA 9s)...")
assert abs(t1.snapshot()["steps"][0]["frac"] - 0.65) < 1e-6, t1.snapshot()
assert pipeline.MVS24_DENSIFY[-2:] == ["--geometric-iters", "1"], pipeline.MVS24_DENSIFY
# no region of interest guessed, so nothing of the rock is cropped (as in 2.3)
assert pipeline.MVS24_DENSIFY[:4] == ["--estimate-roi", "0", "--crop-to-roi", "0"], pipeline.MVS24_DENSIFY
assert pipeline.MVS24_MESH[:2] == ["--crop-to-roi", "0"], pipeline.MVS24_MESH

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


def run_with_mapper(attempts: list, tmp: pathlib.Path, n: int = 8) -> tuple[list[str], list[list[str]]]:
    """reconstruct_colmap_openmvs(), with each mapper attempt leaving models {name: n} behind.

    An attempt fails if it says "abort": True. Stops at levelling the model, raising Stop with the
    model it was given.
    """
    images, work, logged, seen = tmp / "images", tmp / "work", [], []
    images.mkdir()
    for i in range(n):
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


def stops_at(attempts: list, n: int = 8) -> str:
    with tempfile.TemporaryDirectory() as td:
        try:
            run_with_mapper(attempts, pathlib.Path(td), n)
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


def mapper_command() -> list[str]:
    """The mapper's command line, as reconstruct_colmap_openmvs() runs it."""
    with tempfile.TemporaryDirectory() as td:
        (pathlib.Path(td) / "images").mkdir()

        def fake_run(cmd, cwd, log, watch=None):
            if cmd[1] == "mapper":
                raise Stop(cmd)
        saved = pipeline._run, pipeline.COLMAP
        pipeline._run, pipeline.COLMAP = fake_run, "colmap"
        try:
            pipeline.reconstruct_colmap_openmvs(pathlib.Path(td) / "images", pathlib.Path(td) / "work", pathlib.Path(td) / "out",
                                                lambda s: None, False)
        except Stop as e:
            return e.args[0]
        finally:
            pipeline._run, pipeline.COLMAP = saved
    raise AssertionError("no mapper run")


cmd = mapper_command()     # models after the first are kept down to what a mesh needs, not COLMAP's own least of 10
assert cmd[cmd.index("--Mapper.min_model_size") + 1] == str(pipeline.MIN_REGISTERED) == "6", cmd
with tempfile.TemporaryDirectory() as td:
    try:
        run_with_mapper([{"models": {"0": 2}}, {"models": {"0": 3}}, {"models": {}}], pathlib.Path(td))
        raise AssertionError("too small a model should fail")
    except RuntimeError as e:
        assert "only 3 image(s)" in str(e), e
print("mapper abort OK")

# placing many fewer than it was given, an area can have another seed tried too (more seeds, under retry_below of the
# photos), and the model placing most is kept; by default none is (meshopts.MAPPER)
import meshopts  # noqa: E402

assert stops_at([{"models": {"0": 13}}], 20).endswith("sparse/0"), "no more seeds by default"
with meshopts.using({"retry_below": 0.75, "more": 1}):
    assert stops_at([{"models": {"0": 13}}, {"models": {"0": 19}}], 20).endswith("sparse/0"), "13 of 20: another seed, which placed 19"
    assert stops_at([{"models": {"0": 13}}, {"models": {"0": 9}}], 20).endswith("aborted0-0"), "the first placed more: it's kept"
    assert stops_at([{"models": {"0": 13}}, {**ABORT, "models": {"0": 4}}], 20).endswith("aborted0-0"), "an abort then isn't a failure"
    assert stops_at([{"models": {"0": 16}}], 20).endswith("sparse/0"), "16 of 20 is enough"
with meshopts.using({"retry_below": 0, "more": 3}):
    assert stops_at([{"models": {"0": 13}}], 20).endswith("sparse/0"), "off"
with meshopts.using({"retry_below": 0.9, "more": 2}):
    assert stops_at([{"models": {"0": 13}}, {"models": {"0": 17}}, {"models": {"0": 15}}], 20).endswith("aborted1-0"), \
        "two more seeds under 90%, the best of three kept"
assert meshopts.given() is None, "only inside using()"
print("mapper's other seeds OK")


# ---- the photos the mapper left out, tried again against its finished model: those placed join it; failing keeps it
def place_rest(placed_after=None, fails=None):
    """The mapper places 6 of 10; image_registrator then places placed_after in all (or fails, if fails is the step).
    Returns what was logged, the tools run after the mapper, and the model levelling was given."""
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        images, work, logged, ran = tmp / "images", tmp / "work", [], []
        images.mkdir()
        for i in range(10):
            (images / f"{i}.jpg").write_bytes(b"x")

        def fake_run(cmd, cwd, log, watch=None):
            name = cmd[1]
            if name in ("image_registrator", "bundle_adjuster"):
                ran.append(name)
                if name == fails:
                    raise RuntimeError("colmap exited with 1")
                if name == "image_registrator" and placed_after:
                    (pathlib.Path(cmd[cmd.index("--output_path") + 1]) / "images.bin").write_bytes(struct.pack("<Q", placed_after))
                    assert cmd[cmd.index("--input_path") + 1].endswith("sparse/0") and "--Mapper.abs_pose_min_num_inliers" in cmd, cmd
            if name == "mapper":
                model(work / "sparse", "0", 6)
            if name == "model_orientation_aligner":
                given = cmd[cmd.index("--input_path") + 1]
                raise Stop(given, vggt_place_count(pathlib.Path(given)), (work / "mapper_0").is_dir())
        saved = pipeline._run, pipeline.COLMAP
        pipeline._run, pipeline.COLMAP = fake_run, "colmap"
        try:
            with meshopts.using({"retry_below": 0, "more": 0}):
                pipeline.reconstruct_colmap_openmvs(images, work, tmp / "out", logged.append, False)
            raise AssertionError("didn't reach levelling")
        except Stop as e:
            return logged, ran, e.args
        finally:
            pipeline._run, pipeline.COLMAP = saved


def vggt_place_count(m):
    return pipeline.vggt_place.count(m, "images")


logged, ran, (given, n, kept) = place_rest(placed_after=9)
assert ran == ["image_registrator", "bundle_adjuster"] and given.endswith("sparse/0") and n == 9 and kept, (ran, given, n, kept)
assert any("placed 3 more of them" in s for s in logged) and any("registered 9/10 images" in s for s in logged), logged
logged, ran, (given, n, kept) = place_rest(placed_after=6)
assert ran == ["image_registrator"] and n == 6 and not kept and any("none of them could be placed" in s for s in logged), (ran, logged)
logged, ran, (given, n, kept) = place_rest(fails="image_registrator")
assert n == 6 and not kept and any("placing the rest failed" in s for s in logged), logged
print("placing the rest OK")


# ---- when the mapper places too few, VGGT's placements (refined by COLMAP) are another model to choose from
def with_vggt(why=None, placed=None, fails=None):
    """vggt_place standing in: unavailable for why, or placing that many photos as sparse/vggt, or raising fails."""
    calls = []

    def place(colmap, db, images, out, work, log, run):
        calls.append((db, images, out, work))
        if fails:
            raise fails
        model(out.parent, out.name, placed)
        return placed
    saved = pipeline.vggt_place.unavailable, pipeline.vggt_place.place
    pipeline.vggt_place.unavailable, pipeline.vggt_place.place = (lambda: why), place
    return calls, saved


def restore(saved):
    pipeline.vggt_place.unavailable, pipeline.vggt_place.place = saved


calls, saved = with_vggt(placed=12)
try:
    assert stops_at([{"models": {"0": 2}}] * pipeline.MAPPER_ATTEMPTS).endswith("sparse/vggt")
    db, images, out, work = calls[0]
    assert db.name == "database.db" and out == work.parent / "sparse" / "vggt" and work.name == "vggt", calls
    calls.clear()
    assert stops_at([{"models": {"0": 9}}]).endswith("sparse/0") and not calls, "enough placed: no VGGT"
finally:
    restore(saved)
calls, saved = with_vggt(placed=4)          # VGGT's model is too small as well: fails as before
try:
    with tempfile.TemporaryDirectory() as td:
        try:
            run_with_mapper([{"models": {"0": 2}}] * pipeline.MAPPER_ATTEMPTS, pathlib.Path(td))
            raise AssertionError("too small a model should fail")
        except RuntimeError as e:
            assert "only 4 image(s)" in str(e), e
finally:
    restore(saved)
crash = pipeline.vggt_place.RoundFailed({"round": 1, "of": 3, "angle": 8, "px": 150, "points_before": 0, "photos": 10,
                                         "why": "colmap crashed with SIGABRT (signal 6)"})
for why, fails, said in (("no CUDA GPU is visible", None, "but not here: no CUDA GPU is visible"),
                         (None, RuntimeError("only 1 photo(s) share verified matches"), "placing them with VGGT failed: only 1"),
                         (None, crash, "FALLBACK: vggt_round: round 1 of VGGT's placing failed (colmap crashed")):
    calls, saved = with_vggt(why=why, fails=fails)
    try:
        with tempfile.TemporaryDirectory() as td:
            try:
                run_with_mapper([{"models": {"0": 2}}] * pipeline.MAPPER_ATTEMPTS, pathlib.Path(td))
                raise AssertionError("too small a model should fail")
            except RuntimeError as e:
                assert "only 2 image(s)" in str(e), e
            fell = pathlib.Path(td) / "work" / pipeline.FALLBACKS_FILE
            if fails is crash:      # a round that failed is recorded with what it was, beside the mesh's other fallbacks
                got = json.loads(fell.read_text())
                assert [(f["step"], f["round"], f["angle"]) for f in got] == [("vggt_round", 1, 8)] and said[10:] in f"FALLBACK: {got[0]['step']}: {got[0]['why']}"[10:], got
    finally:
        restore(saved)
calls, saved = with_vggt(fails=Cancelled())     # stopping the job stops it, rather than being taken for a failure
try:
    with tempfile.TemporaryDirectory() as td:
        try:
            run_with_mapper([{"models": {"0": 2}}] * pipeline.MAPPER_ATTEMPTS, pathlib.Path(td))
            raise AssertionError("should have been stopped")
        except Cancelled:
            pass
finally:
    restore(saved)
print("VGGT placing OK")


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

# the undistorted photos' masks are the first ones moved (occlusion.warp_masks()): people are looked for again only in
# the photos that couldn't be, or in all of them if moving fails (as here above, where the stub undistorts nothing)
looks = []
for left, want in (([], 1), (["3.jpg"], 2)):
    looks.clear()
    saved_warp, occlusion.warp_masks = occlusion.warp_masks, (lambda *a, _left=left, **k: list(_left))
    saved_read, pipeline.orientation.read_cameras = pipeline.orientation.read_cameras, (lambda m: [])
    try:
        cmds, logged = commands_with(lambda images, *a, **k: looks.append(images.name) or {})
    finally:
        occlusion.warp_masks, pipeline.orientation.read_cameras = saved_warp, saved_read
    assert len(looks) == want and looks[0] == "images", (left, looks)
    assert "--ignore-mask-label" in by_tool(cmds, "DensifyPointCloud") and "--mask-path" in by_tool(cmds, "DensifyPointCloud"), cmds
    assert any("had no mask to move" in l for l in logged) == bool(left), logged
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

        # the masks of undistorted photos, moved from the first ones as the undistorter moved the photos
        import cv2
        first, und = pathlib.Path(td) / "first", pathlib.Path(td) / "und"
        first.mkdir()
        m = np.full((40, 60), 255, np.uint8)
        m[10:20, 30:40] = 0                             # a person
        for name in ("a.jpg", "b.jpg", "small.jpg"):
            cv2.imwrite(str(first / f"{name}.png"), m)
        lens = lambda model, w, h, *params: {"model": model, "width": w, "height": h, "params": list(params)}  # noqa: E731
        placed = [{"name": "a.jpg", "lens": lens(2, 60, 40, 50.0, 30.0, 20.0, 0.0)},           # no distortion
                  {"name": "b.jpg", "lens": lens(2, 60, 40, 50.0, 30.0, 20.0, 0.0)},
                  {"name": "small.jpg", "lens": lens(2, 30, 20, 25.0, 15.0, 10.0, 0.0)},       # not its mask's size
                  {"name": "none.jpg", "lens": lens(2, 60, 40, 50.0, 30.0, 20.0, 0.0)},        # no first mask
                  {"name": "odd.jpg", "lens": lens(9, 60, 40, 50.0, 30.0, 20.0, 0.0, 0, 0)}]   # a lens model it doesn't know
        cv2.imwrite(str(first / "odd.jpg.png"), m)
        flat = [{"name": "a.jpg", "lens": lens(1, 60, 40, 50.0, 50.0, 30.0, 20.0)},            # the same view
                {"name": "b.jpg", "lens": lens(1, 70, 40, 50.0, 50.0, 35.0, 20.0)}]            # 5 pixels more to the left
        flat += [{"name": c["name"], "lens": lens(1, 60, 40, 50.0, 50.0, 30.0, 20.0)} for c in placed[2:]]
        left = occlusion.warp_masks(first, placed, flat, und, lambda f: f"{f.stem}.mask.png", print)
        assert left == ["small.jpg", "none.jpg", "odd.jpg"], left
        a, b = (cv2.imread(str(und / f"{n}.mask.png"), cv2.IMREAD_GRAYSCALE) for n in ("a", "b"))
        assert (a == m).all(), "an undistorted lens the same as the photo's leaves the mask as it was"
        assert b.shape == (40, 70) and (b[10:20, 35:45] == 0).all() and (b == 0).sum() == 100, "moved with the lens's centre"
        assert (b[:, :5] == 255).all() and (b[:, 65:] == 255).all(), "outside the photo: nobody"
        shares = json.loads((und / occlusion.SHARES).read_text())
        assert set(shares) == {"a.jpg", "b.jpg"} and abs(shares["a.jpg"] - 100 / 2400) < 1e-9, shares
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


MATCH, EXTRACT = "--FeatureMatching.use_gpu", "--FeatureExtraction.use_gpu"
pipeline.GPU_SIFT = False       # the internet settings' own SIFT first; GPU_SIFT (on by default) below
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
# depth maps: OpenMVS's CUDA build where the image has one and a GPU has room, else (or if it fails) the CPU build
def dense_cmds(free, fails=False, cuda_dir=True):
    with tempfile.TemporaryDirectory() as td:
        tmp, cmds, logged = pathlib.Path(td), [], []
        images, work, cudabin = tmp / "images", tmp / "work", tmp / "mvs-cuda"
        images.mkdir()
        cudabin.mkdir()
        (cudabin / "DensifyPointCloud").write_bytes(b"")
        for i in range(8):
            (images / f"{i}.jpg").write_bytes(b"x")

        def fake_run(cmd, cwd, log, watch=None):
            cmds.append(cmd)
            if cmd[1] == "mapper":
                model(work / "sparse", "0", 8)
            if cmd[1] == "image_undistorter":
                (work / "dense" / "images").mkdir(parents=True)
            if cmd[0].endswith("DensifyPointCloud") and "--cuda-device" in cmd and fails:
                raise RuntimeError("DensifyPointCloud exited with -6")
            if "ReconstructMesh" in cmd[0]:
                (work / "scene_dense_mesh.ply").write_bytes(b"ply")
        saved = pipeline._run, pipeline.COLMAP, pipeline._mvs, pipeline.gpu_free_mb, occlusion.available, pipeline._MVS_CUDA_DIR
        pipeline._run, pipeline.COLMAP, pipeline._mvs = fake_run, "colmap", lambda name: name
        pipeline.gpu_free_mb, occlusion.available = (lambda: free), (lambda: False)
        pipeline._MVS_CUDA_DIR = str(cudabin) if cuda_dir else str(tmp / "none")
        try:
            pipeline.reconstruct_colmap_openmvs(images, work, tmp / "out", logged.append, True)
        finally:
            pipeline._run, pipeline.COLMAP, pipeline._mvs, pipeline.gpu_free_mb, occlusion.available, pipeline._MVS_CUDA_DIR = saved
        return [c for c in cmds if c[0].endswith("DensifyPointCloud")], [c[0] for c in cmds if "Mesh" in c[0]], logged


saved_version = pipeline.mvs_version
runs, others, dlog = dense_cmds(17000)
assert len(runs) == 1 and runs[0][0].endswith("mvs-cuda/DensifyPointCloud") and runs[0][-2:] == ["--cuda-device", "-1"], runs
assert others == ["ReconstructMesh", "TextureMesh"], "the other steps stay with the CPU build"
assert any("DensifyPointCloud: using the GPU (17000 MB" in l for l in dlog), dlog
for free, why in ((None, "no GPU visible"), (500, "only 500 MB")):
    runs, _, dlog = dense_cmds(free)
    assert [r[0] for r in runs] == ["DensifyPointCloud"] and "--cuda-device" not in runs[0], runs
    assert any(why in l for l in dlog), dlog
pipeline.mvs_version = lambda: (2, 4)
runs, _, dlog = dense_cmds(17000, fails=True)
assert runs[1].count("--fusion-filter") == 1, "the CPU fallback is given 2.4's values too, once"
pipeline.mvs_version = saved_version
assert [r[0].rsplit("/", 2)[-2:] for r in runs] == [["mvs-cuda", "DensifyPointCloud"], ["DensifyPointCloud"]], runs
assert any("GPU run failed" in l for l in dlog), dlog
runs, _, dlog = dense_cmds(17000, cuda_dir=False)
assert [r[0] for r in runs] == ["DensifyPointCloud"] and not any("DensifyPointCloud: using" in l for l in dlog), "an image with no CUDA build: as before"
# OpenMVS 2.4 is given 2.3's own fusion and meshing values; 2.3, which has no --fusion-filter, is given none
saved_version = pipeline.mvs_version
try:
    for version, want in (((2, 4), True), ((2, 3), False), (None, False)):
        pipeline.mvs_version = lambda _v=version: _v
        runs, _, _ = dense_cmds(17000)
        assert ("--fusion-filter" in runs[0]) == want and ("--number-views-fuse" in runs[0]) == want, (version, runs)
        mesh_cmds, _ = commands_with(lambda *a, **k: {})
        mesh = by_tool(mesh_cmds, "ReconstructMesh")
        assert ("--min-point-distance" in mesh) == want, (version, mesh)
finally:
    pipeline.mvs_version = saved_version
print("depth maps' device OK")

# GPU_SIFT (on unless GPU_SIFT=0): internet photos' features as plain SIFT on the GPU; with no GPU, or if it fails, the CPU's
pipeline.GPU_SIFT = True
try:
    sift_cmds, sift_logged = gpu_run(True, 17000)
    extract = next(c for c in sift_cmds if c[1] == "feature_extractor")
    assert flags(sift_cmds, "feature_extractor", EXTRACT) == ["1"] and "--SiftExtraction.estimate_affine_shape" not in extract, sift_cmds
    sift_cmds, sift_logged = gpu_run(True, 228)
    extract = next(c for c in sift_cmds if c[1] == "feature_extractor")
    assert flags(sift_cmds, "feature_extractor", EXTRACT) == ["0"] and "--SiftExtraction.estimate_affine_shape" in extract, sift_cmds
    sift_cmds, sift_logged = gpu_run(True, 17000, fails=("feature_extractor",))
    first, second = [c for c in sift_cmds if c[1] == "feature_extractor"]
    assert "--SiftExtraction.domain_size_pooling" not in first and "--SiftExtraction.domain_size_pooling" in second and second[-1] == "0", sift_cmds
    assert any("starting again on the CPU" in l for l in sift_logged), sift_logged
    assert any(l.startswith("FALLBACK: feature_extractor: GPU SIFT failed") for l in sift_logged), sift_logged
    # photos the GPU left out without failing (out of memory: COLMAP 4.2.1 carries on): tried again on the GPU; still left
    # out, the CPU's SIFT from the start
    real_extracted = pipeline.extracted
    try:
        counts = iter([2, 8])
        pipeline.extracted = lambda db: next(counts, 8)
        sift_cmds, sift_logged = gpu_run(True, 17000)
        assert flags(sift_cmds, "feature_extractor", EXTRACT) == ["1", "1"], sift_cmds
        assert any("6 photo(s) left out on the GPU: trying them again (1 of 2)" in l for l in sift_logged), sift_logged
        pipeline.extracted = lambda db: 2
        sift_cmds, sift_logged = gpu_run(True, 17000)
        assert flags(sift_cmds, "feature_extractor", EXTRACT) == ["1", "1", "1", "0"], sift_cmds
        assert any("6 of 8 photos still left out on the GPU" in l for l in sift_logged), sift_logged
    finally:
        pipeline.extracted = real_extracted
finally:
    pipeline.GPU_SIFT = False
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


# ---- GPU SIFT's placing judged, and placed again from the CPU's SIFT when it falls short
assert pipeline._plain_sift_short({"placed": 60, "matched": 100, "ok": True, "why": ""}).startswith("its model places 60 of the 100")
assert pipeline._plain_sift_short({"placed": 70, "matched": 100, "ok": True, "why": ""}) is None, "70% is enough"
assert pipeline._plain_sift_short({"placed": 90, "matched": 100, "ok": False, "why": "pairs"}) == "its model fails its own check (pairs)"
assert pipeline._plain_sift_short({"placed": 90, "matched": None, "ok": None, "why": None}) is None, "what can't be told isn't held against it"
assert pipeline._plain_sift_short({"placed": 0, "matched": 50, "ok": False, "why": "it made no model"}) == "it made no model"
B = pipeline.plain_sift_better
assert B({"placed": 90, "ok": False}, {"placed": 80, "ok": True}) and B({"placed": 50, "ok": True}, {"placed": 60, "ok": None})
assert not B({"placed": 90, "ok": True}, {"placed": 80, "ok": True}) and not B({"placed": 50, "ok": True}, {"placed": 60, "ok": False})
assert B({"placed": 21, "ok": False}, {"placed": 44, "ok": False}), "both failing: the one placing more"
assert not B({"placed": 44, "ok": False}, {"placed": 21, "ok": False})


def sift_fallback(gpu_ok: bool, interrupt_cpu: bool = False, gpu_n: int = 12, cpu_n: int = 12):
    """A job of 12 photos from the internet, GPU SIFT on, whose GPU-SIFT model's check says gpu_ok and the CPU's passes;
    stopped at levelling. Returns the commands, the log, mapper.json, fallbacks.json and what's set aside."""
    with tempfile.TemporaryDirectory() as td:
        tmp, cmds, logged = pathlib.Path(td), [], []
        images, work = tmp / "images", tmp / "work"
        images.mkdir()
        for i in range(12):
            (images / f"{i}.jpg").write_bytes(b"x")
        state = {"stop_cpu": interrupt_cpu}

        def fake_run(cmd, cwd, log, watch=None):
            cmds.append(cmd)
            if cmd[1] == "feature_extractor":
                (work / "database.db").write_bytes(b"gpu" if cmd[-1] == "1" else b"cpu")
            if cmd[1] == "mapper":
                if state["stop_cpu"] and (work / "database.db").read_bytes() == b"cpu":
                    state["stop_cpu"] = False
                    raise Cancelled()
                model(work / "sparse", "0", gpu_n if (work / "database.db").read_bytes() == b"gpu" else cpu_n)
            if cmd[1] == "model_orientation_aligner":
                raise Stop()

        def summary(model_dir, registered, db, log):
            gpu = db.read_bytes() == b"gpu"
            return {"placed": registered, "matched": 12, "ok": gpu_ok if gpu else True, "why": "pairs" if gpu and not gpu_ok else None}
        saved = (pipeline._run, pipeline.COLMAP, pipeline.colmap_has_cuda, pipeline.gpu_free_mb, occlusion.available,
                 pipeline.placing_summary, pipeline.GPU_SIFT)
        pipeline._run, pipeline.COLMAP, pipeline.colmap_has_cuda, pipeline.gpu_free_mb = fake_run, "colmap", (lambda: True), (lambda: 17000)
        occlusion.available, pipeline.placing_summary, pipeline.GPU_SIFT = (lambda: False), summary, True
        try:
            for _ in range(2):      # an interrupted job is run again on the same work/
                try:
                    pipeline.reconstruct_colmap_openmvs(images, work, tmp / "out", logged.append, True, internet=True)
                except (Stop, Cancelled) as e:
                    if isinstance(e, Stop):
                        break
        finally:
            (pipeline._run, pipeline.COLMAP, pipeline.colmap_has_cuda, pipeline.gpu_free_mb, occlusion.available,
             pipeline.placing_summary, pipeline.GPU_SIFT) = saved
        read = lambda f: json.loads(f.read_text()) if f.exists() else None  # noqa: E731
        sift_fallback.db = (work / "database.db").read_bytes()
        return (cmds, logged, read(work / pipeline.MAPPER_FILE), read(work / pipeline.FALLBACKS_FILE),
                sorted(p.name for p in (work / pipeline.PLAIN_SIFT_ASIDE).iterdir()) if (work / pipeline.PLAIN_SIFT_ASIDE).exists() else [])


cmds, logged, how, fell, aside = sift_fallback(gpu_ok=True)
assert flags(cmds, "feature_extractor", EXTRACT) == ["1"] and not fell and not aside and "plain_sift_rejected" not in how, (cmds, fell)
assert any(l.startswith("GPU SIFT: placed 12 of 12 with matches, check passed") for l in logged), logged
for interrupted in (False, True):
    cmds, logged, how, fell, aside = sift_fallback(gpu_ok=False, interrupt_cpu=interrupted)
    extracts = [c for c in cmds if c[1] == "feature_extractor"]
    assert [c[-1] for c in extracts] == ["1", "0"] and "--SiftExtraction.estimate_affine_shape" in extracts[1], extracts
    assert "database.db" in aside and "sparse" in aside and pipeline.EXTRACT_FILE in aside, aside
    r = how["plain_sift_rejected"]
    assert r["why"] == "its model fails its own check (pairs)" and r["gpu"]["ok"] is False and r["cpu"]["ok"] is True and r["better"], r
    assert len(fell) == 1 and fell[0]["step"] == "plain_sift" and fell[0]["better"] and fell[0]["cpu"]["placed"] == 12, fell
    said = [l for l in logged if l.startswith("FALLBACK: plain_sift:")]
    assert len(said) == 1 and "the CPU's did better" in said[0] and "check failed" in said[0], logged
# the CPU's placing no better (fewer photos, as in 29 of 84 fallbacks to Nov 17): the GPU's is put back and kept
cmds, logged, how, fell, aside = sift_fallback(gpu_ok=True, gpu_n=8, cpu_n=7)
r = how["plain_sift_rejected"]
assert r["why"].startswith("its model places 8 of the 12") and r["better"] is False and r["kept"] == "gpu", r
assert sift_fallback.db == b"gpu" and fell[0]["kept"] == "gpu", "the GPU's database and model are the ones meshed"
assert any("the CPU's did no better, so the GPU's is kept" in l for l in logged), logged
print("GPU SIFT's fallback to the CPU's OK")


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
def run_until(tmp, stop_at=None, faces=None, placed=8):
    """reconstruct_colmap_openmvs() on the same work/, raising Stop at a tool as a restart would. Returns the tools it ran.

    Args:
        faces: the faces ReconstructMesh's mesh says it has; without, a file with no header.
        placed: how many photos the mapper places.
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
            model(work / "sparse", "0", placed)
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

# a mesh from VGGT's placements says so, and one from the mapper's doesn't
for placed, vggt in ((2, True), (8, False)):
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)

        def place(colmap, db, images, out, work, log, run):
            model(out.parent, out.name, 7)
            work.mkdir(parents=True, exist_ok=True)
            (work / "placed.json").write_text(json.dumps({"by": "vggt", "photos": 7}))
            return 7
        saved = pipeline.vggt_place.unavailable, pipeline.vggt_place.place
        pipeline.vggt_place.unavailable, pipeline.vggt_place.place = (lambda: None), place
        try:
            assert run_until(tmp, faces=50_000, placed=placed)[-1] == "TextureMesh"
        finally:
            restore(saved)
        assert (tmp / "out" / "placement.json").exists() is vggt, (placed, vggt)
print("placement record OK")

# --- every model the mapper made is recorded beside the mesh, largest first, with the photos each placed
def images_bin(path: pathlib.Path, names: list[str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with open(path / "images.bin", "wb") as f:
        f.write(struct.pack("<Q", len(names)))
        for i, n in enumerate(names):
            f.write(struct.pack("<i", i + 1) + struct.pack("<4d", 1, 0, 0, 0) + struct.pack("<3d", 0, 0, 0) + struct.pack("<i", 1))
            f.write(n.encode() + b"\0" + struct.pack("<Q", 0))


def colmap_db(path: pathlib.Path, names: list[str], pairs: dict[tuple[str, str], tuple[int, int]]) -> None:
    """A COLMAP database with just what best_ties() reads: images, and each pair's two-view geometry (its inliers and
    configuration), its id made as COLMAP makes it, from the lower image id."""
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE images (image_id INTEGER PRIMARY KEY, name TEXT, camera_id INTEGER)")
    con.execute("CREATE TABLE two_view_geometries (pair_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB, config INTEGER)")
    ids = {n: i + 1 for i, n in enumerate(names)}
    con.executemany("INSERT INTO images VALUES (?, ?, 1)", [(i, n) for n, i in ids.items()])
    for (a, b), (inliers, config) in pairs.items():
        lo, hi = sorted((ids[a], ids[b]))
        con.execute("INSERT INTO two_view_geometries VALUES (?, ?, 2, ?, ?)", (lo * pipeline.PAIR_BASE + hi, inliers, b"x" * 8, config))
    con.commit()
    con.close()


with tempfile.TemporaryDirectory() as d:
    sparse = pathlib.Path(d) / "sparse"
    images_bin(sparse / "0", ["a1.jpg", "a2.jpg", "b1.jpg", "b2.jpg", "b3.jpg"])
    images_bin(sparse / "1", ["c1.jpg", "c2.jpg", "c3.jpg", "c4.jpg", "c5.jpg", "c6.jpg"])
    images_bin(sparse / "2", ["d1.jpg", "d2.jpg"])      # too few to count
    (sparse / "3").mkdir()                               # empty
    got = pipeline.write_models(sparse, pathlib.Path(d) / "out")      # (the work folder, in a run)
    assert [(m["name"], len(m["photos"])) for m in got["models"]] == [("1", 6), ("0", 5)] and "left" not in got, got
    assert json.loads((pathlib.Path(d) / "out" / pipeline.MODELS_FILE).read_text())["models"][1]["photos"][:2] == ["a1.jpg", "a2.jpg"]
    assert pipeline.load_models(pathlib.Path(d) / "out") == got and pipeline.load_models(pathlib.Path(d)) is None

    # with the database and the model the mesh is made from: each photo it doesn't place, with the placed photo it shares
    # most verified matches with, in either order of their ids; an overlay's matches (a watermark) aren't the scene's
    db = pathlib.Path(d) / "database.db"
    names = ["c1.jpg", "a1.jpg", "c2.jpg", "c3.jpg", "c4.jpg", "c5.jpg", "c6.jpg", "a2.jpg", "b1.jpg", "b2.jpg", "b3.jpg", "d1.jpg",
             "d2.jpg", "e1.jpg"]
    colmap_db(db, names, {("a1.jpg", "c1.jpg"): (40, 2), ("c2.jpg", "a1.jpg"): (17, 3), ("a2.jpg", "c3.jpg"): (90, 7),
                          ("a2.jpg", "c4.jpg"): (12, 2), ("a2.jpg", "c5.jpg"): (0, 1), ("a1.jpg", "d1.jpg"): (30, 2),
                          ("a1.jpg", "a2.jpg"): (300, 2), ("e1.jpg", "d1.jpg"): (50, 2)})
    got = pipeline.write_models(sparse, pathlib.Path(d) / "out", db, sparse / "1")
    assert got["main"] == "1", got
    left = {n: (v["with"], v["inliers"]) for n, v in got["left"].items()}
    assert left == {"a1.jpg": ("c1.jpg", 40), "a2.jpg": ("c4.jpg", 12), "b1.jpg": (None, 0), "b2.jpg": (None, 0), "b3.jpg": (None, 0),
                    "d1.jpg": (None, 0), "d2.jpg": (None, 0), "e1.jpg": (None, 0)}, left
    assert json.loads((pathlib.Path(d) / "out" / pipeline.MODELS_FILE).read_text()) == got
    # a main model smaller than those worth recording is recorded all the same, as the one the mesh is made from
    assert [m["name"] for m in pipeline.write_models(sparse, pathlib.Path(d) / "out", db, sparse / "2")["models"]] == ["1", "0", "2"]
    # an unreadable database only costs the ties
    (pathlib.Path(d) / "bad.db").write_text("not a database")
    got = pipeline.write_models(sparse, pathlib.Path(d) / "out", pathlib.Path(d) / "bad.db", sparse / "1")
    assert "left" not in got and len(got["models"]) == 2, got
print("models recorded OK")


# ---- COLMAP 4's global mapper places the cameras, its untied cameras are taken out, and today's mapper is the fallback
def model_bin(path: pathlib.Path, seen: dict[str, int]) -> None:
    """A 3.9.1 binary model whose photos see that many of its points each (orientation.read_cameras()'s "seen")."""
    path.mkdir(parents=True, exist_ok=True)
    with open(path / "images.bin", "wb") as f:
        f.write(struct.pack("<Q", len(seen)))
        for i, (name, k) in enumerate(seen.items()):
            f.write(struct.pack("<i4d3di", i + 1, 1, 0, 0, 0, 0, 0, 0, 1) + name.encode() + b"\0" + struct.pack("<Q", k + 2))
            f.write(b"".join(struct.pack("<ddq", 1.0, 2.0, j) for j in range(k)) + struct.pack("<ddq", 0, 0, -1) * 2)


def global_run(tmp: pathlib.Path, models: dict | None = None, fails: str | None = None, mapper: str = "global",
               colmap4: str | None = "colmap4", n: int = 10, interrupt: str | None = None, gpu: bool = False,
               solves: tuple[str, ...] = (), gpu_fails: bool = False):
    """reconstruct_colmap_openmvs() up to levelling, with the global mapper making `models` ({name: {photo: points
    seen}}) and the incremental one, if it runs, a model of 7. fails: the step that fails ("colmap4 global_mapper",
    "colmap4 model_converter", "image_deleter"); interrupt: the one a restart stops at. gpu: a CUDA COLMAP 4 and a GPU
    with room, for the global mapper alone; solves: its BENCH_ lines; gpu_fails: it fails when asked to use the GPU.
    Returns the commands, what was logged, the model levelling was given, and mapper.json."""
    images, work, cmds, logged = tmp / "images", tmp / "work", [], []
    images.mkdir(exist_ok=True)
    for i in range(n):
        (images / f"{i}.jpg").write_bytes(b"x")
    models = models if models is not None else {"0": {f"{i}.jpg": (300 if i < 7 else 3) for i in range(9)}, "1": {"9.jpg": 40}}

    def fake_run(cmd, cwd, log, watch=None):
        what = f"{cmd[0]} {cmd[1]}" if cmd[0] == "colmap4" else cmd[1]
        cmds.append(cmd)
        if what == "feature_extractor":
            (work / "database.db").write_bytes(b"3.9.1's")
        if what == fails:
            raise RuntimeError(f"{what} exited with 1")
        if what == interrupt:
            raise Stop("interrupted")
        if what == "colmap4 global_mapper":
            if gpu_fails and cmd[cmd.index("--GlobalMapper.gp_use_gpu") + 1] == "1":
                raise RuntimeError("colmap4 exited with -6 (CUDA out of memory)")
            if watch:
                for line in ("=== Reconstructing component 1 / 2 with 9 images ===", "=== Running global positioning ===",
                             *solves):
                    watch(line)
            for name in models:
                d = pathlib.Path(cmd[cmd.index("--output_path") + 1]) / name
                d.mkdir(parents=True)
                (d / "images.bin").write_bytes(b"COLMAP 4's")
        if what == "model_converter" and cmd[cmd.index("--output_type") + 1] == "BIN":     # 3.9.1's, from 4's text
            out = pathlib.Path(cmd[cmd.index("--output_path") + 1])
            model_bin(out, models[out.name])
        if what == "image_deleter":
            drop = set(pathlib.Path(cmd[cmd.index("--image_names_path") + 1]).read_text().split())
            src = pathlib.Path(cmd[cmd.index("--input_path") + 1])
            kept = {c["name"]: c["seen"] for c in pipeline.orientation.read_cameras(src) if c["name"] not in drop}
            model_bin(pathlib.Path(cmd[cmd.index("--output_path") + 1]), kept)
        if what == "mapper":
            model(work / "sparse", "0", 7)
        if what == "model_orientation_aligner":
            raise Stop(cmd[cmd.index("--input_path") + 1])
    saved = pipeline._run, pipeline.COLMAP, pipeline.COLMAP4, pipeline.MAPPER, pipeline.colmap4_has_cuda, pipeline.colmap_has_cuda, pipeline.gpu_free_mb
    pipeline._run, pipeline.COLMAP, pipeline.COLMAP4, pipeline.MAPPER = fake_run, "colmap", colmap4, mapper
    pipeline.colmap4_has_cuda, pipeline.colmap_has_cuda, pipeline.gpu_free_mb = (lambda: gpu), (lambda: False), (lambda: 8000 if gpu else None)
    given = None
    try:
        with meshopts.using({"retry_below": 0, "more": 0}):
            pipeline.reconstruct_colmap_openmvs(images, work, tmp / "out", logged.append, gpu, internet=True,
                                                tracker=Tracker([("mapper", "Place cameras", 1)]))
    except Stop as e:
        given = e.args[0]
    finally:
        (pipeline._run, pipeline.COLMAP, pipeline.COLMAP4, pipeline.MAPPER, pipeline.colmap4_has_cuda, pipeline.colmap_has_cuda,
         pipeline.gpu_free_mb) = saved
    record = json.loads((work / pipeline.MAPPER_FILE).read_text()) if (work / pipeline.MAPPER_FILE).exists() else None
    return cmds, logged, given, record


def names_of(cmds):
    return [f"{c[0]} {c[1]}" if c[0] == "colmap4" else c[1] for c in cmds]


with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    cmds, logged, given, record = global_run(tmp)
    ran = names_of(cmds)
    i = ran.index("colmap4 global_mapper")
    assert ran[i:i + 7] == ["colmap4 global_mapper", "colmap4 model_converter", "model_converter", "colmap4 model_converter",
                            "model_converter", "image_deleter", "image_registrator"], ran
    assert "mapper" not in ran, "the incremental mapper doesn't run"
    g = cmds[i]
    assert g[g.index("--database_path") + 1].endswith("work/global.db") and (tmp / "work" / "database.db").exists(), g
    assert (tmp / "work" / "global.db").exists(), "on a copy of the database"
    for flag, value in (("--GlobalMapper.min_model_size", "6"), ("--GlobalMapper.ba_refine_principal_point", "1"),
                        ("--GlobalMapper.min_num_matches", "20"), ("--GlobalMapper.gp_use_gpu", "0"),   # no GPU here
                        ("--GlobalMapper.ba_ceres_use_gpu", "0"), ("--GlobalMapper.random_seed", "0")):
        assert g[g.index(flag) + 1] == value, (flag, g)
    conv4, conv3 = cmds[i + 1], cmds[i + 2]
    assert conv4[conv4.index("--output_type") + 1] == "TXT" and conv3[conv3.index("--input_path") + 1] == conv4[conv4.index("--output_path") + 1]
    assert conv3[conv3.index("--output_path") + 1].endswith("work/sparse/0"), conv3
    # the untied cameras: those of the largest model seeing fewer than 15 of its points, taken out of it, then tried again
    deleter = cmds[ran.index("image_deleter")]
    assert (tmp / "work" / "untied.txt").read_text().split() == ["7.jpg", "8.jpg"], "7 and 8 see 3 points each"
    assert deleter[deleter.index("--input_path") + 1].endswith("work/sparse/0"), deleter
    assert (tmp / "work" / "global_untied_0" / "images.bin").exists(), "the model as the global mapper left it, kept"
    reg = cmds[ran.index("image_registrator")]
    assert reg[reg.index("--input_path") + 1].endswith("work/sparse/0") and "--Mapper.abs_pose_min_num_inliers" in reg, reg
    assert sorted(c["name"] for c in pipeline.orientation.read_cameras(tmp / "work" / "sparse" / "0")) == [f"{i}.jpg" for i in range(7)]
    assert given.endswith("sparse/0"), given
    assert record["mapper"] == "global" and record["untied"] == ["7.jpg", "8.jpg"] and record["placed"] == 7, record
    assert record["models"] == 2 and record["untied_least"] == 15 and "--GlobalMapper.gp_use_gpu" in record["args"], record
    assert isinstance(record["seconds"], float), record
    assert any("took out 2 of its 9 cameras" in l for l in logged) and any("7 of 10 photos placed" in l for l in logged), logged
    # the step, when interrupted after the global mapper, carries on without running it again
with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    global_run(tmp, interrupt="colmap4 model_converter")
    again = names_of(global_run(tmp)[0])
    assert "colmap4 global_mapper" not in again and "colmap4 model_converter" in again, again
    assert global_run(tmp)[3]["mapper"] == "global"
# no cameras to take out: none are
with tempfile.TemporaryDirectory() as td:
    cmds, logged, given, record = global_run(pathlib.Path(td), models={"0": {f"{i}.jpg": 100 for i in range(10)}})
    assert "image_deleter" not in names_of(cmds) and "image_registrator" not in names_of(cmds) and record["untied"] == [], names_of(cmds)
    assert record["device"] == "cpu" and record["device_why"] == "USE_GPU=0", record
print("global mapper OK")

# on the GPU where its COLMAP 4 has CUDA and there's room, each solve's device counted; a GPU failure goes again on the CPU
GPU_SOLVES = ("BENCH_GP images=82 device=cuda solver=SPARSE_SCHUR sparse=CUDA_SPARSE",
              "BENCH_BA images=82 residuals=90000 device=cuda solver=SPARSE_SCHUR dense=CUDA sparse=CUDA_SPARSE threads=6",
              "BENCH_BA images=12 residuals=900 device=cpu solver=DENSE_SCHUR dense=EIGEN sparse=SUITE_SPARSE threads=6")
with tempfile.TemporaryDirectory() as td:
    cmds, logged, given, record = global_run(pathlib.Path(td), gpu=True, solves=GPU_SOLVES)
    g = [c for c in cmds if c[:2] == ["colmap4", "global_mapper"]]
    assert len(g) == 1 and g[0][g[0].index("--GlobalMapper.gp_use_gpu") + 1] == "1", g
    assert g[0][g[0].index("--GlobalMapper.ba_ceres_use_gpu") + 1] == "1", g
    assert record["device"] == "gpu" and record["solves"] == {"cuda": 2, "cpu": 1, "cpu_large": 0}, record
    assert record["args"][record["args"].index("--GlobalMapper.gp_use_gpu") + 1] == "1", record
    assert any("solving on the GPU" in l for l in logged) and not any("WARNING" in l for l in logged), logged
with tempfile.TemporaryDirectory() as td:
    cmds, logged, given, record = global_run(pathlib.Path(td), gpu=True, gpu_fails=True)
    g = [c[c.index("--GlobalMapper.gp_use_gpu") + 1] for c in cmds if c[:2] == ["colmap4", "global_mapper"]]
    assert g == ["1", "0"] and record["device"] == "gpu+cpu" and record["mapper"] == "global", (g, record)
    assert record["args"][record["args"].index("--GlobalMapper.gp_use_gpu") + 1] == "0", record
    assert any("GPU run failed" in l for l in logged), logged
# the GPU chosen, but a large solve on the CPU: said loudly
with tempfile.TemporaryDirectory() as td:
    cmds, logged, given, record = global_run(pathlib.Path(td), gpu=True, solves=(GPU_SOLVES[0].replace("cuda", "cpu"),))
    assert record["solves"]["cpu_large"] == 1 and any(l.startswith("WARNING") and "cuDSS" in l for l in logged), logged
print("global mapper on the GPU OK")

# today's incremental mapper, with VGGT after it, wherever the global mapper can't be had or doesn't give enough
for case, kwargs, said in (
        ("no COLMAP 4", {"colmap4": None}, "no COLMAP 4"),
        ("it fails", {"fails": "colmap4 global_mapper"}, "the global mapper failed"),
        ("its models can't be converted", {"fails": "colmap4 model_converter"}, "couldn't be converted"),
        ("too few placed", {"models": {"0": {f"{i}.jpg": 200 for i in range(4)}}}, "places only 4 of 10"),
        ("too few left tied", {"models": {"0": {f"{i}.jpg": (200 if i < 4 else 2) for i in range(9)}}}, "places only 4 of 10 photos that see 15"),
        ("the untied can't be taken out", {"fails": "image_deleter"}, "couldn't be taken out")):
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        cmds, logged, given, record = global_run(tmp, **kwargs)
        ran = names_of(cmds)
        assert ran.count("mapper") == 1 and given.endswith("sparse/0"), (case, ran, given)
        assert record["mapper"] == "incremental" and said in record["why"], (case, record)
        assert any("incremental mapper instead" in l and said in l for l in logged), (case, logged)
        sparse = sorted(p.name for p in (tmp / "work" / "sparse").iterdir())
        assert sparse == ["0"], (case, sparse)
        if case in ("too few placed", "too few left tied", "the untied can't be taken out"):     # its converted models, set aside
            assert any(p.name.startswith("global_sparse") for p in (tmp / "work").iterdir()), case
# MAPPER=incremental: exactly today's commands, with COLMAP 4 there or not
def without(td: str, cmds: list[list[str]]) -> list[list[str]]:
    """Commands with their run's temporary folder taken out, to compare two runs'."""
    return [[a.replace(td, "<tmp>") for a in c] for c in cmds]


with tempfile.TemporaryDirectory() as td:
    cmds, logged, given, record = global_run(pathlib.Path(td), mapper="incremental")
    chosen = without(td, cmds), given.replace(td, "<tmp>")
with tempfile.TemporaryDirectory() as td:
    cmds_today, _, given_today, _ = global_run(pathlib.Path(td), colmap4=None)
    today = without(td, cmds_today), given_today.replace(td, "<tmp>")
assert chosen == today, (chosen, today)
assert "colmap4" not in {c[0] for c in chosen[0]} and record["why"] == "the mapper chosen is 'incremental'", record
assert not any("incremental mapper instead" in l for l in logged), "nothing to explain when it was asked for"
print("mapper fallback OK")

# progress through the global mapper's phases, part by part; mapper.json goes beside the mesh, and into build.json
t = Tracker([("mapper", "Place cameras", 1)])
w = pipeline._global(t)
for line, want in (("=== Running rotation averaging ===", 0.05), ("=== Reconstructing component 2 / 2 with 30 images ===", 0.05),
                   ("=== Running iterative bundle adjustment ===", 0.75)):
    w(line)
    got = next(s["frac"] for s in t.snapshot()["steps"] if s["key"] == "mapper")
    assert abs(got - want) < 1e-9, (line, got)
assert "part 2 of 2" in next(s["detail"] for s in t.snapshot()["steps"] if s["key"] == "mapper")
with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    (tmp / "work").mkdir()
    (tmp / "work" / pipeline.MAPPER_FILE).write_text(json.dumps({"mapper": "global", "untied": ["7.jpg"]}))
    kept = []
    saved = pipeline.reconstruction.keep
    pipeline.reconstruction.keep = lambda *a, **k: kept.append(a)
    try:
        run_until(tmp, faces=50_000)
    finally:
        pipeline.reconstruction.keep = saved
    # run_until's mapper (the incremental one: no COLMAP 4 here) rewrote it, and it reached the mesh
    beside = json.loads((tmp / "out" / pipeline.MAPPER_FILE).read_text())
    assert beside["mapper"] == "incremental" and kept[0][-1] == beside, (beside, kept)
print("mapper record OK")

# --- texture from every second photo, where OpenMVS can be told which (2.4 on) and the site says so ---------------------
with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    (td / "images").mkdir()
    for i in range(25):
        (td / "images" / f"yt_ab12cd34_{i:03d}.jpg").write_bytes(b"x")
        (td / "images" / f"yt_ab12cd34_{i:03d}.jpg.mask.png").write_bytes(b"x")
    for n in ("mp_1.jpg", "ddg_2.jpg", "vid_99ff00aa_000.jpg", "vid_99ff00aa_001.jpg", "vid_99ff00aa_002.jpg"):
        (td / "images" / n).write_bytes(b"x")       # stills, all kept; another video, thinned on its own
    real = pipeline._mvs24
    said = []
    try:
        pipeline._mvs24 = lambda args: list(args)       # OpenMVS 2.4
        assert pipeline.texture_views(td, td / "images", said.append) == ["--views-file", "texture-views.txt"]
        kept = (td / "texture-views.txt").read_text().split()
        assert kept == sorted(["ddg_2.jpg", "mp_1.jpg", "vid_99ff00aa_000.jpg", "vid_99ff00aa_002.jpg"]
                              + [f"yt_ab12cd34_{i:03d}.jpg" for i in range(0, 25, 2)]), kept
        assert "from 17 of 30 photos (every still, and 15 of 28 video frames" in said[0], said
        meshopts.set_site({"texture_every": 1})         # the admin page's box unticked: every photo, and jobs elsewhere are told
        assert pipeline.texture_views(td, td / "images") == [] and meshopts.given() == {"texture_every": 1}
        with meshopts.using({"more": 2}):
            assert meshopts.given() == {"texture_every": 1, "more": 2} and meshopts.mapper()["texture_every"] == 1
        meshopts.set_site({"texture_every": 2})         # the default again: nothing to tell
        assert meshopts.given() is None and meshopts.mapper()["texture_every"] == 2
        for f in [f for f in (td / "images").glob("yt_*") if not f.name.endswith(".mask.png")][2:]:
            f.unlink()
        assert pipeline.texture_views(td, td / "images") == [], "too few photos to leave any out"
        for i in range(12):
            (td / "images" / f"still_{i}.jpg").write_bytes(b"x")
        assert pipeline.texture_views(td, td / "images") != [], "enough photos, a video thinned"
        for f in (td / "images").glob("*_*_*.jpg"):
            f.unlink()
        assert pipeline.texture_views(td, td / "images") == [], "stills only: every one used"
        pipeline._mvs24 = lambda args: []               # OpenMVS 2.3 has no --views-file
        assert pipeline.texture_views(td, td / "images") == []
    finally:
        pipeline._mvs24 = real
        meshopts.set_site({})
print("texture views OK")

# --- a model OpenMVS can't import (a camera it can't read): said, tidied to what it can, and tried again ----------------
with tempfile.TemporaryDirectory() as td:
    sparse = pathlib.Path(td) / "dense" / "sparse"
    sparse.mkdir(parents=True)
    for n in ("cameras", "images", "points3D"):
        (sparse / f"{n}.bin").write_bytes(b"x")
    TXT = {"cameras.txt": "# c\n1 PINHOLE 100 80 90 90 50 40\n2 SIMPLE_RADIAL 100 80 90 50 40 0.01\n3 PINHOLE 100 80 90 90 50 40\n",
           "images.txt": "# i\n1 1 0 0 0 0 0 0 1 a.jpg\n10 10 0\n2 1 0 0 0 0 0 0 2 b.jpg\n11 11 1\n3 1 0 0 0 0 0 0 1 c.jpg\n12 12 2\n",
           "points3D.txt": "# p\n0 1 2 3 9 9 9 0.5 1 0 2 0\n1 1 2 3 9 9 9 0.5 1 1 2 1 3 0\n2 1 2 3 9 9 9 0.5 2 2 3 1\n"}
    seen = []

    def fake_run(cmd, cwd, log, watch=None):
        seen.append(cmd)
        out = pathlib.Path(cmd[cmd.index("--output_path") + 1])
        for name, text in TXT.items():
            (out / name).write_text(text)
    real, pipeline._run = pipeline._run, fake_run
    said = []
    try:
        assert pipeline.openmvs_ready_model(sparse, said.append) is True
    finally:
        pipeline._run = real
    assert seen and seen[0][1] == "model_converter", seen
    assert any("1 with a camera OpenMVS can't take: b.jpg" in l for l in said), said
    assert sorted(p.name for p in (sparse / "binary").iterdir()) == ["cameras.bin", "images.bin", "points3D.bin"] and not list(sparse.glob("*.bin"))
    cams = (sparse / "cameras.txt").read_text().split("\n")
    assert [c.split()[0] for c in cams if c] == ["1", "3"], cams
    imgs = [l for l in (sparse / "images.txt").read_text().splitlines() if l]
    assert [l.split()[9] for l in imgs[::2]] == ["a.jpg", "c.jpg"], imgs
    pts = [l.split() for l in (sparse / "points3D.txt").read_text().splitlines() if l]
    assert [p[0] for p in pts] == ["1"] and pts[0][8:] == ["1", "1", "3", "0"], pts      # b's observations gone; points seen once dropped
    # a model with nothing OpenMVS can't take: left as it is
    TXT["cameras.txt"] = "1 PINHOLE 100 80 90 90 50 40\n2 PINHOLE 100 80 90 90 50 40\n3 PINHOLE 100 80 90 90 50 40\n"
    pipeline._run = fake_run
    try:
        assert pipeline.openmvs_ready_model(sparse, said.append) is False
    finally:
        pipeline._run = real
print("a model OpenMVS can't import, tidied OK")


# --- only consistent cameras go on: a global placing that fails its check placed again by the incremental mapper (the
# one with more consistent cameras kept), then the cameras outside the largest agreeing group dropped
def consistent(first: dict, again: dict | None, mapper: str = "global"):
    with tempfile.TemporaryDirectory() as td:
        work = pathlib.Path(td)
        sparse = work / "sparse"
        model(sparse, "0", first["placed"])
        (sparse / "0" / "who").write_text("global")
        (work / pipeline.MAPPER_FILE).write_text(json.dumps({"mapper": mapper}))
        calls, logged = [], []

        def judge(m, db):
            j = first if (m / "who").read_text() == "global" else again
            return {"why": "model check: ...", "share": None, "failed": [], **j}

        def incremental(db, images, sp, w, log, tracker, n, least, internet, why):
            calls.append(("incremental", why))
            model(sp, "0", again["placed"])
            (sp / "0" / "who").write_text("incremental")
            return {"mapper": "incremental", "why": why}

        def fake_run(cmd, cwd, log, watch=None):
            calls.append(tuple(cmd[:2]))
            out = pathlib.Path(cmd[cmd.index("--output_path") + 1])
            (out / "who").write_text("dropped")
        saved = pipeline._judge_placing, pipeline._incremental_mapper, pipeline._run
        pipeline._judge_placing, pipeline._incremental_mapper, pipeline._run = judge, incremental, fake_run
        try:
            m, r = pipeline._keep_consistent(work / "db", work / "images", sparse, work, logged.append, Tracker([]), sparse / "0",
                                             first["placed"], first["placed"], 6, True)
        finally:
            pipeline._judge_placing, pipeline._incremental_mapper, pipeline._run = saved
        how = json.loads((work / pipeline.MAPPER_FILE).read_text())
        fell = json.loads((work / pipeline.FALLBACKS_FILE).read_text()) if (work / pipeline.FALLBACKS_FILE).exists() else []
        return r, how, fell, calls, logged, (work / "before_consistent_0" / "who").read_text() if (work / "before_consistent_0").exists() else None


SPLIT = {"ok": False, "failed": ["split"], "placed": 179, "keep": 82, "out": [f"x{i}.jpg" for i in range(97)]}
GOOD = {"ok": True, "failed": [], "placed": 160, "keep": 158, "out": ["a.jpg", "b.jpg"]}
r, how, fell, calls, logged, before = consistent(SPLIT, GOOD)
assert calls[0][0] == "incremental" and "failed its check (split)" in calls[0][1], calls
assert how["mapper"] == "incremental" and how["replaced_global"] == {"mapper": "global"} and how["consistent"]["kept"] == "incremental", how
assert before is None and r == 160 and "dropped" not in how["consistent"], ("an incremental placing is left whole", before, r, how)
assert fell[0]["step"] == "global_mapper_check" and fell[0]["kept"] == "incremental", fell
WORSE = {"ok": True, "failed": [], "placed": 40, "keep": 40, "out": []}
r, how, fell, calls, logged, before = consistent(SPLIT, WORSE)
assert how["mapper"] == "global" and how["consistent"]["kept"] == "global" and before == "global" and r == 179 - 97, (how, r)
assert any("the global mapper's is kept" in l for l in logged), logged
r, how, fell, calls, logged, before = consistent({**GOOD, "out": []}, None)
assert not calls and r == 160 and "dropped" not in how["consistent"], "a sound placing: nothing done"
r, how, fell, calls, logged, before = consistent(SPLIT, None, mapper="incremental")
assert not calls and r == 179 and before is None and "dropped" not in how["consistent"], "the incremental mapper's own: left whole"
print("only consistent cameras kept OK")

# --- which mapper: MAPPER where the image sets it, else the site's choice (the admin page's mesh.mapper), sent with jobs
saved_mapper = pipeline.MAPPER
try:
    pipeline.MAPPER = ""
    meshopts.set_site({})
    assert pipeline.mapper_method() == "incremental" and meshopts.given() is None, "the default"
    meshopts.set_site({"method": "incremental"})
    assert pipeline.mapper_method() == "incremental" and meshopts.given() == {"method": "incremental"}, "sent even as the default"
    assert pipeline._global_mapper(pathlib.Path("db"), pathlib.Path("i"), pathlib.Path("s"), pathlib.Path("w"), print, Tracker([]), 10, 6,
                                   True) == (None, "the mapper chosen is 'incremental'")
    assert "method" not in meshopts.placing(), "not compared with what meshes were made from: their mapper.json says"
    meshopts.set_site({"method": "global"})
    assert pipeline.mapper_method() == "global"
    pipeline.MAPPER = "incremental"
    assert pipeline.mapper_method() == "incremental", "the image's own MAPPER wins"
finally:
    pipeline.MAPPER = saved_mapper
    meshopts.set_site({})
print("the site's choice of mapper OK")
print("ALL PIPELINE TESTS PASSED")
