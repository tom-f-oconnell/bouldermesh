#!/usr/bin/env python3
"""A video's frames as the harvest keeps them (climbs/harvest.py): the sharpest spread over its length, then those that
repeat a kept frame left out (drop_repeats()), the sharper of two kept. Offline: frames are made here, and a video of
them with ffmpeg where it's installed."""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402

from climbs import diversity, harvest  # noqa: E402

W, H = 1280, 720


def scene(seed: int) -> Image.Image:
    """A picture with texture at several sizes, as rock has: blotches of three sizes, each its own brightness."""
    rng = np.random.default_rng(seed)
    img = np.zeros((H, W), float)
    for cell, weight in ((64, 60), (16, 40), (4, 25)):
        small = rng.uniform(-1, 1, (H // cell + 1, W // cell + 1))
        img += weight * np.kron(small, np.ones((cell, cell)))[:H, :W]
    return Image.fromarray(np.clip(128 + img, 0, 255).astype(np.uint8)).convert("RGB")


def frame(img: Image.Image, shift: int = 0, blur: float = 0.0, seed: int = 0) -> Image.Image:
    """The scene moved across by shift pixels (a pan: no parallax), blurred, with a little noise, as a video's frame."""
    out = img.transform(img.size, Image.AFFINE, (1, 0, shift, 0, 1, 0), resample=Image.BILINEAR)
    if blur:
        out = out.filter(ImageFilter.GaussianBlur(blur))
    noise = np.random.default_rng(seed).normal(0, 3, (H, W, 1))
    return Image.fromarray(np.clip(np.asarray(out, float) + noise, 0, 255).astype(np.uint8))


with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    rock, other = scene(1), scene(2)
    # a camera held still on the rock (one frame sharper than the rest), then panning, then another scene, then back
    plan = ([(rock, 0, 2.0), (rock, 0, 0.0), (rock, 0, 2.0), (rock, 0, 1.5)] + [(rock, 40 * k, 1.0) for k in range(1, 4)]
            + [(other, 0, 1.0), (other, 0, 1.0)] + [(rock, 0, 1.0)])
    frames = []
    for i, (img, shift, blur) in enumerate(plan):
        f = tmp / f"f{i + 1:04d}.jpg"
        frame(img, shift, blur, seed=i).save(f, quality=92)
        frames.append(f)
    logged = []
    start = time.time()
    kept = harvest.drop_repeats(frames, {}, logged.append)
    took = time.time() - start
    names = [f.name for f in kept]
    assert names == ["f0002.jpg", "f0008.jpg"], (names, logged)
    assert logged and logged[0].startswith(f"frames: {len(frames) - 2} of {len(frames)} left out"), logged
    print(f"repeats left out, the sharper kept OK ({took:.1f} s for {len(frames)} frames)")

    # different scenes all kept; one frame alone kept as it is
    a, b = tmp / "a.jpg", tmp / "b.jpg"
    frame(rock, seed=20).save(a)
    frame(other, seed=21).save(b)
    assert harvest.drop_repeats([a, b], {}, logged.append) == [a, b]
    assert harvest.drop_repeats([a], {}, logged.append) == [a]
    # diversity's own pair test, which view_graph uses as it did
    fa, fb, fa2 = diversity.orb_features([a, b, frames[1]])
    k, same = diversity.pair(fa, fa2)
    assert same and k >= diversity.IDENTICAL_INLIERS, k
    k, same = diversity.pair(fa, fb)
    assert not same and k < diversity.MIN_INLIERS, k
    print("different frames kept, and diversity's pair test OK")

    # a whole video through extract_frames(), where ffmpeg is installed: its frames chosen, then its repeats left out
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        clip = tmp / "clip"
        clip.mkdir()
        seq = [rock] * 8 + [other] * 8
        for i, img in enumerate(seq):
            frame(img, blur=0.5, seed=100 + i).save(clip / f"s{i:03d}.png")
        video = tmp / "video.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-framerate", "4", "-i", str(clip / "s%03d.png"), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-crf", "18", str(video)], check=True, timeout=120)
        stats = {}
        got = harvest.extract_frames(video, tmp / "work", 8, logged.append, fps=2, stats=stats)
        assert len(got) == 2 and all(f.exists() for f in got), (got, logged[-3:])
        assert stats == {"chosen": 8, "repeats": 6}, "the places repeats leave aren't another video's (video_frames())"
        assert sorted(p.name for p in (tmp / "work").glob("f*.jpg")) == sorted(f.name for f in got), "the rest deleted"
        print("a video's frames OK")
    else:
        print("no ffmpeg: a whole video not tried")
print("ALL FRAMES TESTS PASSED")
