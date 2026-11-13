"""VGGT's cameras as the start of every reconstruction, not only the fallback (runs in the app's -gpu image, from
bench.py): vggt_start.py <database> <photos> <run dir>

As backend/vggt_place.py's place(): up to 80 photos, the best linked by verified matches, get cameras from VGGT; COLMAP
triangulates its matches from them and refines everything, three rounds. Added here: before triangulating, each
camera's rotation relative to each photo it shares a verified pair with is checked against that pair's two-view
geometry in the database, and a camera that disagrees with most of its pairs is left out (the app's later step,
image_registrator, gets another go at it with the rest). Writes <run dir>/sparse/0 and vggt.json: the seconds of
each part, VGGT's peak GPU memory, and what the check dropped.
"""
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "/app/backend")
import vggt_place  # noqa: E402

MAX_ANGLE = 20.0        # degrees a VGGT pair's relative rotation may be off the two-view geometry's and still agree


def pair_rotations(db: Path, chosen: list[dict], focal: dict[str, float]) -> dict[tuple[str, str], list]:
    """For each verified pair among the chosen photos, the two rotations (second camera from first) its two-view
    geometry allows: from its essential matrix, or its fundamental matrix with VGGT's focal lengths."""
    by_id = {c["id"]: c for c in chosen}
    K = {c["id"]: np.array([[focal[c["name"]], 0, c["width"] / 2], [0, focal[c["name"]], c["height"] / 2], [0, 0, 1]])
         for c in chosen}
    W = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1.0]])
    out = {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for pair, rows, config, F, E in con.execute("SELECT pair_id, rows, config, F, E FROM two_view_geometries WHERE rows >= ?",
                                                (vggt_place.MIN_INLIERS,)):
        a, b = divmod(pair, vggt_place.MAX_IMAGE_ID)
        if a not in by_id or b not in by_id:
            continue
        if config == 2 and E:       # calibrated: E is in normalized coordinates of the database's own intrinsics
            M = np.frombuffer(E, np.float64).reshape(3, 3)
        elif F:
            M = K[b].T @ np.frombuffer(F, np.float64).reshape(3, 3) @ K[a]
        else:
            continue
        if not np.isfinite(M).all() or not M.any():
            continue
        U, _, Vt = np.linalg.svd(M)
        if np.linalg.det(U) < 0:
            U = -U
        if np.linalg.det(Vt) < 0:
            Vt = -Vt
        out[(by_id[a]["name"], by_id[b]["name"])] = [U @ W @ Vt, U @ W.T @ Vt]
    con.close()
    return out


def disagreeing(placed: dict, pairs: dict) -> tuple[set[str], dict[str, float]]:
    """Photos whose VGGT camera disagrees with more than half of their pairs, and each photo's share disagreeing."""
    angle = lambda A, B: float(np.degrees(np.arccos(np.clip((np.trace(A @ B.T) - 1) / 2, -1, 1))))     # noqa: E731
    bad, total = {}, {}
    for (a, b), candidates in pairs.items():
        rel = np.array(placed[b][0]) @ np.array(placed[a][0]).T
        off = min(angle(rel, c) for c in candidates) > MAX_ANGLE
        for n in (a, b):
            total[n] = total.get(n, 0) + 1
            bad[n] = bad.get(n, 0) + off
    share = {n: bad[n] / total[n] for n in total}
    return {n for n, s in share.items() if s > 0.5}, share


def main(db: Path, images: Path, run: Path) -> None:
    times, work, out = {}, run / "vggt", run / "sparse" / "0"
    work.mkdir(exist_ok=True)

    def timed(name, f):
        start = time.time()
        result = f()
        times[name] = round(time.time() - start, 1)
        print(f"BENCH_PART {name} {times[name]}", flush=True)
        return result

    def colmap(name, *args):
        timed(name, lambda: subprocess.run(["colmap", *args], check=True))
    import torch
    device, limit = vggt_place.capacity()
    chosen = vggt_place.choose(db, limit)
    print(f"VGGT: {len(chosen)} photos on {device} (room for {limit})", flush=True)
    placed = timed("vggt", lambda: vggt_place.predict([images / c["name"] for c in chosen], device, print))
    peak = torch.cuda.max_memory_allocated() // 2 ** 20 if device == "cuda" else 0
    pairs = pair_rotations(db, chosen, {n: p[2] for n, p in placed.items()})
    dropped, share = disagreeing(placed, pairs)
    kept = [c for c in chosen if c["name"] not in dropped]
    print(f"rotation check: {len(pairs)} pairs, dropped {len(dropped)} of {len(chosen)} cameras", flush=True)
    model = work / "start"
    vggt_place.write_model(model, kept, placed)
    for i, (angle, err, refine_focal) in enumerate(vggt_place.ROUNDS):
        tri, adjusted = work / f"triangulated{i}", out if i == len(vggt_place.ROUNDS) - 1 else work / f"adjusted{i}"
        tri.mkdir(parents=True, exist_ok=True)
        adjusted.mkdir(parents=True, exist_ok=True)
        colmap(f"triangulate{i}", "point_triangulator", "--database_path", str(db), "--image_path", str(images),
               "--input_path", str(model), "--output_path", str(tri),
               "--Mapper.tri_create_max_angle_error", str(angle), "--Mapper.tri_continue_max_angle_error", str(angle),
               "--Mapper.tri_re_max_angle_error", str(angle), "--Mapper.tri_merge_max_reproj_error", str(err),
               "--Mapper.tri_complete_max_reproj_error", str(err), "--Mapper.filter_max_reproj_error", str(err),
               "--Mapper.tri_ignore_two_view_tracks", "0", "--Mapper.tri_min_angle", "1", "--Mapper.filter_min_tri_angle", "1")
        colmap(f"adjust{i}", "bundle_adjuster", "--input_path", str(tri), "--output_path", str(adjusted),
               "--BundleAdjustment.refine_focal_length", str(refine_focal))
        model = adjusted
    (run / "vggt.json").write_text(json.dumps({
        "times": times, "vggt_gpu_peak_mib": int(peak), "chosen": len(chosen), "pairs_checked": len(pairs),
        "dropped": sorted(dropped), "disagreeing_share": {n: round(s, 2) for n, s in sorted(share.items()) if s > 0.25},
        "points": vggt_place.count(out, "points3D")}, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
