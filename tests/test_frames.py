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
        assert (stats["chosen"], stats["repeats"], stats["way"]) == (8, 6, "stretches"), "the places repeats leave aren't another video's (video_frames())"
        assert sorted(p.name for p in (tmp / "work").glob("f*.jpg")) == sorted(f.name for f in got), "the rest deleted"
        assert sorted(stats["times"]) == sorted(f.name for f in got) and stats["times"][got[1].name] >= 2.0, stats["times"]
        print("a video's frames OK")

        def film(name: str, parts: list[tuple[Image.Image, float]]) -> pathlib.Path:
            """A video of stills, each held for its seconds, at 2 frames a second."""
            d = tmp / name
            d.mkdir()
            lines = []
            for i, (img, secs) in enumerate(parts):
                img.save(d / f"p{i:04d}.png")
                lines += [f"file '{d / f'p{i:04d}.png'}'", f"duration {secs}"]
            lines.append(lines[-2])     # the concat demuxer wants the last file named again
            (d / "list.txt").write_text("\n".join(lines) + "\n")
            out = tmp / f"{name}.mp4"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(d / "list.txt"), "-vf", "fps=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(out)], check=True, timeout=300)
            return out

        def round_the_rock(img: Image.Image, k: int) -> Image.Image:
            """The scene as from further round it: a change of perspective no shift, turn and zoom explains."""
            a = 0.00012 * k
            return img.transform(img.size, Image.PERSPECTIVE, (1 + 0.06 * k, 0.02 * k, -30 * k, 0.01 * k, 1 + 0.03 * k, -8 * k, a, a / 2),
                                 resample=Image.BILINEAR)

        still = frame(rock, blur=0.6, seed=1)
        # one shot from a tripod: looking for edits changes nothing, frame for frame
        tripod = film("tripod", [(frame(rock, blur=0.6, seed=i), 2) for i in range(12)])
        plain, edited, st = (harvest.extract_frames(tripod, tmp / "w1", 6, logged.append, fps=1),
                             harvest.extract_frames(tripod, tmp / "w2", 6, logged.append, fps=1, stats=stats, edits=True), dict(stats))
        assert [f.name for f in plain] == [f.name for f in edited] and stats["way"] == "stretches" and stats["shots"] == 1, (plain, edited, stats)
        print("one shot is done as before OK")

        # an edit: 40 s from a tripod, 10 s going round the rock, 8 s of something else, and 10 s back on the tripod.
        # Its share goes to the views it has, not to its minutes: most to the 10 s round the rock, one to each tripod
        parts = ([(still, 40)] + [(frame(round_the_rock(rock, k), blur=0.6, seed=40 + k), 1) for k in range(1, 11)]
                 + [(frame(other, blur=0.6, seed=60), 8), (still, 10)])
        edit = film("edit", parts)
        stats = {}
        by_time = harvest.extract_frames(edit, tmp / "w3", 8, logged.append, fps=1, stats=stats)
        t_old = sorted(stats["times"].values())
        stats = {}
        by_shot = harvest.extract_frames(edit, tmp / "w4", 8, logged.append, fps=1, stats=stats, edits=True)
        t_new = sorted(stats["times"].values())
        going_round = lambda ts: sum(40 <= t < 50 for t in ts)  # noqa: E731
        assert stats["way"] == "shots" and stats["shots"] == 3 and stats["views"] >= 8, (stats, logged[-3:])
        assert going_round(t_new) >= 5 > going_round(t_old), (t_old, t_new)
        assert sum(t < 40 or t >= 58 for t in t_new) <= 2 and sum(50 <= t < 58 for t in t_new) == 1, t_new   # the tripod, though it's cut back to, not once a stretch
        # several shots: twice its places to choose from, as the filter takes out what isn't the rock; its share is still 8
        assert stats["chosen"] == 8 and len(by_shot) <= 8 * harvest.EDIT_SPARE and len(by_shot) > 8, (stats, len(by_shot))
        assert sorted(set(stats["shot_of"].values())) == [0, 1, 2] and set(stats["shot_of"]) == {f.name for f in by_shot}, stats["shot_of"]
        print(f"an edit's frames go to its views OK (round the rock: {going_round(t_old)} of {len(t_old)} by its minutes, {going_round(t_new)} of {len(t_new)} by its shots)")

        # a fade through black is a cut; a blurred frame within a shot isn't
        black = Image.new("RGB", (W, H))
        faded = film("faded", [(still, 4), (black, 2), (frame(other, blur=0.6, seed=61), 4)])
        work = tmp / "w5"
        work.mkdir()
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(faded), "-vf", "fps=1", str(work / "f%04d.jpg")], check=True, timeout=120)
        cut_at = [len(s) for s in harvest.find_shots(sorted(work.glob("f*.jpg")), logged.append)]
        assert len(cut_at) == 2 and cut_at[0] == 6, (cut_at, "black stays with the shot before it")
        print("cuts through black OK")

        # a moment linked: only from just before it, for as long as beta looks
        harvest.MOMENT_SECONDS, real = 20.0, harvest.MOMENT_SECONDS
        try:
            linked = film("linked", [(frame(other, blur=0.6, seed=62), 60), (still, 15)]
                          + [(frame(round_the_rock(rock, k), blur=0.6, seed=70 + k), 1) for k in range(1, 9)] + [(frame(other, blur=0.6, seed=63), 60)])
            stats = {}
            got = harvest.extract_frames(linked, tmp / "w6", 6, logged.append, fps=1, stats=stats, start=70.0, edits=True)
            assert stats["span"] == [60.0, 90.0] and got and all(60 <= t <= 90 for t in stats["times"].values()), stats
            assert sum(75 <= t < 83 for t in stats["times"].values()) >= 3, stats["times"]
            stats = {}
            harvest.extract_frames(linked, tmp / "w7", 6, logged.append, fps=1, stats=stats, start=70.0)      # not asked to: the whole of it
            assert stats["span"][0] == 0 and stats["way"] == "stretches", stats
        finally:
            harvest.MOMENT_SECONDS = real
        print("a linked moment OK")

        # a long video: looked at whole, through no more than MAX_CANDIDATES frames
        harvest.LONG_SECONDS, harvest.MAX_CANDIDATES, real = 60.0, 40, (harvest.LONG_SECONDS, harvest.MAX_CANDIDATES)
        try:
            long_one = film("long", [(frame(other, blur=0.6, seed=64), 50), (still, 20)]
                            + [(frame(round_the_rock(rock, k), blur=0.6, seed=80 + k), 4) for k in range(1, 6)] + [(frame(other, blur=0.6, seed=64), 30)])
            stats = {}
            got = harvest.extract_frames(long_one, tmp / "w8", 6, logged.append, fps=1, stats=stats, edits=True)
            assert stats["way"] == "shots" and len(list((tmp / "w8").glob("f*.jpg"))) == len(got) <= 6 * harvest.EDIT_SPARE, stats
            assert any("through 4" in line and "of its 2 min" in line for line in logged[-4:]), logged[-4:]
            assert sum(70 <= t < 90 for t in stats["times"].values()) >= 3, stats["times"]
        finally:
            harvest.LONG_SECONDS, harvest.MAX_CANDIDATES = real
        print("a long video OK")
    else:
        print("no ffmpeg: a whole video not tried")
print("ALL FRAMES TESTS PASSED")
