"""COLMAP 4.2.1's incremental mapper through pycolmap (the venv's wheel: CPU bundle adjustment), for asking why it
places what it does without Docker: probe.py <database> <photos> <out dir> <seed> <threads> [kind=global] [option=value ...]
Options are IncrementalPipelineOptions fields (mapper.x for its IncrementalMapperOptions). Prints the models' sizes."""
import shutil
import sys
import time
from pathlib import Path

import pycolmap


def main(db: str, photos: str, out: str, seed: int, threads: int, *settings: str) -> None:
    o = Path(out)
    shutil.rmtree(o, ignore_errors=True)
    o.mkdir(parents=True)
    shutil.copyfile(db, o / "database.db")          # 4.2.1 rewrites a 3.9.1 database
    world = "kind=global" in settings      # the global mapper, with the app's equivalents, on the CPU
    settings = tuple(x for x in settings if x != "kind=global")
    opts = pycolmap.GlobalPipelineOptions() if world else pycolmap.IncrementalPipelineOptions()
    opts.random_seed, opts.num_threads, opts.min_model_size, opts.min_num_matches = seed, threads, 6, 20
    if world:
        opts.mapper.bundle_adjustment.refine_principal_point = True
        opts.mapper.bundle_adjustment.ceres.use_gpu = opts.mapper.global_positioning.use_gpu = False
    else:
        opts.ba_refine_principal_point = True
    for s in settings:
        k, v = s.split("=")
        target = opts
        while "." in k:
            head, k = k.split(".", 1)
            target = getattr(target, head)
        setattr(target, k, type(getattr(target, k))(float(v)) if not isinstance(getattr(target, k), bool) else v == "1")
    pycolmap.set_random_seed(seed)
    start = time.time()
    run = pycolmap.global_mapping if world else pycolmap.incremental_mapping
    models = run(str(o / "database.db"), photos, str(o), opts)
    sizes = sorted((m.num_reg_images() for m in models.values()), reverse=True)
    print(f"RESULT {o.name} seed={seed} {' '.join(settings)} models={sizes} {time.time() - start:.0f}s", flush=True)
    (o / "database.db").unlink()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), *sys.argv[6:])
