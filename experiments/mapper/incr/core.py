"""The incremental pipeline's COLMAP and OpenMVS steps on a state folder (state.py), through pycolmap 4.2.1.

    core.py features <state> [--feat sift|siftgpu|aliked] [--threads N]
    core.py match <state> [--cpu] [--threads N]
    core.py place <state> [--init auto|vggt|global|incremental] [--vggt cameras.json] [--ba auto|cpu|gpu|caspar]
                          [--seed N] [--threads N] [--resolve] [--no-scenes]
    core.py assist <state> <cameras.json> [--ba ..] [--threads N]
    core.py undistort|import|dense|mesh|texture <state> [--threads N] [--cpu]

boulder.py runs these, each as one timed step, in the tools build-env.sh makes (pycolmap built against the benchmark's
patched COLMAP, so bundle adjustment can run on the GPU and each solve logs a BENCH_BA line with its device). One
code path builds a boulder and adds to it: place() starts a model if the state has none (from VGGT's cameras, the
global mapper, or the classic incremental mapper), then registers whatever photos are not yet placed against it.
With a model there already, its cameras are held fixed, so the frame the first build set stays put.

Every step prints INCR lines (what it decided) and BENCH_MARK lines (when each part started), and writes
<state>/log/current/<step>.report.json.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
try:
    import pycolmap
except ImportError:     # read_ply alone, outside the tools
    pycolmap = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import state as st  # noqa: E402

MAX_IMAGE_ID = 2147483647
# The app's settings for photos from the internet (backend/pipeline.py)
MIN_MATCHES, MIN_MODEL, MIN_INLIERS = 20, 6, 12
REST = (15, 0.15)       # the app's "rest" step: a photo may be placed on this few inliers, and this share of its matches
# Rounds of (largest angle error in degrees, largest reprojection error in pixels) for points from cameras that are
# only roughly right, as backend/vggt_place.py's ROUNDS
ROUNDS = ((8, 150), (3, 20), (2, 4))
# How often everything is refined together while photos are registered against a model no camera of which is fixed:
# when the placed photos or the points have grown by this factor since the last time. COLMAP's own is 1.1: it grows
# a model from two photos, where every early error is built on. Here the model starts from tens of cameras solved
# together, so less often does (README: what was measured).
GLOBAL_RATIO = float(os.environ.get("INCR_GLOBAL_RATIO", "1.4"))
GLOBAL_REFINEMENTS = int(os.environ.get("INCR_GLOBAL_REFINEMENTS", "3"))
# From this many images a refinement of everything goes to Caspar, with --ba auto or caspar (ba_options)
CASPAR_MIN_IMAGES = int(os.environ.get("INCR_CASPAR_MIN_IMAGES", "20"))
GPU_MIN_IMAGES = 50         # an add's own refinement goes to --ba's solver from this many added photos
ASSIST_MIN_POINTS = 15      # a photo placed from VGGT's camera stays if this many of its points survive refinement
ANCHORS = 12                # placed photos sent to VGGT with the unplaced ones, to tie its frame to the model's
VGGT_MIN_KEPT = 0.6         # VGGT's start is used if at least this share of its cameras pass the rotation check,
VGGT_MIN_PLACED = 0.6       # and this share of them end up with points after the rounds
SMALL = 6                   # fewer cameras than this is no start
MIN_TRI_ANGLE = float(os.environ.get("INCR_MIN_TRI_ANGLE", "1.0"))     # pipeline_options()
LENS_DRIFT = 1.3            # a refinement of everything may move a focal length this far, either way (Session)


def log(msg: str) -> None:
    print(f"INCR {msg}", flush=True)


def mark(name: str) -> None:
    print(f"BENCH_MARK {name} {time.time():.3f}", flush=True)


class Report:
    """A step's findings, written as it goes to <state>/log/current/<step>.report.json."""

    def __init__(self, root: Path, step: str):
        d = root / "log" / "current"
        d.mkdir(parents=True, exist_ok=True)
        self.path, self.data, self.t0 = d / f"{step}.report.json", {"step": step, "parts": {}}, time.time()
        self._part = None

    def part(self, name: str) -> None:
        """A part of the step starts (the one before ends)."""
        now = time.time()
        if self._part:
            self.data["parts"][self._part[0]] = round(self.data["parts"].get(self._part[0], 0) + now - self._part[1], 2)
        self._part = (name, now) if name else None
        if name:
            mark(name)

    def save(self, **more) -> dict:
        self.part("")
        self.data.update(more)
        self.data["seconds"] = round(time.time() - self.t0, 2)
        st.write_json(self.path, self.data)
        return self.data


# ---------------------------------------------------------------- the database

def db_images(db: Path) -> dict[str, int]:
    """Name -> image id of the photos that have features."""
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {n: i for i, n in con.execute("SELECT image_id, name FROM images WHERE image_id IN (SELECT image_id FROM keypoints)")}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def db_pairs(db: Path) -> dict[int, int]:
    """Pair id -> verified inliers, for every pair of photos matching was tried on (0: tried, nothing verified)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        pairs = {p: 0 for p, in con.execute("SELECT pair_id FROM matches")}
        pairs.update({p: r for p, r in con.execute("SELECT pair_id, rows FROM two_view_geometries")})
        return pairs
    finally:
        con.close()


def pair_id(a: int, b: int) -> int:
    a, b = min(a, b), max(a, b)
    return a * MAX_IMAGE_ID + b


def links(db: Path, least: int = MIN_MATCHES) -> dict[int, dict[int, int]]:
    """Image id -> {image id: verified inliers} for the pairs with at least `least`."""
    out: dict[int, dict[int, int]] = {}
    for p, rows in db_pairs(db).items():
        if rows >= least:
            a, b = divmod(p, MAX_IMAGE_ID)
            out.setdefault(a, {})[b] = rows
            out.setdefault(b, {})[a] = rows
    return out


# ---------------------------------------------------------------- features and matches

def features(root: Path, kind: str, threads: int) -> dict:
    """Features for the photos that have none yet. The app's (affine shape and domain-size pooling, which COLMAP has
    only on the CPU) unless asked otherwise; boulder.py keeps a state to the kind its first photos got."""
    rep = Report(root, "features")
    db = root / "database.db"
    have = db_images(db)
    todo = sorted(n for n in os.listdir(root / "photos") if n.lower().endswith(st.PHOTO_TYPES) and n not in have)
    log(f"features: {len(todo)} photo(s) to do, {len(have)} have theirs ({kind})")
    if todo:
        reader = pycolmap.ImageReaderOptions()
        reader.camera_model = "SIMPLE_RADIAL"
        if any((root / "masks").iterdir()):
            reader.mask_path = str(root / "masks")
        ext = pycolmap.FeatureExtractionOptions()
        ext.max_image_size, ext.num_threads = 2400, threads
        if kind == "sift":
            ext.sift.estimate_affine_shape = ext.sift.domain_size_pooling = True
            ext.use_gpu = False
        elif kind == "siftgpu":
            ext.use_gpu = True
        elif kind == "aliked":
            ext.type, ext.use_gpu = pycolmap.FeatureExtractorType.ALIKED_N16ROT, True
        else:
            raise SystemExit(f"unknown features {kind}")
        rep.part("extract")
        pycolmap.extract_features(str(db), str(root / "photos"), image_names=todo, camera_mode=pycolmap.CameraMode.PER_IMAGE,
                                  reader_options=reader, extraction_options=ext,
                                  device=pycolmap.Device.cuda if ext.use_gpu else pycolmap.Device.cpu)
    now = db_images(db)
    return rep.save(kind=kind, extracted=len(todo), missing=sorted(set(todo) - set(now)), photos=len(now),
                    device="cuda" if todo and kind != "sift" else "cpu")


def match(root: Path, gpu: bool, threads: int, kind: str = "sift") -> dict:
    """Matches and verifies the pairs of photos not tried yet: after an add, the new photos with every photo. The
    pairs are listed for COLMAP, so the old ones are not looked at again; their rows are checked to be as they were."""
    rep = Report(root, "match")
    db = root / "database.db"
    here = set(os.listdir(root / "photos"))
    ids = {n: i for n, i in db_images(db).items() if n in here}
    before = db_pairs(db)
    names = sorted(ids)
    todo = [(a, b) for i, a in enumerate(names) for b in names[i + 1:] if pair_id(ids[a], ids[b]) not in before]
    log(f"match: {len(todo)} pair(s) to try of {len(names) * (len(names) - 1) // 2}; {len(before)} tried before")
    if todo:
        pairs_file = root / "log" / "current" / "pairs.txt"
        pairs_file.write_text("".join(f"{a} {b}\n" for a, b in todo))
        m = pycolmap.FeatureMatchingOptions()
        m.guided_matching, m.use_gpu, m.num_threads = True, gpu, threads
        if kind == "aliked":
            m.type = pycolmap.FeatureMatcherType.ALIKED_LIGHTGLUE
        pairing = pycolmap.ImportedPairingOptions()
        pairing.match_list_path = str(pairs_file)
        verify = pycolmap.TwoViewGeometryOptions()
        verify.min_num_inliers = MIN_INLIERS
        rep.part("match")
        pycolmap.match_image_pairs(str(db), matching_options=m, pairing_options=pairing, verification_options=verify,
                                   device=pycolmap.Device.cuda if gpu else pycolmap.Device.cpu)
    after = db_pairs(db)
    changed = sum(1 for p, r in before.items() if after.get(p) != r)
    new = {p: r for p, r in after.items() if p not in before}
    return rep.save(pairs_before=len(before), pairs_listed=len(todo), pairs_after=len(after), old_pairs_changed=changed,
                    new_verified=sum(1 for r in new.values() if r >= MIN_MATCHES), device="cuda" if gpu and todo else "cpu")


# ---------------------------------------------------------------- models

def poses(rec: pycolmap.Reconstruction) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Name -> (world-to-camera rotation, translation) of the placed photos."""
    out = {}
    for i in rec.reg_image_ids():
        im = rec.image(i)
        T = im.cam_from_world()
        out[im.name] = (T.rotation.matrix(), np.array(T.translation))
    return out


def similarity(a: dict, b: dict, names: list[str]) -> tuple[float, np.ndarray, np.ndarray] | None:
    """The scale, rotation and shift that lay model a's cameras over model b's, fitted on the cameras named: rotation
    from the cameras' own orientations (boulder photos are often taken from along a line, which leaves a fit to
    positions alone free to spin about it), then scale and shift from where they stand; fitted again without the
    worst fifth. None with fewer than 3."""
    if len(names) < 3:
        return None
    centre = lambda m, n: -m[n][0].T @ m[n][1]      # noqa: E731

    def fit(use):
        U, _, Vt = np.linalg.svd(sum(b[n][0].T @ a[n][0] for n in use))
        Rs = U @ np.diag([1, 1, np.sign(np.linalg.det(U @ Vt))]) @ Vt
        X, Y = np.array([Rs @ centre(a, n) for n in use]), np.array([centre(b, n) for n in use])
        mx, my = X.mean(0), Y.mean(0)
        s = ((Y - my) * (X - mx)).sum() / max(((X - mx) ** 2).sum(), 1e-12)
        return float(s), Rs, my - s * mx
    s, Rs, t = fit(names)
    err = {n: float(np.linalg.norm(s * Rs @ centre(a, n) + t - centre(b, n))) for n in names}
    keep = sorted(names, key=err.get)[:max(3, int(len(names) * 0.8))]
    return fit(keep)


def transform(rec: pycolmap.Reconstruction, sim: tuple[float, np.ndarray, np.ndarray]) -> None:
    s, R, t = sim
    rec.transform(pycolmap.Sim3d(s, pycolmap.Rotation3d(R), t))


def has_pose(rec: pycolmap.Reconstruction, name: str) -> bool:
    im = rec.find_image_with_name(name)
    return im is not None and im.has_pose


def set_pose(rec: pycolmap.Reconstruction, image_id: int, R, t, focal: float | None = None) -> None:
    im = rec.image(image_id)
    frame = rec.frame(im.frame_id)
    frame.set_cam_from_world(im.camera_id, pycolmap.Rigid3d(pycolmap.Rotation3d(np.array(R, dtype=float)), np.array(t, dtype=float)))
    if focal:
        cam = rec.camera(im.camera_id)
        p = np.array(cam.params)
        p[list(cam.focal_length_idxs())] = focal
        cam.params = p


def pipeline_options(threads: int, seed: int) -> pycolmap.IncrementalPipelineOptions:
    """COLMAP's incremental mapper options with the app's settings, but for the least angle a 3D point must be seen
    across: MIN_TRI_ANGLE (1 degree, as the app's VGGT rounds have it) for COLMAP's 1.5, as boulders photographed
    from about one place lose most of their points at 1.5 (on the 33-photo boulder, 15,400 of them to under 600)
    and photos added later have nothing to register against."""
    o = pycolmap.IncrementalPipelineOptions()
    o.min_num_matches, o.min_model_size, o.ba_refine_principal_point = MIN_MATCHES, MIN_MODEL, True
    o.mapper.filter_min_tri_angle = o.triangulation.min_angle = MIN_TRI_ANGLE
    o.num_threads, o.random_seed = threads, seed
    o.mapper.num_threads, o.mapper.random_seed = threads, seed
    o.extract_colors = False
    return o


def ba_options(o: pycolmap.IncrementalPipelineOptions, images: int, ba: str, local: bool = False,
               final: bool = False) -> pycolmap.BundleAdjustmentOptions:
    """A bundle adjustment's options for a problem of so many images, on what --ba asks for.

    "auto": Caspar (COLMAP's GPU solver, float32) for every refinement of everything from CASPAR_MIN_IMAGES images up,
    but the final one (final), which is Ceres's on the CPU (SuiteSparse): on the 82-photo boulder, from the same VGGT
    start, placement took 26 s with Caspar throughout, 181 s with Ceres on the CPU and 211 s with Ceres on the GPU
    (cuDSS), all placing the same 72 photos. Caspar's cameras end 0.34 degrees (median; 3.5 at the 90th percentile)
    from Ceres's, and a final Ceres refinement does not take them back (0.34 and 2.9 after it; 0.10 from Caspar's
    own): the two solvers settle in slightly different places from the start on, at reprojection errors of 0.64 and
    0.66 px. Which is nearer the truth is for the benchmark's seed-to-seed spread to say, so the final refinement on
    Ceres is kept, as the more exact solver, at one round. "caspar", "cpu" and "gpu" (Ceres with CUDA) use the one
    throughout."""
    b = o.get_local_bundle_adjustment() if local else o.get_global_bundle_adjustment()
    b.print_summary = False
    gpu_there = pycolmap.has_cuda and pycolmap.get_num_cuda_devices() > 0
    if gpu_there and not local and images >= CASPAR_MIN_IMAGES and (ba == "caspar" or ba == "auto" and not final):
        b.backend = pycolmap.BundleAdjustmentBackend.CASPAR
    elif ba == "gpu" and gpu_there:
        b.ceres.use_gpu, b.ceres.min_num_images_gpu_solver = True, 0
    else:
        b.ceres.use_gpu = False
    return b


def open_cache(db: Path, names: set[str] | None) -> pycolmap.DatabaseCache:
    co = pycolmap.DatabaseCacheOptions()
    co.min_num_matches = MIN_MATCHES
    if names is not None:
        co.image_names = set(names)
    database = pycolmap.Database.open(str(db))
    try:
        return pycolmap.DatabaseCache.create(database, co)
    finally:
        database.close()


def stats_of(rec: pycolmap.Reconstruction) -> dict:
    return {"placed": rec.num_reg_images(), "points": rec.num_points3D(),
            "track": round(rec.compute_mean_track_length(), 2), "error_px": round(rec.compute_mean_reprojection_error(), 3)}


class Session:
    """A model being grown: COLMAP's incremental mapper on it, and a guard on the refinements of everything together.

    Where the photos were taken from about one place, the matches fix neither the lenses nor the depths, and each
    bundle adjustment with the lenses free walks them further off (on the benchmark's 33-photo boulder, whose points
    are seen across a median of 1.4 degrees: one focal length to 0.67, 0.31 and 0.12 of its start over three
    refinements, and its camera then dropped as degenerate). So a refinement that moves a lens further than
    LENS_DRIFT from where its first refinement left it, or loses cameras, is undone and done again with those lenses
    held, and they stay held: a lens is then refined only until the first refinement of everything it is part of.
    """

    def __init__(self, cache, rec: pycolmap.Reconstruction, o, ba: str):
        self.cache, self.rec, self.o, self.ba = cache, rec, o, ba
        self.lenses_held, self.anchor, self.undone, self.since, self.solves = False, {}, 0, 0, 0
        self.mapper = pycolmap.IncrementalMapper(cache)
        self.mapper.begin_reconstruction(rec)
        self.note_lenses()

    def note_lenses(self) -> None:
        """Takes the lenses of the cameras placed now as refined (unless COLMAP itself would call one degenerate), and
        forgets those of cameras no longer placed: one placed again gets a new estimate, to be refined afresh."""
        placed = {self.rec.image(i).camera_id for i in self.rec.reg_image_ids()}
        self.anchor = {c: f for c, f in self.anchor.items() if c in placed}
        m = self.o.mapper
        for cid in placed - set(self.anchor):
            cam = self.rec.camera(cid)
            if not cam.has_bogus_params(m.min_focal_length_ratio, m.max_focal_length_ratio, m.max_extra_param):
                self.anchor[cid] = cam.mean_focal_length()

    def drifted(self) -> list[int]:
        out = []
        for i in self.rec.reg_image_ids():
            cid = self.rec.image(i).camera_id
            if cid in self.anchor and not 1 / LENS_DRIFT <= self.rec.camera(cid).mean_focal_length() / self.anchor[cid] <= LENS_DRIFT:
                out.append(cid)
        return out

    def restart(self) -> None:
        """The mapper afresh on the model, so that photos it had given up on are tried again."""
        self.mapper.end_reconstruction(False)
        self.mapper = pycolmap.IncrementalMapper(self.cache)
        self.mapper.begin_reconstruction(self.rec)

    def _solve(self, mo, tri, refinements: int, principal_point: bool, final: bool = False) -> None:
        b = ba_options(self.o, self.rec.num_reg_images(), self.ba, final=final)
        b.refine_principal_point = principal_point
        mo.constant_cameras = set(self.anchor) if self.lenses_held else set()
        self.mapper.iterative_global_refinement(refinements, self.o.ba_global_max_refinement_change, mo, b, tri, True)
        self.mapper.filter_frames(mo)
        self.solves += 1

    def _undo(self, before: pycolmap.Reconstruction) -> None:
        self.mapper.end_reconstruction(False)
        self.rec = before
        self.mapper = pycolmap.IncrementalMapper(self.cache)
        self.mapper.begin_reconstruction(before)
        self.undone += 1

    def refine(self, mo, tri, refinements: int = GLOBAL_REFINEMENTS, final: bool = False) -> None:
        """Refines every camera, point and lens together, under the guard (final: on the most exact solver)."""
        before, had = copy.deepcopy(self.rec), set(self.rec.reg_image_ids())
        self._solve(mo, tri, refinements, False, final)
        lost, drift = had - set(self.rec.reg_image_ids()), self.drifted()
        if (lost or drift) and not self.lenses_held:
            log(f"a refinement of everything lost {len(lost)} camera(s) and walked {len(drift)} lens(es) off: undone, "
                f"and done again with the {len(self.anchor)} lenses refined before held")
            self._undo(before)
            self.lenses_held = True
            self._solve(mo, tri, refinements, False, final)
        self.note_lenses()
        mo.constant_cameras = set(self.anchor) if self.lenses_held else set()
        self.since = 0

    def polish(self, mo, tri) -> bool:
        """One more refinement with the principal points free too, as the app's mapper has them for photos from the
        internet (often cropped): kept only if it loses no camera, walks no lens off and keeps the points."""
        if self.lenses_held:
            return False
        before, had, points = copy.deepcopy(self.rec), set(self.rec.reg_image_ids()), self.rec.num_points3D()
        self._solve(mo, tri, 1, True)
        if had - set(self.rec.reg_image_ids()) or self.drifted() or self.rec.num_points3D() < 0.95 * points:
            log(f"refining the principal points too was undone: {len(had - set(self.rec.reg_image_ids()))} camera(s) lost, "
                f"{len(self.drifted())} lens(es) off, {self.rec.num_points3D()} of {points} points kept")
            self._undo(before)
            return False
        return True

    def close(self) -> pycolmap.Reconstruction:
        self.mapper.end_reconstruction(False)
        return self.rec


def register_loop(S: Session, mo, tri, refine_all: bool, how: str, placed: dict[str, str]) -> int:
    """Registers the photos of the session's cache that aren't placed, as COLMAP's own loop does: the best candidate
    next, by its points already in the model, then its own points triangulated and its neighbourhood refined. With
    refine_all, everything is refined together each time the model has grown by GLOBAL_RATIO, and once more when no
    photo more can be registered, before the last try. Without (cameras of the model held fixed by mo), the caller
    refines what was added. Returns how many it registered; placed gains their names."""
    o = S.o
    local = ba_options(o, o.mapper.ba_local_num_images, "cpu", local=True)
    flags = [False, True] if o.structure_less_registration_fallback else [False]
    prev_frames, prev_points, done = S.rec.num_reg_frames(), S.rec.num_points3D(), 0
    ok = prev_ok = True
    while ok or prev_ok:
        prev_ok, ok, image_id = ok, False, None
        for structure_less in flags:
            for image_id in S.mapper.find_next_images(mo, structure_less):
                ok = (S.mapper.register_next_structure_less_image(mo, image_id) if structure_less
                      else S.mapper.register_next_image(mo, image_id))
                if ok:
                    break
            if ok:
                break
        if ok:
            S.mapper.triangulate_image(tri, image_id)
            S.mapper.iterative_local_refinement(o.ba_local_max_refinements, o.ba_local_max_refinement_change, mo, local, tri, image_id)
            placed[S.rec.image(image_id).name] = how
            done, S.since = done + 1, S.since + 1
            if refine_all and (S.rec.num_reg_frames() >= GLOBAL_RATIO * prev_frames or S.rec.num_points3D() >= GLOBAL_RATIO * prev_points):
                S.refine(mo, tri)
                prev_frames, prev_points = S.rec.num_reg_frames(), S.rec.num_points3D()
        elif prev_ok and refine_all and S.since:
            S.refine(mo, tri)
            prev_frames, prev_points = S.rec.num_reg_frames(), S.rec.num_points3D()
    return done


def refine_added(mapper, rec, o, mo, tri, added: list[int], ba: str, rounds: int = 2, fixed_lenses: set[int] = frozenset()) -> None:
    """Refines the cameras just added and the points they see, with every other camera as it is (pose and lens: the
    lenses named are held even where a new photo shares one): only they and those points are in the problem, so its
    size follows the add, not the model."""
    if not added:
        return
    for _ in range(rounds):
        config = pycolmap.BundleAdjustmentConfig()
        points = set()
        for i in added:
            if not rec.image(i).has_pose:
                continue
            config.add_image(i)
            points.update(p.point3D_id for p in rec.image(i).points2D if p.has_point3D())
        for p in points:
            config.add_variable_point(p)
        for cid in fixed_lenses:
            config.set_constant_cam_intrinsics(cid)
        b = ba_options(o, len(added), ba if len(added) >= GPU_MIN_IMAGES else "cpu")
        b.refine_principal_point = False
        pycolmap.create_default_bundle_adjuster(b, config, rec).solve()
        changed = mapper.complete_and_merge_tracks(tri)
        changed += mapper.observation_manager.filter_points3D_in_images(mo.filter_max_reproj_error, mo.filter_min_tri_angle, set(added))
        if changed == 0:
            break


def start_from_cameras(cache, cams: dict, o, ba: str, rep: Report) -> pycolmap.Reconstruction | None:
    """A model from cameras that are only roughly right (VGGT's): COLMAP triangulates its verified matches from them
    and refines cameras, points and lenses together, the tolerances tightening over ROUNDS (as the app's
    vggt_place.place() does with point_triangulator and bundle_adjuster, here in one mapper session, so that the
    bundle adjustments can go to the GPU). None if the cameras fail the checks: too few pass the rotation check,
    or too few keep points."""
    dropped = set(cams.get("dropped", []))
    by_name = {im.name: i for i, im in cache.images.items()}
    kept = [c for c in cams["chosen"] if c["name"] not in dropped and c["name"] in by_name and c["name"] in cams["placed"]]
    if len(kept) < SMALL or len(kept) < VGGT_MIN_KEPT * len(cams["chosen"]):
        log(f"VGGT start refused: {len(kept)} of {len(cams['chosen'])} cameras pass the rotation check")
        return None
    rec = pycolmap.Reconstruction()
    rec.load(cache)
    for c in kept:
        R, t, focal = cams["placed"][c["name"]]
        i = by_name[c["name"]]
        set_pose(rec, i, R, t, focal)
        rec.register_frame(rec.image(i).frame_id)
    mapper = pycolmap.IncrementalMapper(cache)
    mapper.begin_reconstruction(rec)
    mo, tri = o.get_mapper(), o.get_triangulation()
    tri.ignore_two_view_tracks, tri.min_angle, mo.filter_min_tri_angle = False, 1.0, 1.0
    for k, (angle, err) in enumerate(ROUNDS):
        rep.part(f"triangulate{k}")
        tri.create_max_angle_error = tri.continue_max_angle_error = tri.re_max_angle_error = angle
        tri.merge_max_reproj_error = tri.complete_max_reproj_error = mo.filter_max_reproj_error = err
        for i in sorted(rec.reg_image_ids()):
            mapper.triangulate_image(tri, i)
        rep.part(f"adjust{k}")
        b = ba_options(o, rec.num_reg_images(), ba)
        b.refine_principal_point = False
        mapper.iterative_global_refinement(1, o.ba_global_max_refinement_change, mo, b, tri, True)
        log(f"round {k + 1} (up to {angle} deg, {err} px): {rec.num_points3D()} points, {rec.compute_mean_reprojection_error():.2f} px")
    with_points = [i for i in rec.reg_image_ids() if rec.image(i).num_points3D >= ASSIST_MIN_POINTS]
    if len(with_points) < max(SMALL, VGGT_MIN_PLACED * len(kept)):
        log(f"VGGT start refused: only {len(with_points)} of {len(kept)} cameras kept {ASSIST_MIN_POINTS}+ points")
        mapper.end_reconstruction(True)
        return None
    for i in sorted(set(rec.reg_image_ids()) - set(with_points)):   # a camera its points didn't confirm is tried again by registration
        mapper.observation_manager.deregister_frame(rec.image(i).frame_id)
    mapper.end_reconstruction(False)
    return rec


def start_global(db: Path, photos: Path, names: set[str], o, ba: str, work: Path) -> tuple[pycolmap.Reconstruction | None, list]:
    """COLMAP's global mapper on the photos named: every camera's orientation from the pairs at once, then positions
    and points, then bundle adjustment. Returns (the largest model, the others)."""
    g = pycolmap.GlobalPipelineOptions()
    g.min_num_matches, g.min_model_size = MIN_MATCHES, MIN_MODEL
    g.num_threads = g.mapper.num_threads = o.num_threads
    g.random_seed = g.mapper.random_seed = o.random_seed
    g.image_names = sorted(names)
    g.mapper.bundle_adjustment.refine_principal_point = True
    gpu = ba != "cpu" and pycolmap.has_cuda and pycolmap.get_num_cuda_devices() > 0
    g.mapper.global_positioning.use_gpu = g.mapper.bundle_adjustment.ceres.use_gpu = gpu
    if ba == "gpu":
        g.mapper.global_positioning.min_num_images_gpu_solver = g.mapper.bundle_adjustment.ceres.min_num_images_gpu_solver = 0
    if ba == "caspar":
        g.mapper.bundle_adjustment.backend = pycolmap.BundleAdjustmentBackend.CASPAR
    out = work / "global"
    out.mkdir(parents=True, exist_ok=True)
    models = sorted(pycolmap.global_mapping(str(db), str(photos), str(out), g).values(), key=lambda m: -m.num_reg_images())
    return (models[0], models[1:]) if models and models[0].num_reg_images() >= SMALL else (None, [])


def start_incremental(db: Path, photos: Path, names: set[str], o, ba: str, work: Path) -> tuple[pycolmap.Reconstruction | None, list]:
    """COLMAP's own incremental mapper with the app's settings, from two photos up. Returns (the largest model, the
    others)."""
    oo = pipeline_options(o.num_threads, o.random_seed)
    oo.image_names = sorted(names)
    oo.ba_use_gpu = ba in ("gpu", "auto") and pycolmap.has_cuda and pycolmap.get_num_cuda_devices() > 0
    if ba == "caspar":
        oo.ba_global_backend = pycolmap.BundleAdjustmentBackend.CASPAR
    out = work / "incremental"
    out.mkdir(parents=True, exist_ok=True)
    models = sorted(pycolmap.incremental_mapping(str(db), str(photos), str(out), oo).values(), key=lambda m: -m.num_reg_images())
    return (models[0], models[1:]) if models and models[0].num_reg_images() >= SMALL else (None, [])


def linked_to(names: list[str], placed: set[str], ids: dict[str, int], link: dict) -> list[str]:
    """Those of the photos named that share verified matches with a placed one."""
    placed_ids = {ids[n] for n in placed if n in ids}
    return [n for n in names if n in ids and placed_ids & set(link.get(ids[n], {}))]


def joined(names: list[str], placed: set[str], ids: dict[str, int], link: dict) -> list[str]:
    """Those of the photos named that verified matches join to a placed one, directly or through other photos."""
    by_id = {i: n for n, i in ids.items()}
    seen = {ids[n] for n in placed if n in ids}
    todo = list(seen)
    while todo:
        i = todo.pop()
        for j in link.get(i, {}):
            if j in by_id and j not in seen:
                seen.add(j)
                todo.append(j)
    return [n for n in names if n in ids and ids[n] in seen]


def anchors_for(names: list[str], placed: set[str], ids: dict[str, int], link: dict, limit: int = ANCHORS) -> list[str]:
    """The placed photos that tie the photos named to the model best: by their verified matches with them, each one
    taken counting less the matches of photos that have an anchor already; made up to `limit` with the placed photos
    most linked to the rest of the model."""
    by_id = {i: n for n, i in ids.items()}
    want = {ids[n]: 1.0 for n in names if n in ids}
    placed_ids = {ids[n] for n in placed if n in ids}
    chosen: list[int] = []
    while len(chosen) < limit:
        score = {p: sum(w * link.get(p, {}).get(u, 0) for u, w in want.items()) for p in placed_ids - set(chosen)}
        if not score or max(score.values()) <= 0:
            break
        best = max(score, key=score.get)
        chosen.append(best)
        for u in want:
            if link.get(best, {}).get(u):
                want[u] *= 0.5
    # VGGT's frame is laid over the model's through these, so the rest are made up of the photos most linked overall
    rest = sorted(placed_ids - set(chosen), key=lambda p: -sum(link.get(p, {}).values()))
    chosen += rest[:max(0, limit - len(chosen))]
    return [by_id[i] for i in chosen]


def place(root: Path, init: str, vggt: Path | None, ba: str, seed: int, threads: int, resolve: bool, scenes: bool) -> dict:
    """Places the photos that aren't: starts a model if the state has none (or a full re-solve is asked for), then
    registers the rest against it; with a model, registers them against it with its cameras fixed. Writes model.new
    and what it did, including whether a full re-solve or a new mesh is due (state.py's gates), and whether VGGT
    should be asked about photos that are linked to the model yet could not be registered."""
    s, rep = st.State(root), Report(root, "place")
    db, photos, work = root / "database.db", root / "photos", root / "log" / "current" / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    pycolmap.set_random_seed(seed)
    here = set(os.listdir(photos))
    ids, link = {n: i for n, i in db_images(db).items() if n in here}, links(db)
    o = pipeline_options(threads, seed)
    old = pycolmap.Reconstruction(str(root / "model")) if s.has_model() and not resolve else None
    placed: dict[str, str] = {}
    others: list = []
    points_before = old.num_points3D() if old else 0
    if old is None:
        names = set(ids)
        log(f"{'full re-solve' if resolve else 'first build'}: {len(names)} photos, start: {init}")
        rec, used, tried_starts = None, None, []
        # "poses": cameras from a file in VGGT's format ({chosen, placed}), e.g. a phone's tracked poses: started as VGGT's are
        order = {"auto": ["vggt", "global", "incremental"], "vggt": ["vggt", "global", "incremental"],
                 "poses": ["poses", "global", "incremental"], "global": ["global", "incremental"], "incremental": ["incremental"]}[init]
        for kind in order:
            rep.part(f"start_{kind}")
            try:
                if kind in ("vggt", "poses"):
                    if not vggt or not vggt.exists():
                        log("no VGGT cameras were given: trying the next start")
                        tried_starts.append("vggt: no cameras")
                        continue
                    cams = json.loads(vggt.read_text())
                    rec = start_from_cameras(open_cache(db, names), cams, o, ba, rep)
                    rep.data[kind] = {k: cams.get(k) for k in ("seconds", "gpu_peak_mib", "device", "pairs_checked", "dropped")}
                    rep.data[kind]["chosen"] = len(cams["chosen"])
                elif kind == "global":
                    rec, others = start_global(db, photos, names, o, ba, work)
                else:
                    rec, others = start_incremental(db, photos, names, o, ba, work)
            except Exception as e:      # a start that fails is no reason not to try the next
                log(f"start {kind} failed: {type(e).__name__}: {e}")
                rec = None
            tried_starts.append(f"{kind}: {'ok, ' + str(rec.num_reg_images()) + ' cameras' if rec else 'no model'}")
            if rec is not None:
                used = kind
                break
        rep.data["starts"] = tried_starts
        if rec is None:
            log("no start gave a model")
            return rep.save(kind="none", placed={}, tried=sorted(ids), start=None, resolve={"due": False, "why": "no model"},
                            remesh={"due": False, "why": "no model"})
        placed = {rec.image(i).name: used for i in rec.reg_image_ids()}
        rep.data["start"] = {"kind": used, **stats_of(rec)}
        log(f"start by {used}: {stats_of(rec)}")
        # the photos the start left out, registered against its model: the usual way, then on the app's rest terms
        rep.part("register")
        S = Session(open_cache(db, names), rec, o, ba)
        mo, tri = o.get_mapper(), o.get_triangulation()
        n1 = register_loop(S, mo, tri, True, "pnp", placed)
        S.restart()
        mo.abs_pose_min_num_inliers, mo.abs_pose_min_inlier_ratio = REST
        n2 = register_loop(S, mo, tri, True, "pnp-rest", placed)
        rep.part("polish")      # principal points too, if that holds up; then the last word on the most exact solver
        polished = S.polish(o.get_mapper(), tri)
        rep.part("final_adjust")
        S.refine(o.get_mapper(), tri, 1, final=True)
        rec = S.close()
        rep.data["refinements"] = {"solves": S.solves, "undone": S.undone, "lenses_held": S.lenses_held, "principal_points": polished}
        placed = {n: how for n, how in placed.items() if has_pose(rec, n)}
        log(f"registered {n1} + {n2} (rest terms) more: {stats_of(rec)}; lenses held: {S.lenses_held}; principal points refined: {polished}")
        if resolve and s.has_model():       # back into the frame the first build set
            prev = pycolmap.Reconstruction(str(root / "model"))
            a, b = poses(rec), poses(prev)
            sim = similarity(a, b, sorted(set(a) & set(b)))
            if sim:
                transform(rec, sim)
                rep.data["aligned_on"] = len(set(a) & set(b))
            else:
                log("the re-solved model shares under 3 cameras with the old one: its frame is new")
        kind = "resolve" if resolve and s.has_model() else "first"
        tried = sorted(ids)
        added_ids: list[int] = []
    else:
        old_poses = poses(old)
        was = set(old_poses)
        cand = sorted(n for n in ids if n not in was)
        log(f"add: {len(cand)} photo(s) to place against a model of {len(was)}")
        rec, kind, tried = old, "add", cand
        S = Session(open_cache(db, was | set(cand)), rec, o, ba)
        mo, tri = o.get_mapper(), o.get_triangulation()
        mo.fix_existing_frames = True
        old_lenses = {old.image(i).camera_id for i in old.reg_image_ids()}
        lens_before = {c: list(old.camera(c).params) for c in old_lenses}
        mo.constant_cameras = set(old_lenses)
        added_ids = []
        for how, terms in (("pnp", None), ("pnp-rest", REST)):
            rep.part("register" if terms is None else "register_rest")
            if terms:
                S.restart()
                mo.abs_pose_min_num_inliers, mo.abs_pose_min_inlier_ratio = terms
            before = dict(placed)
            register_loop(S, mo, tri, False, how, placed)
            new_ids = [rec.find_image_with_name(n).image_id for n in placed if n not in before]
            rep.part("adjust_added")
            refine_added(S.mapper, rec, o, mo, tri, new_ids, ba, fixed_lenses=old_lenses)
            added_ids += new_ids
        S.mapper.filter_frames(mo)      # a new camera whose lens came out degenerate is not placed
        S.close()
        placed = {n: how for n, how in placed.items() if has_pose(rec, n)}
        after = poses(rec)
        rep.data["old_cameras_moved"] = max((float(np.abs(after[n][1] - t).max()) for n, (_, t) in old_poses.items() if n in after), default=0.0)
        rep.data["old_cameras_lost"] = sorted(was - set(after))
        rep.data["old_lenses_changed"] = sum(1 for c, p in lens_before.items() if rec.exists_camera(c) and list(rec.camera(c).params) != p)
    rep.part("gates")
    now = {rec.image(i).name for i in rec.reg_image_ids()}
    left = sorted(n for n in ids if n not in now)
    left_linked = linked_to(left, now, ids, link)
    tried_linked = linked_to(tried, now - set(placed), ids, link) if kind == "add" else []
    left_joined = joined(left, now, ids, link)
    due, why = (False, "this was a full solve") if kind != "add" else st.resolve_due(
        len(now) - len(placed), (s.manifest.get("solve") or {}).get("placed"), len(tried_linked), len([n for n in tried_linked if n in placed]),
        len(left_joined), len(now))
    points_new = max(0, rec.num_points3D() - points_before)
    gain = None
    if kind == "add" and s.manifest.get("mesh"):
        rep.part("coverage")
        try:
            gain = surface_gain(root, pycolmap.Reconstruction(str(root / "model")), rec)
        except Exception as e:      # the gate then falls back on counting points
            log(f"coverage of the mesh couldn't be measured: {type(e).__name__}: {e}")
        log(f"coverage: {gain}")
    mesh_due, mesh_why = st.remesh_due(s.manifest.get("mesh") if kind == "add" else None, len(now), points_before, points_new, gain)
    request = None
    if left_linked and kind in ("add", "first", "resolve"):
        request = {"names": left_linked, "anchors": anchors_for(left_linked, now, ids, link)}
    rep.part("write")
    out = root / "model.new"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    rec.update_point_3d_errors()
    rec.write(str(out))
    if others and scenes:       # the other models a start found: other sides of the rock, or other rocks
        shutil.rmtree(root / "scenes.new", ignore_errors=True)
        for k, m in enumerate(others):
            (root / "scenes.new" / str(k)).mkdir(parents=True)
            m.write(str(root / "scenes.new" / str(k)))
    shutil.rmtree(work, ignore_errors=True)
    log(f"{kind}: placed {len(placed)} this time, {len(now)} in all, {len(left)} left out ({len(left_linked)} of them linked to the model)")
    log(f"full re-solve due: {due} ({why}); new mesh due: {mesh_due} ({mesh_why})")
    return rep.save(kind=kind, placed=placed, tried=tried, left=left, left_linked=left_linked, left_joined=left_joined, model=stats_of(rec),
                    points_before=points_before, points_new=points_new, other_models=[m.num_reg_images() for m in others],
                    resolve={"due": due, "why": why}, remesh={"due": mesh_due, "why": mesh_why}, assist_request=request,
                    added_ids=added_ids)


def assist(root: Path, cams_file: Path, ba: str, threads: int) -> dict:
    """Places photos registration could not, from VGGT's cameras for them and for some placed photos (the anchors):
    VGGT's frame is laid over the model's through the anchors, each photo gets its camera from that, its matches are
    triangulated, and it and its points are refined with the model's cameras fixed. A photo stays if enough of its
    points survive. Works on model.new, as place() left it."""
    rep = Report(root, "assist")
    db = root / "database.db"
    cams = json.loads(cams_file.read_text())
    rec = pycolmap.Reconstruction(str(root / "model.new"))
    have = poses(rec)
    v = {n: (np.array(R), np.array(t)) for n, (R, t, _) in cams["placed"].items()}
    anchors = [n for n in cams["anchors"] if n in have and n in v]
    sim = similarity(v, have, anchors)
    todo = [n for n in cams["names"] if n in v and n not in have]
    if sim is None or not todo:
        log(f"assist: {len(anchors)} anchors both have, {len(todo)} photos to place: nothing to do")
        return rep.save(placed={}, anchors=len(anchors), tried=todo)
    scale, Rs, shift = sim
    centre = lambda n: -v[n][0].T @ v[n][1]      # noqa: E731
    off = [float(np.linalg.norm(scale * Rs @ centre(n) + shift - (-have[n][0].T @ have[n][1]))) for n in anchors]
    size = float(np.sqrt(np.var(np.array([-have[n][0].T @ have[n][1] for n in have]), axis=0).sum())) or 1.0
    log(f"assist: VGGT's frame over the model's on {len(anchors)} anchors, median {np.median(off) / size:.3f} of the scene's size off")
    o = pipeline_options(threads, 0)
    names = set(have) | set(todo)
    cache = open_cache(db, names)
    rec.load(cache)
    ids = {im.name: i for i, im in cache.images.items()}
    todo = [n for n in todo if n in ids]
    rep.part("pose")
    fixed = {rec.image(i).camera_id for i in rec.reg_image_ids()}
    for n in todo:
        R = v[n][0] @ Rs.T
        c = scale * Rs @ centre(n) + shift
        set_pose(rec, ids[n], R, -R @ c, cams["placed"][n][2])
        rec.register_frame(rec.image(ids[n]).frame_id)
    mapper = pycolmap.IncrementalMapper(cache)
    mapper.begin_reconstruction(rec)
    mo, tri = o.get_mapper(), o.get_triangulation()
    kept: list[str] = list(todo)
    for k, (angle, err) in enumerate(ROUNDS):
        rep.part(f"triangulate{k}")
        tri.create_max_angle_error = tri.continue_max_angle_error = tri.re_max_angle_error = angle
        tri.merge_max_reproj_error = tri.complete_max_reproj_error = err
        mo.filter_max_reproj_error = err
        for n in kept:
            mapper.triangulate_image(tri, ids[n])
        rep.part(f"adjust{k}")
        refine_added(mapper, rec, o, mo, tri, [ids[n] for n in kept], ba, rounds=1, fixed_lenses=fixed)
        log(f"assist round {k + 1}: points per photo " + ", ".join(f"{n}: {rec.image(ids[n]).num_points3D}" for n in kept))
        kept = [n for n in kept if rec.image(ids[n]).num_points3D >= (ASSIST_MIN_POINTS if k == len(ROUNDS) - 1 else 3)]
    for n in todo:
        if n not in kept and rec.image(ids[n]).has_pose:
            mapper.observation_manager.deregister_frame(rec.image(ids[n]).frame_id)
    mapper.end_reconstruction(False)
    rec.update_point_3d_errors()
    rec.write(str(root / "model.new"))
    log(f"assist: {len(kept)} of {len(todo)} photos kept {ASSIST_MIN_POINTS}+ points and are placed")
    return rep.save(placed={n: "vggt-assist" for n in kept}, tried=todo, anchors=len(anchors),
                    anchors_off=round(float(np.median(off) / size), 4), model=stats_of(rec))


# ---------------------------------------------------------------- a state from a mesh that has none

def bootstrap(root: Path, cameras: Path, threads: int) -> dict:
    """A model from a finished mesh's cameras.json (each placed photo's world-to-camera q, t and its lens, in the
    mesh's own frame), for a mesh made before states were kept: the cameras are set as they are, COLMAP's verified
    matches triangulated from them, and the points refined with every camera held, so that the model is in the
    mesh's frame exactly and later adds keep to it. Writes model.new; photos cameras.json names that the database
    lacks (or whose lens doesn't fit) are left out and reported."""
    rep = Report(root, "bootstrap")
    db, photos = root / "database.db", root / "photos"
    cams = json.loads(cameras.read_text())
    cams = cams.get("cameras", cams) if isinstance(cams, dict) else cams
    cache = open_cache(db, {c["name"] for c in cams})
    rec = pycolmap.Reconstruction()
    rec.load(cache)
    by_name = {im.name: i for i, im in cache.images.items()}
    set_, skipped = [], {}
    rep.part("cameras")
    for c in cams:
        i = by_name.get(c["name"])
        if i is None:
            skipped[c["name"]] = "not in the database (no features, or no verified matches)"
            continue
        cam = rec.camera(rec.image(i).camera_id)
        lens = c.get("lens")
        if lens:
            if (lens["width"], lens["height"]) != (cam.width, cam.height):
                skipped[c["name"]] = f"its lens is for {lens['width']}x{lens['height']}, the photo is {cam.width}x{cam.height}"
                continue
            cam.model = pycolmap.CameraModelId(int(lens["model"]))
            cam.params = [float(x) for x in lens["params"]]
        w, x, y, z = c["q"]
        im = rec.image(i)
        rec.frame(im.frame_id).set_cam_from_world(im.camera_id, pycolmap.Rigid3d(pycolmap.Rotation3d([x, y, z, w]), np.array(c["t"], float)))
        rec.register_frame(im.frame_id)
        set_.append(c["name"])
    if len(set_) < 2:
        return rep.save(kind="none", placed={}, skipped=skipped, why="fewer than two of its cameras are in the database")
    rep.part("triangulate")
    o = pipeline_options(threads, 0)
    out = root / "log" / "current" / "work-bootstrap"
    out.mkdir(parents=True, exist_ok=True)
    before = poses(rec)
    rec = pycolmap.triangulate_points(rec, str(db), str(photos), str(out), clear_points=True, options=o, refine_intrinsics=False)
    after = poses(rec)
    moved = max(float(np.abs(after[n][1] - t).max()) for n, (_, t) in before.items() if n in after)
    turned = max(float(np.degrees(np.arccos(np.clip((np.trace(after[n][0] @ R.T) - 1) / 2, -1, 1)))) for n, (R, _) in before.items() if n in after)
    rep.part("write")
    new = root / "model.new"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    rec.update_point_3d_errors()
    rec.write(str(new))
    shutil.rmtree(out, ignore_errors=True)
    log(f"bootstrap: {len(set_)} cameras set ({len(skipped)} skipped), {rec.num_points3D()} points, "
        f"{rec.compute_mean_reprojection_error():.2f} px; cameras moved at most {moved:.2e} (units), turned {turned:.2e} deg")
    return rep.save(kind="bootstrap", placed={n: "cameras.json" for n in after}, tried=sorted(by_name), skipped=skipped,
                    model=stats_of(rec), moved_max=moved, turned_max_deg=turned)


# ---------------------------------------------------------------- whether an add calls for a new mesh

def read_ply(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Vertices and faces of OpenMVS's binary PLY mesh."""
    import re as _re
    data = path.read_bytes()
    end = data.index(b"end_header\n") + len(b"end_header\n")
    header = data[:end].decode()
    nv = int(_re.search(r"element vertex (\d+)", header).group(1))
    nf = int(_re.search(r"element face (\d+)", header).group(1))
    vprops = _re.search(r"element vertex \d+\n(.*?)element face", header, _re.S).group(1).count("property")
    V = np.frombuffer(data, "<f4", nv * vprops, end).reshape(nv, vprops)[:, :3].astype(np.float64)
    F = np.frombuffer(data, np.dtype([("n", "u1"), ("i", "<u4", 3)]), nf, end + nv * vprops * 4)["i"].astype(np.int64)
    return V, F


def plan_cameras(rec: pycolmap.Reconstruction) -> list[dict]:
    """The model's cameras as the app's cameras.json has them, for backend/captureplan.py's coverage()."""
    out = []
    for i in rec.reg_image_ids():
        im = rec.image(i)
        T = im.cam_from_world()
        x, y, z, w = T.rotation.quat
        cam = rec.camera(im.camera_id)
        out.append({"name": im.name, "q": [w, x, y, z], "t": list(T.translation),
                    "lens": {"model": int(cam.model.value), "params": list(cam.params), "width": cam.width, "height": cam.height}})
    return out


def surface_gain(root: Path, before: pycolmap.Reconstruction, after: pycolmap.Reconstruction) -> dict | None:
    """What an add brings to the rock the mesh shows, measured as the app's capture plan measures it (captureplan.py's
    coverage() and NEED, on the mesh cut down as it cuts it), and what it brings beyond it: the share of the mesh's
    area covered well enough (NEED's patch: fine enough, from enough directions) by the cameras before and after,
    and the share of the add's new 3D points that lie off the mesh (rock it doesn't have yet). None without a mesh.
    The scale is unknown here, so it is taken as captureplan takes an unscaled mesh's (GUESS_ACROSS_M across)."""
    ply = root / "dense" / "scene_dense_mesh.ply"
    if not ply.exists():
        return None
    sys.path.insert(0, "/backend")
    import captureplan as cp
    V, F = cp.decimate(*read_ply(ply))
    C, N, area = cp.geometry(V, F)
    lo, hi = np.percentile(V, 5, axis=0), np.percentile(V, 95, axis=0)
    size = float((hi - lo).max()) or 1.0
    mpu = cp.GUESS_ACROSS_M / size
    need = cp.NEED["patch"]

    def covered(rec):
        best, views = cp.coverage(C, N, plan_cameras(rec))
        good = (best * mpu * 1000 <= need["gsd_mm"]) & (views >= need["views"])
        return float(area[good].sum() / area.sum()), float(area[np.isfinite(best)].sum() / area.sum())
    good0, seen0 = covered(before)
    good1, seen1 = covered(after)
    # new points off the mesh: in no cell (of 1/40 the mesh's size) next to one a face centre is in
    cell = size / 40
    occupied = {tuple(k) for k in np.floor(C / cell).astype(np.int64)}
    near = {(a + i, b + j, c + k) for a, b, c in occupied for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1)}
    old_ids = set(before.point3D_ids())
    new = np.array([p.xyz for pid, p in after.points3D.items() if pid not in old_ids]).reshape(-1, 3)
    off = int(sum(1 for k in np.floor(new / cell).astype(np.int64) if tuple(k) not in near)) if len(new) else 0
    return {"faces": len(F), "covered_before": round(good0, 4), "covered_after": round(good1, 4), "seen_before": round(seen0, 4),
            "seen_after": round(seen1, 4), "new_points": len(new), "new_points_off_mesh": off}


# ---------------------------------------------------------------- dense, mesh, texture

MVS = "/usr/local/bin/OpenMVS"


def run(cmd: list, cwd: Path) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def undistort(root: Path, threads: int) -> dict:
    rep = Report(root, "undistort")
    out = root / "dense.new"
    shutil.rmtree(out, ignore_errors=True)
    (out / "model").mkdir(parents=True)
    # COLMAP leaves a camera that has no distortion in its own model, and OpenMVS reads only pinhole ones
    rec, plain = pycolmap.Reconstruction(str(root / "model")), 0
    for cam in rec.cameras.values():
        if cam.is_undistorted() and cam.model != pycolmap.CameraModelId.PINHOLE:
            f, cx, cy = cam.mean_focal_length(), cam.principal_point_x, cam.principal_point_y
            cam.model = pycolmap.CameraModelId.PINHOLE
            cam.params = [f, f, cx, cy]
            plain += 1
    rec.write(str(out / "model"))
    rep.data["cameras_without_distortion"] = plain
    pycolmap.undistort_images(str(out / "dense"), str(out / "model"), str(root / "photos"), output_type="COLMAP", num_threads=threads)
    return rep.save(images=len(list((out / "dense" / "images").iterdir())))


def mvs(root: Path, step: str, threads: int, cpu: bool) -> dict:
    """One OpenMVS step in dense.new: import (the undistorted model as its scene), dense (depth maps and their
    fusion: CUDA unless cpu), mesh, texture."""
    rep, d = Report(root, step), root / "dense.new"
    t = ["--max-threads", threads] if threads > 0 else []
    if step == "import":
        run([f"{MVS}/InterfaceCOLMAP", "-i", "dense", "-o", "scene.mvs", "--image-folder", d / "dense" / "images", "-w", d], d)
    elif step == "dense":
        run([f"{MVS}/DensifyPointCloud", "scene.mvs", "-w", d, "--resolution-level", "2", *t, *(["--cuda-device", "-2"] if cpu else [])], d)
    elif step == "mesh":
        run([f"{MVS}/ReconstructMesh", "scene_dense.mvs", "-w", d, *t], d)
    elif step == "texture":
        run([f"{MVS}/TextureMesh", "scene_dense.mvs", "-m", "scene_dense_mesh.ply", "-o", "scene_dense_mesh_texture.mvs", "-w", d,
             "--export-type", "glb", "--empty-color", str(0x808080), "--max-texture-size", "4096", *t], d)
        dmaps = sum(f.stat().st_size for f in d.glob("*.dmap"))
        rep.data["dmap_mb"] = round(dmaps / 2 ** 20, 1)
    return rep.save()


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("step")
    p.add_argument("state", type=Path)
    p.add_argument("cameras", nargs="?", type=Path)
    p.add_argument("--feat", default="sift")
    p.add_argument("--init", default="auto", choices=["auto", "vggt", "poses", "global", "incremental"])
    p.add_argument("--vggt", "--cameras", dest="vggt", type=Path, help="starting cameras: VGGT's, or poses from elsewhere")
    p.add_argument("--ba", default="auto", choices=["auto", "cpu", "gpu", "caspar"])
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--threads", type=int, default=-1)
    p.add_argument("--cpu", action="store_true")
    p.add_argument("--resolve", action="store_true")
    p.add_argument("--no-scenes", action="store_true")
    a = p.parse_args(argv)
    if a.step == "features":
        features(a.state, a.feat, a.threads)
    elif a.step == "match":
        match(a.state, not a.cpu, a.threads, a.feat)
    elif a.step == "place":
        place(a.state, a.init, a.vggt, a.ba, a.seed, a.threads, a.resolve, not a.no_scenes)
    elif a.step == "bootstrap":
        bootstrap(a.state, a.cameras, a.threads)
    elif a.step == "assist":
        assist(a.state, a.cameras, a.ba, a.threads)
    elif a.step == "undistort":
        undistort(a.state, a.threads)
    elif a.step in ("import", "dense", "mesh", "texture"):
        mvs(a.state, a.step, a.threads, a.cpu)
    else:
        raise SystemExit(f"unknown step {a.step}")


if __name__ == "__main__":
    main(sys.argv[1:])
