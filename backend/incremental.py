"""Adding photos to a mesh's kept model, rather than placing every photo again (since PIPELINE_VERSION 2): the new
photos' features (as the mesh's were made), matched with every photo, registered against the model with its cameras and
lenses held, and refined alone; then the pipeline's own steps from levelling on (pipeline.reconstruct_colmap_openmvs()).
Through pycolmap 4.2.1, as experiments/mapper/incr worked it out (its README has the measurements: added cameras
0.4-1 degree, median, from a from-scratch build's; the old ones unmoved).

An add gives way to a full re-solve (resolve_due()) when the model would grow by more than half since its last full
solve, when under half of 6 or more new photos linked to it can be placed, when photos joined to it by verified matches
are left out (a quarter as many as it places), or when the model passed its own check before and fails it with the new photos. Each decision is
kept beside the new mesh (ADD_FILE), with the measure it was taken on, for the admin page to count and tune by.

    add(kept, images, masks, work, log)      the add, into work/ as the pipeline would have left its placing; or Resolve
"""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
ADD_FILE = "add.json"       # beside a mesh: whether it was made by adding photos to its former version's model, or not
MAX_IMAGE_ID = 2147483647
MIN_MATCHES, MIN_INLIERS = 20, 12
REST = (15, 0.15)       # the rest step's terms: a photo may be placed on this few inliers, and this share of its matches
MIN_TRI_ANGLE = 1.0     # degrees: points seen across less are kept (COLMAP's 1.5 left low-parallax boulders next to none)
# the gates (experiments/mapper/incr/state.py's first guesses, reported with every decision so that they can be tuned)
RESOLVE_GROWTH = 0.5        # placed photos grown by more than this share since the last full solve
RESOLVE_MIN_TRIED = 6       # with at least this many new photos linked to placed ones,
RESOLVE_MIN_PLACED = 0.5    # under this share of them placed
RESOLVE_MIN_JOINED = 3      # this many new photos joined to the model by verified matches, and this share of those
RESOLVE_JOINED_SHARE = 0.25 # placed, left out (the incr experiment's 0.1, of any photo, re-solved every add of La Marie
                            # Rose's 16 to 60, Nov 20, where a full solve placed no more: 38 of 76 either way)


class Unavailable(RuntimeError):
    """Photos can't be added to this mesh's model here (no pycolmap, no kept model or features): it's made again whole."""


class Resolve(RuntimeError):
    """A gate says the model is to be solved again in full: .info says which, and what it measured."""

    def __init__(self, info: dict):
        super().__init__(info["why"])
        self.info = info


def available() -> str | None:
    """Why photos can't be added to models here, or None if they can."""
    try:
        import pycolmap  # noqa: F401
    except ImportError as e:
        return f"pycolmap isn't installed here ({e})"
    return None


def db_images(db: Path) -> dict[str, int]:
    """Name -> image id of the photos that have features."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {n: i for i, n in con.execute("SELECT image_id, name FROM images WHERE image_id IN (SELECT image_id FROM keypoints WHERE rows > 0)")}
    finally:
        con.close()


def links(db: Path, least: int = MIN_MATCHES) -> dict[int, dict[int, int]]:
    """Image id -> {image id: verified inliers} for the pairs with at least `least`."""
    out: dict[int, dict[int, int]] = {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for p, rows in con.execute("SELECT pair_id, rows FROM two_view_geometries WHERE rows >= ?", (least,)):
            a, b = divmod(p, MAX_IMAGE_ID)
            out.setdefault(a, {})[b] = rows
            out.setdefault(b, {})[a] = rows
    finally:
        con.close()
    return out


def linked_to(names, placed: set[str], ids: dict[str, int], link: dict) -> list[str]:
    """Those of the photos named that share verified matches with a placed one."""
    placed_ids = {ids[n] for n in placed if n in ids}
    return [n for n in names if n in ids and placed_ids & set(link.get(ids[n], {}))]


def joined(names, placed: set[str], ids: dict[str, int], link: dict) -> list[str]:
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


def resolve_due(placed_before: int, placed_at_solve: int | None, tried_linked: int, placed_linked: int, left_joined: int,
                placed_now: int, check_now: dict | None, check_before: dict | None) -> dict:
    """Whether to solve everything again rather than add, which gate says so, and what it measured:
    {due, gate, why, measured}."""
    grown = placed_before + placed_linked
    measured = {"placed_before": placed_before, "placed_at_solve": placed_at_solve, "tried_linked": tried_linked,
                "placed_linked": placed_linked, "left_joined": left_joined, "placed_now": placed_now,
                "growth": round(grown / placed_at_solve - 1, 3) if placed_at_solve else None,
                "placed_share": round(placed_linked / tried_linked, 3) if tried_linked else None}
    out = lambda gate, why: {"due": bool(gate), "gate": gate, "why": why, "measured": measured}  # noqa: E731
    if placed_at_solve and grown > placed_at_solve * (1 + RESOLVE_GROWTH):
        return out("growth", f"{grown} photos placed against {placed_at_solve} at the last full solve: more than {RESOLVE_GROWTH:.0%} grown")
    if tried_linked >= RESOLVE_MIN_TRIED and placed_linked < tried_linked * RESOLVE_MIN_PLACED:
        return out("placed_share", f"only {placed_linked} of {tried_linked} new photos linked to placed ones could be placed "
                                   f"(under {RESOLVE_MIN_PLACED:.0%})")
    if left_joined >= max(RESOLVE_MIN_JOINED, RESOLVE_JOINED_SHARE * placed_now):
        return out("joined", f"{left_joined} photos joined to the model by verified matches are left out ({placed_now} placed): "
                             "a full solve takes them together")
    if check_now and not check_now.get("ok", True) and (check_before or {}).get("ok", True):
        return out("check", f"with the new photos the model fails its own check ({check_now.get('why')})")
    return out(None, "the model takes the new photos as it is")


def _options(threads: int):
    import pycolmap
    o = pycolmap.IncrementalPipelineOptions()
    o.min_num_matches, o.ba_refine_principal_point = MIN_MATCHES, True
    o.mapper.filter_min_tri_angle = o.triangulation.min_angle = MIN_TRI_ANGLE
    o.num_threads = o.mapper.num_threads = threads
    o.extract_colors = False
    return o


def _ba(o, local: bool = False):
    import pycolmap
    b = o.get_local_bundle_adjustment() if local else o.get_global_bundle_adjustment()
    b.print_summary = False
    b.ceres.use_gpu = False     # Ceres on the CPU (Caspar was wrong on 31de; Ceres on the GPU no faster at these sizes)
    return b


def _cache(db: Path, names: set[str]):
    import pycolmap
    co = pycolmap.DatabaseCacheOptions()
    co.min_num_matches = MIN_MATCHES
    co.image_names = set(names)
    database = pycolmap.Database.open(str(db))
    try:
        return pycolmap.DatabaseCache.create(database, co)
    finally:
        database.close()


def _register(mapper, rec, o, mo, tri, placed: dict[str, str], how: str) -> list[int]:
    """COLMAP's own loop with the model's cameras held (mo): the best candidate next, its points triangulated, its
    neighbourhood refined. Returns the image ids it registered; placed gains their names."""
    local = _ba(o, local=True)
    done, ok, prev_ok = [], True, True
    while ok or prev_ok:
        prev_ok, ok, image_id = ok, False, None
        for image_id in mapper.find_next_images(mo, False):
            ok = mapper.register_next_image(mo, image_id)
            if ok:
                break
        if ok:
            mapper.triangulate_image(tri, image_id)
            mapper.iterative_local_refinement(o.ba_local_max_refinements, o.ba_local_max_refinement_change, mo, local, tri, image_id)
            placed[rec.image(image_id).name] = how
            done.append(image_id)
    return done


def _refine_added(mapper, rec, o, mo, tri, added: list[int], fixed_lenses: set[int], rounds: int = 2) -> None:
    """The cameras just added and the points they see refined, every other camera (pose and lens) as it is; their points'
    tracks completed and merged at COLMAP's tolerances, and filtered in the added photos only."""
    import pycolmap
    for _ in range(rounds):
        config, points = pycolmap.BundleAdjustmentConfig(), set()
        for i in added:
            if rec.image(i).has_pose:
                config.add_image(i)
                points.update(p.point3D_id for p in rec.image(i).points2D if p.has_point3D())
        for p in points:
            config.add_variable_point(p)
        for cid in fixed_lenses:
            config.set_constant_cam_intrinsics(cid)
        b = _ba(o)
        b.refine_principal_point = False
        pycolmap.create_default_bundle_adjuster(b, config, rec).solve()
        changed = 0
        if points:
            alive = {p for p in points if rec.exists_point3D(p)}
            changed += mapper.triangulator.complete_tracks(tri, alive)
            changed += mapper.triangulator.merge_tracks(tri, {p for p in alive if rec.exists_point3D(p)})
        changed += mapper.observation_manager.filter_points3D_in_images(mo.filter_max_reproj_error, mo.filter_min_tri_angle, set(added))
        if changed == 0:
            break


def _check(model: Path, db: Path) -> dict | None:
    try:
        import modelcheck
        return modelcheck.check(model, db)
    except Exception:  # noqa: BLE001 - the check gate is then left out
        return None


def add(kept_mesh: Path, unpacked: Path, images: Path, masks: Path | None, work: Path, log: Log, colmap: str,
        run: Callable[[list[str]], None], use_gpu: bool, threads: int = 0) -> dict:
    """Add the photos in images/ that the mesh's model doesn't have to it, leaving work/database.db and
    work/sparse/0 as the pipeline's placing would have (its later steps then run as for any mesh).

    Args:
        kept_mesh: the folder of the mesh added to (its kept features, check.json and ADD_FILE).
        unpacked: its kept reconstruction, taken out (reconstruction.unpack()).
        run: runs a COLMAP command, raising if it fails.

    Returns:
        What it did, for ADD_FILE: {kind: "add" | "none", added, placed, left, decision, seconds, ...}.

    Raises:
        Unavailable: the mesh kept no features, or pycolmap isn't here. Resolve: a gate says to solve it all again.
    """
    import reconstruction
    why = available()
    if why:
        raise Unavailable(why)
    t0, seconds = time.time(), {}
    db, sparse = work / "database.db", work / "sparse"
    try:
        reconstruction.full_database(kept_mesh, unpacked, db)
    except FileNotFoundError as e:
        raise Unavailable(str(e)) from e
    build = json.loads((unpacked / "build.json").read_text())
    have = db_images(db)
    photos = sorted(p.name for p in images.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    new = [n for n in photos if n not in have]
    if not new:
        return {"kind": "none", "added": 0, "why": "no photos it doesn't have"}
    log(f"adding {len(new)} photo(s) to a model of {len(have)}")

    # features of the new photos, as the mesh's were made (COLMAP does only the photos its database lacks)
    extract = build.get("extract") or {}
    swap = {"<database>": str(db), "<photos>": str(images), "<masks>": str(masks) if masks else None}
    args = [swap.get(a, a) for a in extract.get("args") or []]
    if None in args:        # masks then, none now: without
        i = args.index(None)
        del args[i - 1:i + 1]
    device = "1" if extract.get("device") == "gpu" else "0"
    names = work / "new-photos.txt"
    names.write_text("\n".join(new) + "\n")
    run([colmap, "feature_extractor", *args, "--image_list_path", str(names), "--FeatureExtraction.use_gpu", device])
    seconds["features"] = round(time.time() - t0, 1)

    # the new photos matched with every photo
    t1 = time.time()
    ids = db_images(db)
    pairs = work / "new-pairs.txt"
    pairs.write_text("".join(f"{a} {b}\n" for i, a in enumerate(new) if a in ids for b in [*sorted(have), *new[:i]] if b in ids))
    run([colmap, "matches_importer", "--database_path", str(db), "--match_list_path", str(pairs), "--match_type", "pairs",
         "--FeatureMatching.guided_matching", "1", "--FeatureMatching.use_gpu", "1" if use_gpu else "0",
         "--TwoViewGeometry.min_num_inliers", str(MIN_INLIERS)])
    seconds["matching"] = round(time.time() - t1, 1)

    # registration against the model, its cameras and lenses held, then the added ones refined alone
    import pycolmap
    t2 = time.time()
    o = _options(threads or 0)
    rec = pycolmap.Reconstruction(str(unpacked / "model"))
    was = {rec.image(i).name for i in rec.reg_image_ids()}
    ids, link = db_images(db), links(db)
    mapper = pycolmap.IncrementalMapper(_cache(db, was | set(new)))
    mapper.begin_reconstruction(rec)
    mo, tri = o.get_mapper(), o.get_triangulation()
    mo.fix_existing_frames = True
    old_lenses = {rec.image(i).camera_id for i in rec.reg_image_ids()}
    mo.constant_cameras = set(old_lenses)
    placed: dict[str, str] = {}
    added: list[int] = []
    for how, terms in (("pnp", None), ("pnp-rest", REST)):
        if terms:
            mapper.end_reconstruction(False)
            mapper = pycolmap.IncrementalMapper(_cache(db, was | set(new)))
            mapper.begin_reconstruction(rec)
            mo.abs_pose_min_num_inliers, mo.abs_pose_min_inlier_ratio = terms
        got = _register(mapper, rec, o, mo, tri, placed, how)
        _refine_added(mapper, rec, o, mo, tri, got, old_lenses)
        added += got
    mapper.filter_frames(mo)
    mapper.end_reconstruction(False)
    seconds["register"] = round(time.time() - t2, 1)
    now = {rec.image(i).name for i in rec.reg_image_ids()}
    placed = {n: h for n, h in placed.items() if n in now}
    left = sorted(n for n in ids if n not in now)
    tried_linked = linked_to(new, was, ids, link)
    # new photos joined to the model but left out (the last full solve's own leftovers, and photos only the new ones join,
    # don't count: on photos from about one place a full solve leaves as many out, and every add would give way to one)
    old_ids = {n: i for n, i in ids.items() if n in have}
    old_link = {i: {j: r for j, r in nb.items() if j in set(old_ids.values())} for i, nb in link.items() if i in set(old_ids.values())}
    joined_before = set(joined([n for n in old_ids if n not in was], was, old_ids, old_link))
    joined_now = [n for n in joined(left, now, ids, link) if n not in joined_before and n in new]
    sparse.mkdir(parents=True, exist_ok=True)
    out = sparse / "0"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    rec.write(str(out))

    # the gates: whether a full solve is due instead
    prior = {}
    try:
        prior = json.loads((kept_mesh / ADD_FILE).read_text())
    except (OSError, ValueError):
        pass
    at_solve = prior.get("solve_placed") or len(was)
    check_before = None
    try:
        check_before = json.loads((kept_mesh / "check.json").read_text())
    except (OSError, ValueError):
        pass
    check_now = _check(out, db)
    decision = resolve_due(len(was), at_solve, len(tried_linked), len([n for n in tried_linked if n in placed]),
                           len(joined_now), len(now), check_now, check_before)
    decision["measured"]["joined_before"] = len(joined_before)
    record = {"kind": "add", "to": kept_mesh.name, "added": len(new), "placed": len(placed), "placed_now": len(now),
              "left": len(left), "old_lost": sorted(was - now), "solve_placed": at_solve, "decision": decision,
              "seconds": {**seconds, "all": round(time.time() - t0, 1)}}
    if decision["due"]:
        shutil.rmtree(out, ignore_errors=True)
        raise Resolve({**record, "kind": "resolve", "why": decision["why"]})
    log(f"added {len(placed)} of {len(new)} photo(s): {len(now)} placed in all, {len(left)} left out; {decision['why']}")
    return record
