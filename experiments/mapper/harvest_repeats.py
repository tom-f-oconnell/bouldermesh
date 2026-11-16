"""The video frames the app's harvest would drop as repeats, for a benchmark boulder: harvest_repeats.py <project> ...

Runs backend/climbs/harvest.py's drop_repeats() on each video's frames (yt_<video>_<n>.jpg, in order), which are the
sharpest frames the harvest chose: drop_repeats() is what extract_frames() now does to those. Writes the dropped frames
to r2/<project>/sift391_harvest/listed.json, as {frame: "harvest"}, for the derived set sift391_harvest, and prints them
beside sift391_dedup90's. Needs OpenCV, numpy and PIL.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "backend")]
import bench  # noqa: E402
from climbs import harvest  # noqa: E402

for P in sys.argv[1:]:
    photos = bench.BENCH / "projects" / P / "photos"
    vids = {}
    for f in photos.iterdir():
        if m := re.fullmatch(r"yt_([0-9a-f]+)_(\d+)\.jpg", f.name):
            vids.setdefault(m.group(1), []).append((int(m.group(2)), f))
    dropped = []
    for v, frames in sorted(vids.items()):
        frames = [f for _, f in sorted(frames)]
        kept = harvest.drop_repeats(frames, {}, lambda m: None)
        dropped += [f.name for f in frames if f not in kept]
    d = bench.R / P / "sift391_harvest"
    d.mkdir(parents=True, exist_ok=True)
    (d / "listed.json").write_text(json.dumps({n: "harvest" for n in sorted(dropped)}, indent=1))
    ref = bench.R / P / "sift391_dedup90" / "derive.json"
    old = set(json.loads(ref.read_text())["dropped_frames"]) if ref.exists() else set()
    frames = sum(len(f) for f in vids.values())
    print(f"{P}: {frames} frames in {len(vids)} videos; the harvest drops {len(dropped)}, dedup90 dropped {len(old)}; "
          f"both {len(set(dropped) & old)}, only the harvest {len(set(dropped) - old)}, only dedup90 {len(old - set(dropped))}")
