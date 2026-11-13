"""Where bundle adjustment starts paying on the GPU: one global bundle adjustment of models of each size, on the CPU
(Ceres, its own solver choice) and on the GPU (Ceres with CUDA; Caspar), the median of three runs each.

    crossover.py <out.json> [threads] [sizes...]        (in the pipeline's tools: inenv.sh -g python3 /incr/crossover.py ..)
    crossover.py real <out.json> <threads> <model dir>...   the same on real models (a state's model/), with each
                                                            backend's result compared with the CPU's

The models are COLMAP's synthetic ones (synthesize_dataset), with points and noise like a boulder's: 300 points
seen per photo, 1 px of noise. A real boulder's model can solve differently (its cameras see each other's points
far less evenly); core.py's GPU_MIN_IMAGES is set from this and checked against the BENCH_BA lines of real runs.
"""
import copy
import json
import statistics
import sys
import time

import pycolmap


def model(images: int, seed: int) -> pycolmap.Reconstruction:
    s = pycolmap.SyntheticDatasetOptions()
    s.num_rigs, s.num_cameras_per_rig, s.num_frames_per_rig = 1, 1, images
    s.num_points3D = max(2000, images * 150)
    pycolmap.set_random_seed(seed)
    rec = pycolmap.synthesize_dataset(s)
    n = pycolmap.SyntheticNoiseOptions()
    n.point2D_stddev = 1.0
    n.rig_from_world_rotation_stddev = 0.5
    n.rig_from_world_translation_stddev = 0.05
    n.point3D_stddev = 0.05
    pycolmap.synthesize_noise(n, rec)
    return rec


def solve(rec, how: str, threads: int) -> float:
    b = pycolmap.BundleAdjustmentOptions()
    b.print_summary = False
    b.ceres.solver_options.num_threads = threads
    b.ceres.solver_options.max_num_iterations = 50
    if how == "gpu":
        b.ceres.use_gpu, b.ceres.min_num_images_gpu_solver = True, 0
    elif how == "caspar":
        b.backend = pycolmap.BundleAdjustmentBackend.CASPAR
    r = copy.deepcopy(rec)
    t = time.time()
    pycolmap.bundle_adjustment(r, b)
    return time.time() - t


def main(out: str, threads: int, sizes: list[int]) -> None:
    rows = []
    for n in sizes:
        rec = model(n, n)
        row = {"images": n, "points": rec.num_points3D(), "observations": rec.compute_num_observations()}
        for how in ("cpu", "gpu", "caspar"):
            try:
                row[how] = round(statistics.median(solve(rec, how, threads) for _ in range(3)), 3)
            except Exception as e:      # a backend this build lacks
                row[how] = f"{type(e).__name__}: {e}"
        print(json.dumps(row), flush=True)
        rows.append(row)
    with open(out, "w") as f:
        json.dump({"threads": threads, "rows": rows}, f, indent=1)


def real(out: str, threads: int, models: list[str]) -> None:
    """Each model bundle adjusted on each device from the same start, timed, with the reprojection error after and
    how far each backend's cameras end from the CPU's (Caspar solves in float32)."""
    import numpy as np
    rows = []
    for path in models:
        base = pycolmap.Reconstruction(path)
        row = {"model": path, "images": base.num_reg_images(), "points": base.num_points3D(), "observations": base.compute_num_observations()}
        results = {}
        for how in ("cpu", "gpu", "caspar"):
            times, r = [], None
            for _ in range(3):
                r = copy.deepcopy(base)
                b = pycolmap.BundleAdjustmentOptions()
                b.print_summary = False
                b.refine_principal_point = False
                b.ceres.solver_options.num_threads = threads
                if how == "gpu":
                    b.ceres.use_gpu, b.ceres.min_num_images_gpu_solver = True, 0
                elif how == "caspar":
                    b.backend = pycolmap.BundleAdjustmentBackend.CASPAR
                t = time.time()
                pycolmap.bundle_adjustment(r, b)
                times.append(time.time() - t)
            results[how] = r
            row[how] = round(statistics.median(times), 3)
            row[how + "_err_px"] = round(r.compute_mean_reprojection_error(), 4)
        ref = results["cpu"]
        for how in ("gpu", "caspar"):
            d = [float(np.linalg.norm(results[how].image(i).projection_center() - ref.image(i).projection_center())) for i in ref.reg_image_ids()]
            spread = float(np.sqrt(np.var([ref.image(i).projection_center() for i in ref.reg_image_ids()], axis=0).sum()))
            row[how + "_vs_cpu_median"] = round(float(np.median(d)) / spread, 6)
        print(json.dumps(row), flush=True)
        rows.append(row)
    with open(out, "w") as f:
        json.dump({"threads": threads, "rows": rows}, f, indent=1)


if __name__ == "__main__":
    if sys.argv[1] == "real":
        real(sys.argv[2], int(sys.argv[3]), sys.argv[4:])
        sys.exit()
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 6,
         [int(x) for x in sys.argv[3:]] or [10, 20, 35, 50, 80, 120, 200, 300])
