"""VGGT's cameras for the incremental pipeline, as a file core.py reads (runs in the app's -gpu image, from boulder.py).

    vggt_cams.py skeleton <database> <photos> <out.json>             a first build's start
    vggt_cams.py assist <database> <photos> <out.json> <request>     photos registration could not place

skeleton: as backend/vggt_place.py's place(), the photos best linked by verified matches, as many as the GPU has room
for (80 at most), with the benchmark's check (vggt_start.py): a camera whose rotation relative to most of the photos
it shares a verified pair with disagrees with those pairs' two-view geometry is listed as dropped.
assist: the request's photos to place and the placed photos to tie VGGT's frame to the model (its anchors), in one
call; as many of the photos to place as fit beside the anchors.

Writes {"chosen": [{id, name, camera, width, height}], "placed": {name: [rotation, translation, focal length]},
"dropped": [names], "names" and "anchors" (assist), "seconds", "gpu_peak_mib", "device", "pairs_checked"}.
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app/backend")
sys.path.insert(0, "/bench")
import vggt_place  # noqa: E402
import vggt_start  # noqa: E402


def rows(db: Path, names: list[str]) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sizes = {c: (w, h) for c, w, h in con.execute("SELECT camera_id, width, height FROM cameras")}
        by_name = {n: (i, c) for i, n, c in con.execute("SELECT image_id, name, camera_id FROM images")}
    finally:
        con.close()
    return [{"id": by_name[n][0], "name": n, "camera": by_name[n][1], "width": sizes[by_name[n][1]][0],
             "height": sizes[by_name[n][1]][1]} for n in names if n in by_name]


def main(mode: str, db: Path, photos: Path, out: Path, request: Path | None = None) -> None:
    import torch
    why = vggt_place.unavailable()
    if why:
        raise SystemExit(f"VGGT can't run here: {why}")
    device, limit = vggt_place.capacity()
    if device != "cuda":        # never quietly on the CPU, where it takes minutes
        raise SystemExit(f"VGGT would run on {device}, not CUDA: torch {torch.__version__}, CUDA available: {torch.cuda.is_available()}")
    print(f"torch {torch.__version__} on {torch.cuda.get_device_name(0)}, cuDNN {torch.backends.cudnn.version()}", flush=True)
    extra = {}
    if mode == "skeleton":
        chosen = vggt_place.choose(db, limit)
    else:
        req = json.loads(Path(request).read_text())
        anchors = req["anchors"][:max(3, limit // 3)]
        names = req["names"][:limit - len(anchors)]
        chosen = rows(db, anchors + names)
        extra = {"names": names, "anchors": anchors, "left": req["names"][len(names):]}
    if len(chosen) < vggt_place.MIN_PHOTOS:
        raise SystemExit(f"only {len(chosen)} photo(s) to give VGGT")
    print(f"VGGT: {len(chosen)} photos on {device} (room for {limit})", flush=True)
    start = time.time()
    placed = vggt_place.predict([photos / c["name"] for c in chosen], device, print)
    seconds = round(time.time() - start, 1)
    peak = int(torch.cuda.max_memory_allocated() // 2 ** 20) if device == "cuda" else 0
    pairs = vggt_start.pair_rotations(db, chosen, {n: p[2] for n, p in placed.items()})
    dropped, share = vggt_start.disagreeing(placed, pairs)
    print(f"rotation check: {len(pairs)} pairs, {len(dropped)} of {len(chosen)} cameras disagree with most of theirs", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"chosen": chosen, "placed": placed, "dropped": sorted(dropped), **extra,
                               "disagreeing_share": {n: round(s, 2) for n, s in sorted(share.items()) if s > 0.25},
                               "seconds": seconds, "gpu_peak_mib": peak, "device": device, "pairs_checked": len(pairs)}))


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]) if len(sys.argv) > 5 else None)
