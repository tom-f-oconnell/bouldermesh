"""The app's people masks for a folder of photos, with the app's own code, for bench.py's people steps (run in the
app's image): people_step.py <photos> <out folder> <mask name pattern>

The pattern names each photo's mask from its {name} (yt_x_000.jpg) or {stem} (yt_x_000): the app's first pass
writes {name}.png beside the photos' masks, its second {stem}.mask.png beside the undistorted photos. The model is
loaded first, on one photo, and that marked (BENCH_MARK loaded), so that the masks' own time can be told from the
load and the one-off download of its weights. PEOPLE_GPU=0 keeps it off the GPU, as in the app. PEOPLE_BACKEND names
another backend folder than the image's /app/backend (a newer one, mounted).
"""
import inspect
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("PEOPLE_BACKEND", "/app/backend"))
import occlusion  # noqa: E402


def main(photos: Path, out: Path, pattern: str) -> None:
    log = lambda text: print(text, flush=True)      # noqa: E731
    name_for = lambda f: pattern.format(name=f.name, stem=f.stem)      # noqa: E731
    files = sorted(f for f in photos.iterdir() if f.suffix.lower() in occlusion.PHOTO_EXT and not f.name.endswith(".mask.png"))
    more = {"gpu": os.environ.get("PEOPLE_GPU", "1") != "0"} if "gpu" in inspect.signature(occlusion.write_masks).parameters else {}
    start = time.time()
    if files:
        occlusion.person_mask(files[0], log)
    print(f"BENCH_MARK loaded {time.time():.3f}\nBENCH_PART load {time.time() - start:.1f}", flush=True)
    start = time.time()
    shares = occlusion.write_masks(photos, out, name_for, log, **more)
    print(f"BENCH_MARK masked {time.time():.3f}\nBENCH_PART masks {time.time() - start:.1f}")
    print(f"BENCH_PEOPLE photos={len(files)} masks={len(shares)} device={getattr(occlusion, '_where', '?')} "
          f"with_people={sum(1 for v in shares.values() if v > 0)}", flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
